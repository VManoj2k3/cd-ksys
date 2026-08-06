"""LLM pipeline accuracy suite — proves every anti-FP / fix-validation gate.

    python -m tests.test_llm_pipeline

Runs the REAL orchestrator + fix engine against a scripted fake llama-server
(tests/fake_llm.py) that emulates an adversarial model: hallucinated quotes,
disallowed categories, malformed JSON, no-op / destructive / syntax-breaking
fixes, fixes that smuggle in new violations. The pipeline must reject all of
that and keep only the planted real findings with validated fixes.

No GPU needed; the model's RAW quality is measured separately on the GPU
stack with tests/accuracy_eval.py — this suite proves the machinery that
turns raw model output into precise final output.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TMPDIR = tempfile.mkdtemp(prefix="koosys-llmtest-")
_PORT = 8931

_OVERLAY = f"""
logging:
  level: warning
llm:
  base_url: http://127.0.0.1:{_PORT}/v1
  mock: false
  timeout_seconds: 30
  max_parallel_requests: 4
  health_cache_seconds: 1
  fix:
    max_retries: 1
audit:
  enabled: false
"""
_overlay_path = Path(_TMPDIR) / "overlay.yaml"
_overlay_path.write_text(_OVERLAY, encoding="utf-8")
os.environ["KOOSYS_CONFIG"] = str(_REPO / "config.yaml")
os.environ["KOOSYS_CONFIG_OVERLAY"] = str(_overlay_path)

from backend.app_config import CFG  # noqa: E402
from backend.fixes.fix_engine import (_fix_deletes_logic, _is_noop,  # noqa: E402
                                      _map_to_original, generate_fix)
from backend.layers.llm_review import _anchor  # noqa: E402
from backend.llm.client import CLIENT  # noqa: E402
from backend.models import Layer, ReviewJob, Severity, Violation  # noqa: E402
from backend.orchestrator import run_review  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'PASS ' if ok else 'FAIL '} {label}")
    if not ok:
        FAILURES.append(label)


_LOOP = asyncio.new_event_loop()  # one loop for the whole suite (as in prod)


def run_async(coro):
    return _LOOP.run_until_complete(coro)


def review(code: str, filename: str = "sample.py") -> ReviewJob:
    job = ReviewJob(job_id="t", filename=filename, code=code,
                    requested_language="py")
    run_async(run_review(job))
    assert job.state == "done", f"job errored: {job.error}"
    return job


def llm_violations(job: ReviewJob) -> list:
    return [v for v in job.violations if v.layer == Layer.LLM]


INERT_FIX = {"start_line": 0, "end_line": 0, "replacement": ""}

# a real semantic bug no deterministic layer flags: division by zero on empty
# input at line 5
BUGGY = """def average(values):
    total = 0
    for v in values:
        total += v
    return total / len(values)
