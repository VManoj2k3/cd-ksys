"""Pipeline orchestrator: runs all layers, merges, dedups, fills fixes."""
from __future__ import annotations

import asyncio

from backend.app_config import CFG
from backend.fixes.fix_engine import fill_missing_fixes
from backend.layers.hardcode import run_hardcode_layer
from backend.layers.llm_review import run_llm_review
from backend.layers.spell import run_spell_layer
from backend.layers.static_lint import run_bandit, run_ruff
from backend.llm.client import CLIENT
from backend.models import Layer, LayerStatus, ReviewJob, Violation

_DEDUP_WINDOW = 1  # LLM finding within this many lines of a deterministic one


def _make_layers() -> list[LayerStatus]:
    return [
        LayerStatus(name="spell"),
        LayerStatus(name="lint"),
        LayerStatus(name="security"),
        LayerStatus(name="hardcode"),
        LayerStatus(name="llm_review"),
        LayerStatus(name="fixes"),
    ]


def _layer(job: ReviewJob, name: str) -> LayerStatus:
    return next(s for s in job.layers if s.name == name)


_SPELLING_WORDS = ("misspell", "misspelled", "spelling", "spelled", "typo")


def _dedup(deterministic: list[Violation], llm: list[Violation]) -> list[Violation]:
    """Drop LLM findings that duplicate a deterministic finding nearby."""
    kept = []
    for v in llm:
        # spelling is the spell layer's job — an LLM 'finding' about a typo is
        # either a duplicate (dict-confirmed) or unreliable (not in dict)
        if any(w in v.message.lower() for w in _SPELLING_WORDS):
            spell_dup = next(
                (d for d in deterministic if d.layer == Layer.SPELL
                 and abs(d.line - v.line) <= _DEDUP_WINDOW),
                None,
            )
            if spell_dup is not None:
                spell_dup.verification_note = (
                    spell_dup.verification_note or ""
                ) + " (independently confirmed by LLM review)"
            continue  # drop either way
        dup = next(
            (d for d in deterministic if abs(d.line - v.line) <= _DEDUP_WINDOW
             and (d.layer == Layer.SECURITY) == (v.rule == "security")),
            None,
        )
        if dup is not None and v.rule in ("security", "error_handling"):
            dup.verification_note = (
                dup.verification_note or ""
            ) + " (independently confirmed by LLM review)"
            continue
        kept.append(v)
    return kept


async def run_review(job: ReviewJob) -> None:
    code, filename = job.code, job.filename
    job.state = "running"
    job.layers = _make_layers()
    stats: dict = {}

    async def run_sync(fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ---- deterministic layers (run concurrently in threads) ----
    async def do_spell():
        st = _layer(job, "spell")
        if not CFG.get("spell.enabled", True):
            st.state = "skipped"
            return []
        st.state = "running"
        out = await run_sync(run_spell_layer, code)
        st.found, st.state = len(out), "done"
        return out

    async def do_ruff():
        st = _layer(job, "lint")
        if not CFG.get("lint.ruff.enabled", True):
            st.state = "skipped"
            return []
        st.state = "running"
        out = await run_sync(run_ruff, code, filename)
        st.found, st.state = len(out), "done"
        return out

    async def do_bandit():
        st = _layer(job, "security")
        if not CFG.get("lint.bandit.enabled", True):
            st.state = "skipped"
            return []
        st.state = "running"
        out = await run_sync(run_bandit, code, filename)
        st.found, st.state = len(out), "done"
        return out

    async def do_hardcode():
        st = _layer(job, "hardcode")
        if not CFG.get("hardcode.enabled", False):
            st.state = "skipped"
            st.detail = "disabled in config"
            return []
        st.state = "running"
        out = await run_sync(run_hardcode_layer, code)
        st.found, st.state = len(out), "done"
        return out

    spell_v, ruff_v, bandit_v, hard_v = await asyncio.gather(
        do_spell(), do_ruff(), do_bandit(), do_hardcode()
    )
    deterministic = [*spell_v, *ruff_v, *bandit_v, *hard_v]

    # ---- LLM layers ----
    llm_st = _layer(job, "llm_review")
    job.llm_available = await CLIENT.health()
    llm_v: list[Violation] = []
    if not job.llm_available:
        llm_st.state = "skipped"
        llm_st.detail = "llama-server unreachable — deterministic layers only"
    else:
        llm_st.state = "running"
        try:
            llm_v = await run_llm_review(code, filename, stats)
            llm_v = _dedup(deterministic, llm_v)
            llm_st.found, llm_st.state = len(llm_v), "done"
            llm_st.detail = (
                f"raw {stats.get('llm_raw_findings', 0)} → "
                f"anchored -{stats.get('llm_rejected_bad_anchor', 0)} "
                f"category -{stats.get('llm_rejected_bad_category', 0)} "
                f"verifier -{stats.get('llm_rejected_by_verifier', 0)}"
            )
        except Exception as exc:  # noqa: BLE001 — layer failure must not kill the job
            llm_st.state = "error"
            llm_st.detail = str(exc)

    violations = sorted([*deterministic, *llm_v], key=lambda v: (v.line, v.layer.value))

    # ---- fixes for anything still missing one ----
    fx = _layer(job, "fixes")
    fx.state = "running"
    try:
        await fill_missing_fixes(violations, code, filename, job.llm_available)
        fx.state = "done"
        fx.found = sum(1 for v in violations if v.fix is not None)
        fx.detail = f"{fx.found}/{len(violations)} violations have validated fixes"
    except Exception as exc:  # noqa: BLE001
        fx.state = "error"
        fx.detail = str(exc)

    job.violations = violations
    job.stats = stats
    job.state = "done"
