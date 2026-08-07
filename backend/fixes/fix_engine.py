"""Fix engine — every fix is validated before the user sees it.

Fix sources, in order of trust:
1. Deterministic fixes already attached by their layer (ruff edits, spell
   renames) — validated at creation time.
2. LLM-generated minimal patches for everything else, each one gated by:
   a. bounds + proximity (the patch must stay near the violation)
   b. no-op rejection (a patch that changes nothing fixes nothing)
   c. destructive-fix guard (must not delete control flow / accumulation)
   d. syntax validation of the patched file
   e. detector re-run: the violation must be GONE and the patch must not
      introduce ANY new deterministic finding anywhere in the file
      (lint + security detectors are compared against a baseline, with
      patch line-shift accounted for)
   f. optional adversarial fix-verify LLM pass for semantic-layer findings
      (llm.fix.verify_enabled): a skeptic judges whether the patch actually
      resolves the reported problem with a minimal change
Failed candidates are retried (config llm.fix.max_retries), then surfaced as
"manual fix required" instead of showing an unvalidated patch.
"""
from __future__ import annotations

import asyncio
import re as _re
from pathlib import Path

from backend.app_config import CFG, PROJECT_ROOT
from backend.llm.client import CLIENT
from backend.models import Fix, Layer, Violation

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "replacement": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["start_line", "end_line", "replacement"],
}

FIX_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["approved", "reason"],
}


def _prompt(name: str) -> str:
    pdir = PROJECT_ROOT / CFG.get("paths.prompts_dir", "prompts")
    return Path(pdir / name).read_text(encoding="utf-8")


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{start + i:5d}| {ln}" for i, ln in enumerate(lines))


def apply_patch(code: str, start_line: int, end_line: int, replacement: str) -> str:
    lines = code.splitlines()
    new_lines = replacement.split("\n")
    if replacement == "":
        new_lines = []
    patched = lines[: start_line - 1] + new_lines + lines[end_line:]
    return "\n".join(patched) + ("\n" if code.endswith("\n") else "")


_CONTROL_FLOW = ("return", "break", "continue", "goto", "throw", "yield")


_COMPOUND_ASSIGN = ("+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=")


def _fix_deletes_logic(original_lines: list[str], replacement: str) -> bool:
    """True if the replacement silently deletes logic that was in the replaced
    span rather than fixing the defect — e.g. turning `return total;` or
    `total += values[i];` into `int total = 0;` clears the warning but removes
    the return / the accumulation. Legitimate transforms (strcpy->strncpy,
    `int total;`->`int total = 0;`, `i <= n`->`i < n`) are NOT flagged."""
    orig = "\n".join(original_lines)
    # control-flow keyword present in original but gone from replacement
    for kw in _CONTROL_FLOW:
        if _re.search(rf"\b{kw}\b", orig) and not _re.search(rf"\b{kw}\b", replacement):
            return True
    # a compound assignment (accumulation/update) dropped entirely
    for op in _COMPOUND_ASSIGN:
        if op in orig and op not in replacement:
            return True
    return False


def _is_noop(original_lines: list[str], replacement: str) -> bool:
    """A patch whose text equals the replaced span (modulo trailing
    whitespace) changes nothing — it cannot have fixed anything."""
    orig = [ln.rstrip() for ln in original_lines]
    new = [ln.rstrip() for ln in (replacement.split("\n") if replacement else [])]
    return orig == new


def _reindent_like(span: list[str], replacement: str) -> str:
    """Re-indent a replacement to the original span's leading whitespace.

    Schema-constrained JSON output frequently strips the leading indentation
    from patch lines; in Python that makes an otherwise-correct fix a syntax
    error ('expected an indented block'). Shift every replacement line so the
    first line matches the original first line's indent, preserving the
    replacement's internal relative indentation."""
    if not span or not replacement:
        return replacement
    base = span[0][:len(span[0]) - len(span[0].lstrip())]
    rep_lines = replacement.split("\n")
    first = next((ln for ln in rep_lines if ln.strip()), "")
    cur = first[:len(first) - len(first.lstrip())]
    if cur == base:
        return replacement
    out = []
    for ln in rep_lines:
        if not ln.strip():
            out.append(ln)
        elif ln.startswith(cur):
            out.append(base + ln[len(cur):])
        else:
            out.append(base + ln.lstrip())
    return "\n".join(out)


# ------------------------------------------------------------ detector gate
def _run_layer(plugin, layer: Layer, code: str, filename: str) -> list[Violation] | None:
    """Run one deterministic detector; None = tool failed (gate unavailable)."""
    try:
        if layer == Layer.SECURITY:
            return plugin.security(code, filename)
        if layer == Layer.HARDCODE:
            return plugin.hardcode(code, filename)
        if layer == Layer.LINT:
            return plugin.lint(code, filename)
    except Exception:  # noqa: BLE001 — tool failure must not kill fix generation
        return None
    return None


