"""GUIDELINE layer (Phase 2) — RAG review against uploaded coding guidelines.

For each chunk of the submitted code: retrieve the most relevant rules from
the user's selected collection(s), ask the LLM to flag ONLY clear violations
of those specific rules (each citing one rule), then apply the same
anti-false-positive gates as Phase 1 — line-anchor validation and an
adversarial verify pass — before anything is shown. Every surviving finding
carries a Citation back to the exact rule text + source document.
"""
from __future__ import annotations

import asyncio

from backend.app_config import CFG
from backend.layers.llm_review import _SEV, _anchor, _numbered, _prompt
from backend.llm.client import CLIENT
from backend.models import Citation, Layer, Severity, Violation
from backend.rag import store
from backend.rag.embedder import get_embedder

GUIDELINE_SCHEMA = {
    "type": "object",
    "properties": {
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "snippet": {"type": "string"},
                    "rule_index": {"type": "integer"},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["line", "snippet", "rule_index", "severity", "message"],
            },
        }
    },
    "required": ["violations"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["confirmed", "reason"],
}


def _format_rules(rules: list[dict], max_chars: int) -> str:
    out = []
    for i, r in enumerate(rules, 1):
        src = r.get("source") or "guideline"
        page = f" p{r['page']}" if r.get("page") else ""
        text = " ".join((r.get("text") or "").split())[:max_chars]
        out.append(f"[R{i}] (from {src}{page}) {text}")
    return "\n".join(out)


async def run_guideline_review(code: str, filename: str, stats: dict, plugin,
                               user: str, collection_ids: list[str]) -> list[Violation]:
    if not collection_ids:
        return []
    lines = code.splitlines()
    if not lines:
        return []
    chunk_size = int(CFG.get("rag.review.chunk_lines",
                             CFG.get("llm.chunk_lines", 220)))
    top_k = int(CFG.get("rag.review.rules_per_chunk", 6))
    rule_chars = int(CFG.get("rag.review.rule_chars", 500))
    max_tokens = int(CFG.get("llm.max_tokens_review", 2048))
    template = _prompt("guideline.txt")

    # embed all code chunks once (sync embedder -> thread), then retrieve+judge
    chunk_spans = [(s, lines[s:s + chunk_size])
                   for s in range(0, len(lines), chunk_size)]
    embedder = get_embedder()
    try:
        chunk_vecs = await asyncio.to_thread(
            embedder.embed, ["\n".join(c) for _, c in chunk_spans])
    except Exception as exc:  # noqa: BLE001 — embedding backend down: skip layer
        stats["guideline_error"] = f"embedding unavailable: {str(exc)[:80]}"
        return []

    async def judge(start: int, chunk: list[str], qvec: list[float]):
        rules = await asyncio.to_thread(
            store.search, user, collection_ids, qvec, top_k)
        if not rules:
            return []
        prompt = template.format(
            language=plugin.display, rules=_format_rules(rules, rule_chars),
            start_line=start + 1, end_line=start + len(chunk),
            filename=filename or "snippet",
            code_chunk=_numbered(chunk, start + 1))
        res = await CLIENT.chat_json(prompt, GUIDELINE_SCHEMA, max_tokens)
        found = []
        if isinstance(res, dict):
            for v in res.get("violations", []):
                if isinstance(v, dict):
                    found.append((v, rules))
        return found

    results = await asyncio.gather(
        *(judge(s, chunk, vec)
          for (s, chunk), vec in zip(chunk_spans, chunk_vecs)))

    raw = [item for sub in results for item in sub]
    stats["guideline_raw_findings"] = len(raw)

    anchored: list[Violation] = []
    rejected_anchor = 0
    seen: set[tuple[int, int]] = set()
    for i, (v, rules) in enumerate(raw, 1):
        try:
            ridx = int(v.get("rule_index", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= ridx <= len(rules)):
            continue
        true_line = _anchor(int(v.get("line", 0) or 0), str(v.get("snippet", "")), lines)
        if true_line is None:
            rejected_anchor += 1
            continue
        key = (true_line, ridx)
        if key in seen:
            continue
        seen.add(key)
        rule = rules[ridx - 1]
        anchored.append(Violation(
            id=f"guideline-{i}", layer=Layer.GUIDELINE, rule="guideline",
            severity=_SEV.get(str(v.get("severity", "")).lower(), Severity.MEDIUM),
            line=true_line, snippet=lines[true_line - 1].strip(),
            message=str(v.get("message", "")).strip(),
            verified=False,
            citation=Citation(
                collection=str(rule.get("collection", "")),
                collection_name=str(rule.get("collection_name") or ""),
                source=str(rule.get("source") or ""),
                page=rule.get("page"),
                quote=" ".join((rule.get("text") or "").split())[:300],
            ),
        ))
    stats["guideline_rejected_bad_anchor"] = rejected_anchor

    if not CFG.get("rag.review.verify_enabled", CFG.get("llm.verify.enabled", True)):
        for v in anchored:
            v.verification_note = "verify pass disabled"
        return anchored

    verified = await asyncio.gather(
        *(_verify(v, lines, plugin.display) for v in anchored))
    kept = [v for v in verified if v is not None]
    stats["guideline_rejected_by_verifier"] = len(anchored) - len(kept)
    return kept


async def _verify(v: Violation, lines: list[str], language: str) -> Violation | None:
    ctx_n = int(CFG.get("review.context_lines_for_verify", 12))
    lo = max(0, v.line - 1 - ctx_n)
    hi = min(len(lines), v.line + ctx_n)
    prompt = _prompt("guideline_verify.txt").format(
        language=language, rule=(v.citation.quote if v.citation else ""),
        line=v.line, snippet=v.snippet, message=v.message,
        context=_numbered(lines[lo:hi], lo + 1))
    res = await CLIENT.chat_json(
        prompt, VERIFY_SCHEMA, int(CFG.get("llm.max_tokens_verify", 512)))
    if not res or not res.get("confirmed"):
        return None
    v.verified = True
    v.verification_note = str(res.get("reason", "confirmed against the cited rule"))
    return v
