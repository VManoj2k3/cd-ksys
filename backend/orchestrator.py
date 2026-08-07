"""Pipeline orchestrator: runs all layers, merges, dedups, fills fixes."""
from __future__ import annotations

import asyncio

from backend.app_config import CFG
from backend.detect import generated_marker as _looks_generated
from backend.fixes.fix_engine import fill_missing_fixes
from backend.languages.base import assign_functions, plugin_by_language, plugin_for
from backend.layers.llm_review import run_llm_review
from backend.layers.spell import run_spell_layer
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


_SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _merge_same_line_llm(violations: list[Violation]) -> list[Violation]:
    """Collapse multiple LLM findings on the SAME line into one, so a line
    with e.g. both a null-check and an overflow issue yields a single finding
    with a combined message and ONE fix — not two conflicting fixes."""
    from backend.models import Layer

    by_line: dict[int, Violation] = {}
    order: list[int] = []
    passthrough: list[Violation] = []
    for v in violations:
        if v.layer != Layer.LLM:
            passthrough.append(v)
            continue
        if v.line not in by_line:
            by_line[v.line] = v
            order.append(v.line)
        else:
            base = by_line[v.line]
            extra = v.message.strip().rstrip(".")
            if extra.lower() not in base.message.lower():
                base.message = base.message.rstrip(".") + ". Also: " + v.message
            if base.verification_note and v.verification_note:
                base.verification_note = base.verification_note.rstrip(".")
            if _SEV_RANK.get(v.severity.value, 0) > _SEV_RANK.get(base.severity.value, 0):
                base.severity = v.severity
    return passthrough + [by_line[ln] for ln in order]


def _dedup_same_finding(violations: list[Violation],
                        collapse_lint_repeats: bool) -> list[Violation]:
    """Collapse identical findings reported at multiple use-sites — e.g.
    cppcheck reports 'Uninitialized variable: total' at every use. Keep the
    earliest line for each (rule, message).

    Only LINT-layer findings are collapsed across lines, and only when the
    language's linters actually repeat one logical defect at every use-site
    (cppcheck/clang-tidy — their messages carry the identifier, so same
    message = same defect). Linters like ruff report one finding per
    OCCURRENCE with identical message text ('comparison to None' on three
    lines is three distinct, separately-fixable violations), so for those
    languages the collapse is off (config: <lang>.collapse_repeated_lint).
    Spell, security and hardcode always report distinct per-line instances
    and pass through untouched."""
    from backend.models import Layer

    if not collapse_lint_repeats:
        return violations
    seen: dict[tuple, Violation] = {}
    passthrough: list[Violation] = []
    for v in sorted(violations, key=lambda x: x.line):
        if v.layer != Layer.LINT:
            passthrough.append(v)
            continue
        key = (v.rule, v.message)
        if key not in seen:
            seen[key] = v
    return passthrough + list(seen.values())


_SPELLING_WORDS = ("misspell", "misspelled", "spelling", "spelled", "typo")
_CONFIRM_NOTE = " (independently confirmed by LLM review)"


def _confirm(d: Violation) -> None:
    if _CONFIRM_NOTE.strip() not in (d.verification_note or ""):
        d.verification_note = (d.verification_note or "") + _CONFIRM_NOTE


def _dedup(deterministic: list[Violation], llm: list[Violation]) -> list[Violation]:
    """Drop LLM findings that duplicate a deterministic finding nearby.

    On real GPU runs the LLM independently re-finds most defects that
    cppcheck/clang-tidy/ruff already reported (use-after-free, double free,
    leaks...) — showing both is the same bug twice. Any LLM finding within
    the window of a LINT or SECURITY finding is folded into it as an
    "independently confirmed" note. Hardcode (magic numbers) and spell
    findings never absorb an LLM finding — a real bug can share a line with
    a numeric literal. Config: review.dedup_llm_vs_deterministic (on),
    review.dedup_window_lines."""
    window = int(CFG.get("review.dedup_window_lines", _DEDUP_WINDOW))
    fold_enabled = bool(CFG.get("review.dedup_llm_vs_deterministic", True))
    kept = []
    for v in llm:
        # spelling is the spell layer's job — an LLM 'finding' about a typo is
        # either a duplicate (dict-confirmed) or unreliable (not in dict)
        if any(w in v.message.lower() for w in _SPELLING_WORDS):
            spell_dup = next(
                (d for d in deterministic if d.layer == Layer.SPELL
                 and abs(d.line - v.line) <= window),
                None,
            )
            if spell_dup is not None:
                _confirm(spell_dup)
            continue  # drop either way
        if fold_enabled:
            dup = next(
                (d for d in deterministic
                 if d.layer in (Layer.LINT, Layer.SECURITY)
                 and abs(d.line - v.line) <= window),
                None,
            )
            if dup is not None:
                _confirm(dup)
                continue
        kept.append(v)
    return kept


