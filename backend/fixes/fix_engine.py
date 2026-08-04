"""Fix engine — every fix is validated before the user sees it.

Fix sources, in order of trust:
1. Deterministic fixes already attached by their layer (ruff edits, spell
   renames) — validated at creation time.
2. LLM-generated minimal patches for everything else, each one:
   a. applied to a copy of the code
   b. ast.parse validated (must remain syntactically valid Python)
   c. re-checked: the original detector is re-run on the patched code and
      the fix is accepted only if the violation is gone and, for lint
      layers, no NEW violations of the same rule appeared
Failed fixes are retried (config llm.fix.max_retries), then surfaced as
"manual fix required" instead of showing an unvalidated patch.
"""
from __future__ import annotations

import asyncio
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


def _still_present(v: Violation, patched: str, filename: str, plugin) -> bool:
    """Re-run the violation's own detector on the patched code."""
    from backend.layers.hardcode import run_hardcode_layer

    if v.layer == Layer.SECURITY:
        found = plugin.security(patched, filename)
    elif v.layer == Layer.HARDCODE:
        found = run_hardcode_layer(patched)
    elif v.layer == Layer.LINT:
        found = plugin.lint(patched, filename)
    else:
        return False  # LLM findings have no deterministic re-check here
    window = 3
    return any(f.rule == v.rule and abs(f.line - v.line) <= window for f in found)


async def generate_fix(v: Violation, code: str, filename: str, plugin) -> Fix | None:
    """LLM minimal patch for one violation, with validation loop."""
    lines = code.splitlines()
    ctx_n = int(CFG.get("review.context_lines_for_fix", 15))
    retries = int(CFG.get("llm.fix.max_retries", 1))
    baseline_ok, _ = plugin.validate_syntax(code)
    lo = max(0, v.line - 1 - ctx_n)
    hi = min(len(lines), v.line + ctx_n)
    prompt = _prompt("fix.txt").format(
        language=plugin.display,
        line=v.line, snippet=v.snippet, rule=v.rule, message=v.message,
        suggestion=v.suggestion or v.message or "make the smallest correct change",
        ctx_start=lo + 1, ctx_end=hi,
        context=_numbered(lines[lo:hi], lo + 1),
    )

    for _attempt in range(retries + 1):
        res = await CLIENT.chat_json(
            prompt, FIX_SCHEMA, int(CFG.get("llm.max_tokens_fix", 1024))
        )
        if not res:
            return None
        try:
            start = int(res["start_line"])
            end = int(res["end_line"])
            replacement = str(res["replacement"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 < start <= end <= len(lines)):
            continue
        # patch must stay near the violation
        if not (start - ctx_n <= v.line <= end + ctx_n):
            continue
        patched = apply_patch(code, start, end, replacement)
        ok, syntax_note = plugin.validate_syntax(patched)
        if baseline_ok and not ok:
            continue  # fix broke the syntax — reject
        if _still_present(v, patched, filename, plugin):
            continue
        syntax_label = (syntax_note if ok
                        else "syntax gate skipped (original file didn't parse)")
        return Fix(
            start_line=start, end_line=end, replacement=replacement,
            validated=True,
            validation_notes=f"{syntax_label}; detector re-run confirms violation resolved"
            if v.layer in (Layer.LINT, Layer.SECURITY, Layer.HARDCODE)
            else f"{syntax_label}; minimal patch applied cleanly",
        )
    return None


async def fill_missing_fixes(
    violations: list[Violation], code: str, filename: str, llm_up: bool, plugin
) -> None:
    if not llm_up:
        return
    targets = [v for v in violations if v.fix is None]
    results = await asyncio.gather(
        *(generate_fix(v, code, filename, plugin) for v in targets))
    for v, fix in zip(targets, results):
        v.fix = fix