"""
BUG_LINE = 5
BUG_SNIPPET = "return total / len(values)"
GOOD_FIX = "    return total / len(values) if values else 0.0"


def finding(line: int = BUG_LINE, snippet: str = BUG_SNIPPET,
            category: str = "logic_bug", severity: str = "high",
            message: str = "Division by zero when 'values' is empty.") -> dict:
    return {"line": line, "snippet": snippet, "category": category,
            "severity": severity, "message": message}


# ---------------------------------------------------------------- unit gates
print("== unit: helper gates ==")
check(_is_noop(["    x = 1"], "    x = 1"), "no-op detected (identical)")
check(_is_noop(["x = 1  "], "x = 1"), "no-op detected (trailing ws only)")
check(not _is_noop(["x = 1"], "x = 2"), "real change is not a no-op")
check(_fix_deletes_logic(["    return total;"], "    int total = 0;"),
      "deleted return flagged as destructive")
check(_fix_deletes_logic(["    total += v;"], "    int total;"),
      "deleted accumulation flagged as destructive")
check(not _fix_deletes_logic(["    return a / b;"],
                             "    return b != 0 ? a / b : 0;"),
      "guarded return not flagged")
check(_map_to_original(3, 5, 5, 2) == 3, "line before patch unshifted")
check(_map_to_original(8, 5, 5, 2) == 7, "line after growing patch shifts back")
check(_map_to_original(5, 5, 5, 0) == 6, "line after deletion maps forward")
check(_anchor(2, "if x == None:", ["a", "\tif x  ==  None:", "b"]) == 2,
      "anchor tolerates whitespace-only quote differences")
check(_anchor(2, "if y == None:", ["a", "\tif x  ==  None:", "b"]) is None,
      "anchor still rejects content mismatches")

# ---------------------------------------------------------------- pipeline
fake = FakeLLM(_PORT)
fake.start()
try:
    print("== S1: genuine finding survives all gates, fix validated ==")
    CLIENT._schema_mode = "json_schema"
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": GOOD_FIX},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(len(job.violations) == 1 and len(lv) == 1,
          f"exactly the planted finding survives ({len(job.violations)} total)")
    if lv:
        v = lv[0]
        check(v.line == BUG_LINE and v.verified, "line anchored + verified")
        check(v.fix is not None and v.fix.validated, "fix present and validated")
        check(v.fix is not None and "no new deterministic findings"
              in v.fix.validation_notes, "fix notes record the detector gate")
    check(fake.calls.get("verify", 0) == 1, "one adversarial verify call")
    check(fake.calls.get("fix_verify", 0) == 1, "one fix-verify call")

    print("== S2: hallucinated quote rejected by anchor validation ==")
    fake.reset(
        review=lambda p: {"violations": [
            finding(line=3, snippet="foo_bar_nonexistent()",
                    message="Calls an undefined function.")]},
        fix=lambda p: INERT_FIX,
    )
    job = review(BUGGY)
    check(len(llm_violations(job)) == 0, "hallucination dropped")
    check(job.stats.get("llm_rejected_bad_anchor") == 1,
          "rejection counted in stats")
    check(fake.calls.get("verify", 0) == 0, "no verify wasted on it")

    print("== S3: disallowed category rejected ==")
    fake.reset(
        review=lambda p: {"violations": [
            finding(category="style_preference",
                    message="I prefer f-strings here.")]},
        fix=lambda p: INERT_FIX,
    )
    job = review(BUGGY)
    check(len(llm_violations(job)) == 0, "off-list category dropped")
    check(job.stats.get("llm_rejected_bad_category") == 1,
          "rejection counted in stats")

    print("== S4: adversarial verifier kills unconfirmed finding ==")
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        verify=lambda p: {"confirmed": False,
                          "reason": "len(values) is checked by every caller"},
        fix=lambda p: INERT_FIX,
    )
    job = review(BUGGY)
    check(len(llm_violations(job)) == 0, "unconfirmed finding dropped")
    check(job.stats.get("llm_rejected_by_verifier") == 1,
          "verifier rejection counted")

    print("== S5: clean file + plausible hallucination -> zero FPs shown ==")
    clean = (_REPO / "tests" / "stress" / "clean_tricky.py").read_text("utf-8")
    clean_lines = clean.splitlines()
    quote_line = next(i + 1 for i, ln in enumerate(clean_lines)
                      if len(ln.strip()) > 20)
    fake.reset(
        review=lambda p: {"violations": [
            finding(line=quote_line, snippet=clean_lines[quote_line - 1].strip(),
                    message="This may raise at runtime.")]},
        verify=lambda p: {"confirmed": False, "reason": "code is correct"},
        fix=lambda p: INERT_FIX,
    )
    job = review(clean, "clean_tricky.py")
    check(len(llm_violations(job)) == 0,
          "anchored-but-wrong claim still killed by verifier")

    print("== S6: chunked file keeps global line numbers ==")
    CLIENT._schema_mode = "json_schema"
    big_lines = ["z_count = 4"] + [f"a_{i} = {i}" for i in range(2, 500)]
    big_lines[449] = "y = 10 / z_count"
    big = "\n".join(big_lines) + "\n"

    def chunk_review(p: str):
        m = re.search(r"lines (\d+)-(\d+) of", p)
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= 450 <= hi:
            return {"violations": [finding(
                line=450, snippet="y = 10 / z_count", category="logic_bug",
                message="Division may divide by zero if z_count is 0.")]}
        return {"violations": []}

    fake.reset(review=chunk_review, fix=lambda p: INERT_FIX)
    job = review(big, "big.py")
    lv = llm_violations(job)
    check(fake.calls.get("review", 0) == 3, "500 lines -> 3 disjoint chunks")
    check(len(lv) == 1 and lv[0].line == 450,
          f"finding at global line 450 (got {[v.line for v in lv]})")

    print("== S7: overlapping chunks dedup the same finding ==")
    CFG.raw["llm"]["chunk_overlap_lines"] = 30
    try:
        big_lines[399] = "w = 10 / z_count"
        big2 = "\n".join(big_lines) + "\n"

        def overlap_review(p: str):
            m = re.search(r"lines (\d+)-(\d+) of", p)
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= 400 <= hi:
                return {"violations": [finding(
                    line=400, snippet="w = 10 / z_count", category="logic_bug",
                    message="Possible division by zero.")]}
            return {"violations": []}

        fake.reset(review=overlap_review, fix=lambda p: INERT_FIX)
        job = review(big2, "big2.py")
        lv = llm_violations(job)
        check(fake.calls.get("review", 0) == 3,
              "overlap 30 still covers 500 lines in 3 chunks")
        check(len(lv) == 1 and lv[0].line == 400,
              "duplicate from overlapping chunks collapsed to one")
        check(job.stats.get("llm_deduped_same_line") == 1,
              "dedup counted in stats")
    finally:
        CFG.raw["llm"]["chunk_overlap_lines"] = 0

    print("== S8: two findings on one line merge into one ==")
    fake.reset(
        review=lambda p: {"violations": [
            finding(message="Division by zero when 'values' is empty."),
            finding(category="error_handling",
                    message="No guard for empty input before dividing."),
        ]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": GOOD_FIX},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(len(lv) == 1, "same-line findings merged")
    check(lv and "Also:" in lv[0].message, "merged message keeps both claims")

    print("== S9: malformed model output -> mode fallback, no crash ==")
    CLIENT._schema_mode = None
    call_count = {"n": 0}

    def flaky_review(p: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "I could not find any JSON to give you, sorry!"
        return {"violations": [finding()]}

    fake.reset(review=flaky_review,
               fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                              "replacement": GOOD_FIX})
    job = review(BUGGY)
    check(len(llm_violations(job)) == 1,
          "finding recovered via response-format fallback")
    CLIENT._schema_mode = "json_schema"

    # ------------------------------------------------------------- fix gates
    print("== S10: no-op fix rejected ==")
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": "    " + BUG_SNIPPET},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(len(lv) == 1 and lv[0].fix is None,
          "finding kept, no-op patch refused")
    check(fake.calls.get("fix", 0) == 2, "no-op retried then given up")
    check(lv and "no-op" in lv[0].fix_notes,
          f"rejection reason recorded ({lv[0].fix_notes[:60] if lv else ''})")

    print("== S11: syntax-breaking fix rejected, retry succeeds ==")
    fix_count = {"n": 0}

    def flaky_fix(p: str):
        fix_count["n"] += 1
        if fix_count["n"] == 1:
            return {"start_line": BUG_LINE, "end_line": BUG_LINE,
                    "replacement": "    return total / ((("}
        return {"start_line": BUG_LINE, "end_line": BUG_LINE,
                "replacement": GOOD_FIX}

    fake.reset(review=lambda p: {"violations": [finding()]}, fix=flaky_fix)
    job = review(BUGGY)
    lv = llm_violations(job)
    check(lv and lv[0].fix is not None and lv[0].fix.validated,
          "second attempt accepted after syntax reject")

    print("== S12: destructive fix (drops return) rejected ==")
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": "    pass"},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(lv and lv[0].fix is None, "logic-deleting patch refused")

    print("== S13: fix that introduces a NEW violation rejected ==")
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": "    import os\n" + GOOD_FIX},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(lv and lv[0].fix is None,
          "patch smuggling an unused import refused by detector gate")

    print("== S14: fix verifier rejects a bad semantic patch ==")
    fake.reset(
        review=lambda p: {"violations": [finding()]},
        fix=lambda p: {"start_line": BUG_LINE, "end_line": BUG_LINE,
                       "replacement": "    return total / max(len(values), 1)"},
        fix_verify=lambda p: {"approved": False,
                              "reason": "changes the result for empty input "
                                        "instead of guarding it"},
    )
    job = review(BUGGY)
    lv = llm_violations(job)
    check(lv and lv[0].fix is None, "fix-verify veto respected")
    check(fake.calls.get("fix_verify", 0) == 2,
          "verify ran on both attempts")

    print("== S16: LLM duplicate of a deterministic finding folds into it ==")
    E711_FILE = "def check(x):\n    if x == None:\n        return 1\n    return 2\n"
    fake.reset(
        review=lambda p: {"violations": [finding(
            line=2, snippet="if x == None:", category="logic_bug",
            message="Comparison to None with == instead of is.")]},
        fix=lambda p: INERT_FIX,
    )
    job = review(E711_FILE, "e711_dup.py")
    lint_vs = [v for v in job.violations if v.layer == Layer.LINT]
    check(len(llm_violations(job)) == 0,
          "LLM duplicate of ruff E711 not shown as second card")
    check(any("independently confirmed" in v.verification_note
              for v in lint_vs),
          "deterministic finding credited with LLM confirmation")

    print("== S15: LINT-layer fix must clear the finding (detector re-run) ==")
    E711 = "def check(x):\n    if x == None:\n        return 1\n    return 2\n"
    lint_v = Violation(id="l1", layer=Layer.LINT, rule="E711",
                       severity=Severity.LOW, line=2, snippet="if x == None:",
                       message="Comparison to None should be 'is None'")

    from backend.languages.base import plugin_by_language
    py = plugin_by_language("py")

    fake.reset(fix=lambda p: {"start_line": 2, "end_line": 2,
                              "replacement": "    if x is None:"})
    fix = run_async(generate_fix(lint_v, E711, "e711.py", py))
    check(fix is not None and fix.validated,
          "correct lint fix accepted (detector confirms gone)")

    fake.reset(fix=lambda p: {"start_line": 2, "end_line": 2,
                              "replacement": "    if (x == None):"})
    fix = run_async(generate_fix(lint_v, E711, "e711.py", py))
    check(fix is None, "non-fix rejected (detector still finds E711)")
finally:
    fake.stop()

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ALL LLM PIPELINE CHECKS PASSED")