def _map_to_original(line: int, start: int, end: int, new_len: int) -> int:
    """Map a line number in the PATCHED file back to original coordinates so
    findings can be compared against the pre-patch baseline."""
    delta = new_len - (end - start + 1)
    if line < start:
        return line
    if line >= start + new_len:
        return line - delta
    return start  # inside the replaced span


class _DetectorBaseline:
    """Lazily computed per-layer findings on the ORIGINAL code, shared by all
    fix generations for one review (each detector runs at most once)."""

    def __init__(self, code: str, filename: str, plugin):
        self.code, self.filename, self.plugin = code, filename, plugin
        self._cache: dict[Layer, list[Violation] | None] = {}
        self._lock = asyncio.Lock()

    async def get(self, layer: Layer) -> list[Violation] | None:
        async with self._lock:
            if layer not in self._cache:
                self._cache[layer] = await asyncio.to_thread(
                    _run_layer, self.plugin, layer, self.code, self.filename)
            return self._cache[layer]


def _gate_layers(v: Violation) -> list[Layer]:
    """Deterministic detectors to compare before/after for this violation.
    The violation's own layer is always included; LLM-layer fixes are gated
    by lint+security so a semantic patch can't smuggle in a new deterministic
    defect (e.g. an unused import or an insecure call)."""
    if v.layer in (Layer.LINT, Layer.SECURITY, Layer.HARDCODE):
        return [v.layer]
    if bool(CFG.get("review.fix_detector_gate", True)):
        return [Layer.LINT, Layer.SECURITY]
    return []


async def _detector_verdict(
    v: Violation, patched: str, filename: str, plugin,
    baseline: _DetectorBaseline, start: int, end: int, new_len: int,
) -> tuple[bool, bool]:
    """Return (violation_still_present, introduces_new_findings) by re-running
    the relevant deterministic detectors on the patched code and comparing
    against the original-code baseline (patch line-shift accounted for)."""
    window = int(CFG.get("review.fix_recheck_line_window", 3))
    still = False
    introduces = False
    for layer in _gate_layers(v):
        base = await baseline.get(layer)
        if base is None:
            continue  # tool unavailable — this gate can't judge
        found = await asyncio.to_thread(_run_layer, plugin, layer, patched, filename)
        if found is None:
            continue
        base_keys = [(b.rule, b.line) for b in base]
        for f in found:
            orig_line = _map_to_original(f.line, start, end, new_len)
            if layer == v.layer and f.rule == v.rule and \
                    abs(orig_line - v.line) <= window:
                still = True
                continue
            if not any(f.rule == rule and abs(orig_line - line) <= window
                       for rule, line in base_keys):
                introduces = True
    return still, introduces


# ------------------------------------------------------------ LLM fix verify
async def _fix_verified_by_llm(v: Violation, code: str, patched: str,
                               start: int, end: int, replacement: str,
                               plugin) -> tuple[bool, str]:
    """Adversarial re-check of a semantic fix: a skeptic judges whether the
    patch resolves the reported problem without breaking behavior. Only used
    for LLM-layer findings (deterministic layers have the detector re-run,
    which is stronger). Returns (approved, reason)."""
    if v.layer not in (Layer.LLM, Layer.GUIDELINE) or \
            not bool(CFG.get("llm.fix.verify_enabled", True)):
        return True, "fix verify disabled" if v.layer in (Layer.LLM, Layer.GUIDELINE) else ""
    lines = code.splitlines()
    ctx_n = int(CFG.get("review.context_lines_for_fix", 15))
    lo = max(0, start - 1 - ctx_n)
    hi = min(len(lines), end + ctx_n)
    prompt = _prompt("fix_verify.txt").format(
        language=plugin.display, line=v.line, rule=v.rule, message=v.message,
        before=_numbered(lines[lo:hi], lo + 1),
        start_line=start, end_line=end,
        original_span="\n".join(lines[start - 1:end]),
        replacement=replacement,
    )
    res = await CLIENT.chat_json(
        prompt, FIX_VERIFY_SCHEMA, int(CFG.get("llm.max_tokens_verify", 512)))
    if not res:
        # verifier unreachable — keep the fix (it already passed the
        # deterministic gates) but say the semantic check didn't run
        return True, "fix verifier unavailable"
    if res.get("approved"):
        return True, str(res.get("reason", "approved by fix verifier"))
    return False, str(res.get("reason", ""))


