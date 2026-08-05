"""L2 + L3 — LLM semantic review with anti-hallucination controls.

Accuracy mechanisms:
- numbered-line prompt; model must quote the exact code on the claimed line
- backend re-checks the quote against the real file (line-anchor validation);
  mismatched findings are rejected before anyone sees them
- category whitelist from config; anything else is rejected
- adversarial verify pass (L3): each surviving finding is re-judged in
  isolation by a skeptic prompt; unconfirmed findings are dropped
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.app_config import CFG, PROJECT_ROOT
from backend.llm.client import CLIENT
from backend.models import Layer, Severity, Violation

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "snippet": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["line", "snippet", "category", "severity", "message"],
            },
        }
    },
    "required": ["violations"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["confirmed", "reason"],
}

_SEV = {s.value: s for s in Severity}


def _prompt(name: str) -> str:
    pdir = PROJECT_ROOT / CFG.get("paths.prompts_dir", "prompts")
    return Path(pdir / name).read_text(encoding="utf-8")


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{start + i:5d}| {ln}" for i, ln in enumerate(lines))


def _anchor(claimed_line: int, snippet: str, lines: list[str]) -> int | None:
    """Validate the model's quote against the real file. Returns the true
    line number, or None if the quote matches nothing nearby (rejected)."""
    radius = int(CFG.get("llm.anchor_search_radius", 2))
    want = snippet.strip()
    if not want:
        return None
    for offset in sorted(range(-radius, radius + 1), key=abs):
        idx = claimed_line - 1 + offset
        if 0 <= idx < len(lines):
            have = lines[idx].strip()
            if want == have or (len(want) > 8 and want in have):
                return idx + 1
    return None


async def run_llm_review(code: str, filename: str, stats: dict,
                         plugin) -> list[Violation]:
    lines = code.splitlines()
    chunk_size = int(CFG.get("llm.chunk_lines", 220))
    categories = plugin.llm_categories()
    template = _prompt("review.txt")
    max_tokens = int(CFG.get("llm.max_tokens_review", 2048))

    tasks = []
    for start in range(0, len(lines), chunk_size):
        chunk = lines[start:start + chunk_size]
        prompt = template.format(
            language=plugin.display,
            categories=", ".join(categories),
            extra_rules=plugin.llm_extra_rules() or "None.",
            start_line=start + 1,
            end_line=start + len(chunk),
            filename=filename or "snippet",
            code_chunk=_numbered(chunk, start + 1),
        )
        tasks.append(CLIENT.chat_json(prompt, REVIEW_SCHEMA, max_tokens))
    results = await asyncio.gather(*tasks)

    raw: list[dict] = []
    for res in results:
        if isinstance(res, dict):
            raw.extend(v for v in res.get("violations", []) if isinstance(v, dict))
    stats["llm_raw_findings"] = len(raw)

    anchored: list[Violation] = []
    rejected_anchor = 0
    rejected_category = 0
    for i, v in enumerate(raw, 1):
        cat = str(v.get("category", "")).strip()
        if cat not in categories:
            rejected_category += 1
            continue
        true_line = _anchor(int(v.get("line", 0) or 0), str(v.get("snippet", "")), lines)
        if true_line is None:
            rejected_anchor += 1
            continue
        anchored.append(Violation(
            id=f"llm-{i}", layer=Layer.LLM, rule=cat,
            severity=_SEV.get(str(v.get("severity", "")).lower(), Severity.MEDIUM),
            line=true_line,
            snippet=lines[true_line - 1].strip(),
            message=str(v.get("message", "")).strip(),
            verified=False,
        ))
    stats["llm_rejected_bad_anchor"] = rejected_anchor
    stats["llm_rejected_bad_category"] = rejected_category

    # AUTOSAR/MISRA idiom: `(void)Rte_Read(...)` explicitly discards the return
    # ON PURPOSE. Drop "ignored/unused return value" findings on such lines —
    # the (void) cast is the accepted way to say "intentional".
    if CFG.get("review.suppress_void_cast_ignored_return", True):
        def _is_void_cast_noise(v: Violation) -> bool:
            m = v.message.lower()
            about_return = ("return value" in m or "return code" in m) and \
                ("ignor" in m or "not used" in m or "not checked" in m or
                 "unused" in m or "discard" in m)
            return about_return and v.snippet.strip().startswith("(void)")
        before = len(anchored)
        anchored = [v for v in anchored if not _is_void_cast_noise(v)]
        stats["llm_rejected_void_cast"] = before - len(anchored)

    # The LLM is unreliable at magic-number detection (it flags NAMED constants
    # like TRUE / ERROR_PRESENT as "magic literals"). A deterministic scanner
    # (hardcode layer) owns numeric literals now, so drop the LLM's versions.
    if CFG.get("review.suppress_llm_magic_literal", True):
        def _is_magic_literal_noise(v: Violation) -> bool:
            m = v.message.lower()
            return ("magic" in m and
                    ("number" in m or "literal" in m or "constant" in m or
                     "value" in m))
        before = len(anchored)
        anchored = [v for v in anchored if not _is_magic_literal_noise(v)]
        stats["llm_rejected_magic_literal"] = before - len(anchored)

    # C is NOT C++. Drop LLM findings that push C++-only constructs on a C file
    # (e.g. "use static_cast", "use smart pointers / RAII") — those are wrong
    # advice for C and read as false positives. Markers are config-driven.
    if plugin.name == "c" and CFG.get("review.suppress_cpp_advice_on_c", True):
        markers = [s.lower() for s in CFG.get("review.cpp_only_markers", [
            "static_cast", "reinterpret_cast", "const_cast", "dynamic_cast",
            "smart pointer", "unique_ptr", "shared_ptr", "std::", "raii",
            "rule of five", "rule of three", "new/delete", "delete[]",
            "template", "namespace", "constexpr", "nullptr instead",
        ])]
        def _is_cpp_advice(v: Violation) -> bool:
            blob = (v.message + " " + (v.suggestion or "")).lower()
            return any(mk in blob for mk in markers)
        before = len(anchored)
        anchored = [v for v in anchored if not _is_cpp_advice(v)]
        stats["llm_rejected_cpp_advice_on_c"] = before - len(anchored)

    # bound the expensive verify+fix work on large/noisy files: keep the
    # highest-severity findings, note the rest. Prevents a 3000-line file from
    # sending hundreds of findings through per-item LLM verification.
    _sev_rank = {Severity.CRITICAL: 4, Severity.HIGH: 3, Severity.MEDIUM: 2,
                 Severity.LOW: 1, Severity.INFO: 0}
    max_findings = int(CFG.get("review.max_llm_findings", 0))
    if max_findings > 0 and len(anchored) > max_findings:
        anchored.sort(key=lambda v: _sev_rank.get(v.severity, 0), reverse=True)
        stats["llm_capped"] = len(anchored) - max_findings
        anchored = anchored[:max_findings]

    if not CFG.get("llm.verify.enabled", True):
        for v in anchored:
            v.verification_note = "verify pass disabled"
        return anchored

    verified = await asyncio.gather(
        *(_verify(v, lines, plugin.display) for v in anchored))
    kept = [v for v in verified if v is not None]
    stats["llm_rejected_by_verifier"] = len(anchored) - len(kept)
    return kept


async def _verify(v: Violation, lines: list[str], language: str) -> Violation | None:
    ctx_n = int(CFG.get("review.context_lines_for_verify", 12))
    lo = max(0, v.line - 1 - ctx_n)
    hi = min(len(lines), v.line + ctx_n)
    prompt = _prompt("verify.txt").format(
        language=language, line=v.line, snippet=v.snippet, category=v.rule,
        message=v.message, context=_numbered(lines[lo:hi], lo + 1),
    )
    res = await CLIENT.chat_json(
        prompt, VERIFY_SCHEMA, int(CFG.get("llm.max_tokens_verify", 512))
    )
    if not res or not res.get("confirmed"):
        return None
    v.verified = True
    v.verification_note = str(res.get("reason", "confirmed by adversarial verifier"))
    return v