async def run_review(job: ReviewJob) -> None:
    code, filename = job.code, job.filename
    job.state = "running"
    job.layers = _make_layers()
    stats: dict = {}

    # explicit UI selection wins over filename inference (a C file pasted
    # under a .py name must still review as C)
    plugin = plugin_by_language(job.requested_language) or plugin_for(filename)
    if plugin is None:
        job.state = "error"
        job.error = (f"Could not determine language for '{filename}'. "
                     f"Pick a language from the dropdown.")
        return
    job.language = plugin.display

    marker = _looks_generated(code)
    if marker:
        job.notice = (
            f"This looks like a machine-generated file (marker: \"{marker}\"). "
            f"Findings on generated/template code are often noise — review the "
            f"hand-written portions instead.")

    async def run_sync(fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ---- deterministic layers (run concurrently in threads) ----
    async def do_spell():
        st = _layer(job, "spell")
        if not CFG.get("spell.enabled", True):
            st.state = "skipped"
            return []
        st.state = "running"
        out = await run_sync(run_spell_layer, code, plugin)
        st.found, st.state = len(out), "done"
        return out

    async def do_lint():
        st = _layer(job, "lint")
        st.state = "running"
        try:
            out = await run_sync(plugin.lint, code, filename)
        except Exception as exc:  # noqa: BLE001 — tool failure must not kill job
            st.state, st.detail = "error", str(exc)[:200]
            return []
        st.found, st.state = len(out), "done"
        return out

    async def do_security():
        st = _layer(job, "security")
        st.state = "running"
        try:
            out = await run_sync(plugin.security, code, filename)
        except Exception as exc:  # noqa: BLE001
            st.state, st.detail = "error", str(exc)[:200]
            return []
        st.found, st.state = len(out), "done"
        return out

    async def do_hardcode():
        st = _layer(job, "hardcode")
        st.state = "running"
        try:
            out = await run_sync(plugin.hardcode, code, filename)
        except Exception as exc:  # noqa: BLE001 — never kill the job
            st.state, st.detail = "error", str(exc)[:200]
            return []
        if not out and (plugin.name == "python"
                        and not CFG.get("hardcode.enabled", False)):
            st.state, st.detail = "skipped", "disabled in config"
            return []
        st.found, st.state = len(out), "done"
        return out

    spell_v, lint_v, sec_v, hard_v = await asyncio.gather(
        do_spell(), do_lint(), do_security(), do_hardcode()
    )
    deterministic = _dedup_same_finding(
        [*spell_v, *lint_v, *sec_v, *hard_v],
        bool(CFG.get(f"{plugin.name}.collapse_repeated_lint", False)))

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
            llm_v = await run_llm_review(code, filename, stats, plugin)
            llm_v = _dedup(deterministic, llm_v)
            llm_v = _merge_same_line_llm(llm_v)
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

    # tag each violation with its enclosing function so the UI can group them
    try:
        assign_functions(violations, plugin.enclosing_functions(code))
    except Exception:  # noqa: BLE001 — grouping is cosmetic, never fail the job
        pass

    # attach findings NOW, before the expensive fix stage — so a slow/timed-out
    # fix stage can never throw away the findings the user already earned.
    job.violations = violations
    job.stats = stats

    # ---- fixes for anything still missing one (bounded, best-effort) ----
    fx = _layer(job, "fixes")
    fx.state = "running"
    fix_budget = int(CFG.get("review.fix_budget_seconds", 300))
    try:
        await asyncio.wait_for(
            fill_missing_fixes(violations, code, filename, job.llm_available, plugin),
            timeout=fix_budget)
        fx.state = "done"
        fx.found = sum(1 for v in violations if v.fix is not None)
        fx.detail = f"{fx.found}/{len(violations)} violations have validated fixes"
    except asyncio.TimeoutError:
        fx.state = "partial"
        fx.found = sum(1 for v in violations if v.fix is not None)
        fx.detail = (f"fix generation exceeded {fix_budget}s — showing all findings; "
                     f"{fx.found} have fixes, the rest need manual changes")
    except Exception as exc:  # noqa: BLE001
        fx.state = "error"
        fx.detail = str(exc)

    job.state = "done"
