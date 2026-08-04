"""Stress test — run against a LIVE server (local mock or Kaggle T4 stack).

    python -m tests.stress_test

Env overrides: KOOSYS_URL (default http://127.0.0.1:8000),
STRESS_TIMEOUT seconds per job (default 900), STRESS_CONCURRENCY (default 3).

Hard failures (exit 1): crashes, API misbehavior, deterministic false
positives on clean files, missed planted deterministic violations, wrong
line numbers in long-file chunking test.
Soft report: LLM recall on planted semantic bugs, LLM findings on clean
files (candidate FPs), per-file timing.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = int(os.environ.get("STRESS_TIMEOUT", "900"))
CONCURRENCY = int(os.environ.get("STRESS_CONCURRENCY", "3"))
STRESS_DIR = Path(__file__).parent / "stress"

HARD_FAILS: list[str] = []
SOFT_NOTES: list[str] = []


def fail(msg: str) -> None:
    HARD_FAILS.append(msg)
    print(f"  HARD FAIL  {msg}")


def note(msg: str) -> None:
    SOFT_NOTES.append(msg)
    print(f"  note       {msg}")


def submit_and_wait(code: str, filename: str) -> dict:
    r = httpx.post(f"{BASE}/api/review", json={"code": code, "filename": filename},
                   timeout=60)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    start = time.time()
    while time.time() - start < TIMEOUT:
        time.sleep(3)
        j = httpx.get(f"{BASE}/api/job/{job_id}", timeout=60).json()
        if j["state"] in ("done", "error"):
            j["_elapsed"] = round(time.time() - start, 1)
            return j
    raise TimeoutError(f"{filename} did not finish in {TIMEOUT}s")


def rules(job: dict, layer: str | None = None) -> list[tuple[str, int]]:
    return [(v["rule"], v["line"]) for v in job["violations"]
            if layer is None or v["layer"] == layer]


def det_violations(job: dict) -> list[dict]:
    return [v for v in job["violations"] if v["layer"] != "llm"]


def llm_violations(job: dict) -> list[dict]:
    return [v for v in job["violations"] if v["layer"] == "llm"]


def show(job: dict, name: str) -> None:
    counts: dict[str, int] = {}
    for v in job["violations"]:
        counts[v["layer"]] = counts.get(v["layer"], 0) + 1
    print(f"  {name}: state={job['state']} time={job.get('_elapsed')}s "
          f"violations={counts} stats={job.get('stats', {})}")


# ---------------------------------------------------------------- scenarios
def test_clean_files() -> None:
    print("\n== FP-bait: tricky clean files (deterministic layers must be silent) ==")
    for fname in ("clean_tricky.py", "clean_async.py"):
        job = submit_and_wait((STRESS_DIR / fname).read_text(), fname)
        show(job, fname)
        if job["state"] != "done":
            fail(f"{fname}: state={job['state']} err={job.get('error')}")
            continue
        det = det_violations(job)
        if det:
            fail(f"{fname}: {len(det)} deterministic FALSE POSITIVES: "
                 f"{[(v['rule'], v['line'], v['message'][:60]) for v in det]}")
        for v in llm_violations(job):
            note(f"{fname}: LLM finding on clean file (candidate FP): "
                 f"L{v['line']} [{v['rule']}] {v['message'][:80]}")


def test_planted_bugs() -> None:
    print("\n== FN-bait: planted violations (must all be caught) ==")
    job = submit_and_wait((STRESS_DIR / "subtle_bugs.py").read_text(), "subtle_bugs.py")
    show(job, "subtle_bugs.py")
    got = {r for r, _ in rules(job)}
    expected_det = {
        "B006": "mutable default arg",
        "E722": "bare except",
        "B506": "unsafe yaml.load",
        "B324": "weak md5",
        "B602": "shell=True",
        "spell-identifier": "recieve_packet",
    }
    for rule, what in expected_det.items():
        if rule not in got:
            fail(f"subtle_bugs.py: MISSED planted {rule} ({what})")
        else:
            print(f"  PASS       {rule} ({what}) caught")
    leak = [v for v in llm_violations(job) if v["line"] in range(31, 34)]
    if leak:
        print(f"  PASS       LLM caught resource leak: {leak[0]['message'][:70]}")
    else:
        note("subtle_bugs.py: LLM missed the open()-without-close resource leak (recall)")
    fixed = sum(1 for v in job["violations"] if v.get("fix") and v["fix"]["validated"])
    note(f"subtle_bugs.py: {fixed}/{len(job['violations'])} violations have validated fixes")


def test_edge_cases() -> None:
    print("\n== Edge cases: syntax error, unicode ==")
    job = submit_and_wait((STRESS_DIR / "edge_syntax_error.py").read_text(),
                          "edge_syntax_error.py")
    show(job, "edge_syntax_error.py")
    if job["state"] != "done":
        fail(f"syntax-error file crashed the pipeline: {job.get('error')}")
    elif not any(r.startswith("E9") or r == "invalid-syntax"
                 for r, _ in rules(job)):
        fail(f"syntax-error file: no syntax-error violation reported (got {rules(job)})")
    else:
        print("  PASS       syntax error reported, no crash")

    job = submit_and_wait((STRESS_DIR / "edge_unicode.py").read_text(), "edge_unicode.py")
    show(job, "edge_unicode.py")
    if job["state"] != "done":
        fail(f"unicode file crashed the pipeline: {job.get('error')}")
    elif not any("recieve" in v["message"] for v in job["violations"]):
        fail("unicode file: planted 'recieve' in comment not found")
    else:
        print("  PASS       unicode handled; planted misspelling found")


def test_long_file_chunking() -> None:
    print("\n== Long file: 1200 lines, planted at known lines (chunking integrity) ==")
    lines: list[str] = ['"""Generated long module."""']
    planted_e711: list[int] = []
    for i in range(120):
        lines.append(f"def func_{i}(val_{i}):")
        if i in (10, 55, 110):  # spread across llm chunks (220 lines each)
            lines.append(f"    if val_{i} == None:")
            planted_e711.append(len(lines))
            lines.append("        return 0")
        else:
            lines.append(f"    if val_{i} is None:")
            lines.append("        return 0")
        for j in range(6):
            lines.append(f"    val_{i} = val_{i} + {j} if False else val_{i}")
        lines.append(f"    return val_{i}")
        lines.append("")
    code = "\n".join(lines) + "\n"
    print(f"  generated {len(lines)} lines; planted E711 at lines {planted_e711}")
    job = submit_and_wait(code, "edge_long.py")
    show(job, "edge_long.py")
    got_e711 = sorted(line for r, line in rules(job) if r == "E711")
    if got_e711 != sorted(planted_e711):
        fail(f"long file: E711 lines mismatch — planted {planted_e711}, got {got_e711}")
    else:
        print(f"  PASS       all planted E711 found at exact lines {got_e711}")


def test_api_robustness() -> None:
    print("\n== API robustness: empty, oversize, huge single line ==")
    r = httpx.post(f"{BASE}/api/review", json={"code": "", "filename": "e.py"}, timeout=30)
    print(f"  empty code -> HTTP {r.status_code}")
    if r.status_code != 400:
        fail(f"empty code should be 400, got {r.status_code}")

    big = "x = 1\n" * 120_000  # ~720 KB > 512 KB limit
    r = httpx.post(f"{BASE}/api/review", json={"code": big, "filename": "big.py"}, timeout=60)
    print(f"  oversize file -> HTTP {r.status_code}")
    if r.status_code != 413:
        fail(f"oversize should be 413, got {r.status_code}")

    huge_line = "VALUES = [" + ", ".join(str(i) for i in range(8000)) + "]\n"
    job = submit_and_wait(huge_line, "huge_line.py")
    show(job, "huge_line.py")
    if job["state"] != "done":
        fail(f"huge single line crashed: {job.get('error')}")
    else:
        print("  PASS       huge single line handled")


def test_concurrency() -> None:
    print(f"\n== Concurrency: {CONCURRENCY} simultaneous reviews ==")
    code = (STRESS_DIR / "subtle_bugs.py").read_text()
    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(submit_and_wait, code, f"concurrent_{i}.py")
                   for i in range(CONCURRENCY)]
        jobs = [f.result() for f in futures]
    wall = round(time.time() - start, 1)
    states = [j["state"] for j in jobs]
    counts = [len(j["violations"]) for j in jobs]
    print(f"  states={states} violation_counts={counts} wall={wall}s "
          f"(vs ~{sum(j['_elapsed'] for j in jobs)}s serial)")
    if any(s != "done" for s in states):
        fail(f"concurrency: some jobs failed: {states}")
    elif len(set(counts)) != 1:
        fail(f"concurrency: identical inputs produced different violation counts: {counts}")
    else:
        print("  PASS       all concurrent jobs done with identical results")


def main() -> int:
    health = httpx.get(f"{BASE}/api/health", timeout=30).json()
    print(f"target={BASE} llm_available={health['llm_available']}")
    if not health["llm_available"]:
        note("LLM offline — stress test covers deterministic layers + API only")

    test_clean_files()
    test_planted_bugs()
    test_edge_cases()
    test_long_file_chunking()
    test_api_robustness()
    test_concurrency()

    print(f"\n{'=' * 60}")
    print(f"HARD FAILURES: {len(HARD_FAILS)}")
    for m in HARD_FAILS:
        print(f"  - {m}")
    print(f"SOFT NOTES: {len(SOFT_NOTES)}")
    for m in SOFT_NOTES:
        print(f"  - {m}")
    print("STRESS TEST: " + ("PASSED" if not HARD_FAILS else "FAILED"))
    return 1 if HARD_FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