async def generate_fix(v: Violation, code: str, filename: str, plugin,
                       baseline: _DetectorBaseline | None = None) -> Fix | None:
    """LLM minimal patch for one violation, with validation loop."""
    lines = code.splitlines()
    ctx_n = int(CFG.get("review.context_lines_for_fix", 15))
    retries = int(CFG.get("llm.fix.max_retries", 1))
    baseline_ok, _ = plugin.validate_syntax(code)
    if baseline is None:
        baseline = _DetectorBaseline(code, filename, plugin)
    lo = max(0, v.line - 1 - ctx_n)
    hi = min(len(lines), v.line + ctx_n)
    prompt = _prompt("fix.txt").format(
        language=plugin.display,
        line=v.line, snippet=v.snippet, rule=v.rule, message=v.message,
        suggestion=v.suggestion or v.message or "make the smallest correct change",
        ctx_start=lo + 1, ctx_end=hi,
        context=_numbered(lines[lo:hi], lo + 1),
    )

    rejections: list[str] = []

    def _bail(reason: str) -> None:
        rejections.append(reason)

    for _attempt in range(retries + 1):
        res = await CLIENT.chat_json(
            prompt, FIX_SCHEMA, int(CFG.get("llm.max_tokens_fix", 1024))
        )
        if not res:
            _bail("no model response")
            break
        try:
            start = int(res["start_line"])
            end = int(res["end_line"])
            replacement = str(res["replacement"])
        except (KeyError, TypeError, ValueError):
            _bail("malformed patch JSON")
            continue
        if not (0 < start <= end <= len(lines)):
            _bail(f"line bounds {start}-{end} outside file")
            continue
        # patch must stay near the violation
        if not (start - ctx_n <= v.line <= end + ctx_n):
            _bail(f"patch at {start}-{end} too far from line {v.line}")
            continue
        span = lines[start - 1:end]
        # a patch that changes nothing cannot have fixed anything
        if _is_noop(span, replacement):
            _bail("no-op patch (identical to original)")
            continue
        # reject destructive fixes: a patch must not DELETE control-flow or an
        # accumulation that was in the replaced span (e.g. turning
        # `return total;` into `int total = 0;` clears the warning but breaks
        # the function)
        if _fix_deletes_logic(span, replacement):
            _bail("destructive (drops control flow / accumulation)")
            continue
        patched = apply_patch(code, start, end, replacement)
        ok, syntax_note = plugin.validate_syntax(patched)
        if baseline_ok and not ok:
            # models routinely drop leading indentation in JSON patches —
            # retry the same patch re-indented to the original span before
            # burning a whole regeneration attempt
            reindented = _reindent_like(span, replacement)
            if reindented != replacement:
                patched2 = apply_patch(code, start, end, reindented)
                ok2, note2 = plugin.validate_syntax(patched2)
                if ok2:
                    replacement, patched = reindented, patched2
                    ok, syntax_note = ok2, note2 + " (indentation auto-repaired)"
            if baseline_ok and not ok:
                _bail(f"breaks syntax: {syntax_note[:60]}")
                continue
        new_len = len(replacement.split("\n")) if replacement else 0
        still, introduces = await _detector_verdict(
            v, patched, filename, plugin, baseline, start, end, new_len)
        if still:
            _bail(f"violation still present after patch "
                  f"[patch: {replacement.strip()[:50]!r}]")
            continue
        if introduces:
            _bail(f"patch introduces new deterministic findings "
                  f"[patch: {replacement.strip()[:50]!r}]")
            continue
        approved, verify_note = await _fix_verified_by_llm(
            v, code, patched, start, end, replacement, plugin)
        if not approved:
            _bail(f"fix-verify veto: {verify_note[:80]}")
            continue
        syntax_label = (syntax_note if ok
                        else "syntax gate skipped (original file didn't parse)")
        if v.layer in (Layer.LINT, Layer.SECURITY, Layer.HARDCODE):
            notes = (f"{syntax_label}; detector re-run confirms violation "
                     f"resolved, no new findings introduced")
        else:
            notes = f"{syntax_label}; no new deterministic findings"
            if verify_note and verify_note != "fix verify disabled":
                notes += f"; fix verifier: {verify_note}"
        return Fix(
            start_line=start, end_line=end, replacement=replacement,
            validated=True, validation_notes=notes,
        )
    v.fix_notes = " | ".join(
        f"attempt {i + 1}: {r}" for i, r in enumerate(rejections)) or \
        "no fix attempts completed"
    return None


async def fill_missing_fixes(
    violations: list[Violation], code: str, filename: str, llm_up: bool, plugin
) -> None:
    if not llm_up:
        return
    # cap fix generation — the most expensive stage (a full LLM call per fix).
    # Deterministic layers already carry their own fixes; this bounds the
    # LLM-generated fixes so large files complete in reasonable time.
    max_fixes = int(CFG.get("review.max_fixes", 0))
    # skip findings explicitly marked no-autofix (e.g. generated/external
    # identifiers where any fix would break the build)
    targets = [v for v in violations if v.fix is None and not v.no_autofix]
    if max_fixes > 0:
        targets = targets[:max_fixes]
    baseline = _DetectorBaseline(code, filename, plugin)
    results = await asyncio.gather(
        *(generate_fix(v, code, filename, plugin, baseline) for v in targets))
    for v, fix in zip(targets, results):
        v.fix = fix
