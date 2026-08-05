"""Pipeline accuracy + consistency benchmark against a LIVE server.

    python tests/benchmark.py                 # built-in labeled corpus
    python tests/benchmark.py /path/to/dir    # + your own real files (unlabeled)

Reports, per language and overall:
  - FALSE POSITIVES on clean files (must be ~0 — the headline number)
  - RECALL on planted bugs in the *_bad / *_memory_bugs files
  - CONSISTENCY: same file reviewed N times, variance in finding count
  - timing per file

Env: KOOSYS_URL (default http://127.0.0.1:8000), BENCH_REPEAT (default 3),
KOOSYS_TOKEN (if auth.mode=token), BENCH_TIMEOUT (default 900).
Exit code 0 if no false positives on clean files, else 1.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
REPEAT = int(os.environ.get("BENCH_REPEAT", "3"))
TIMEOUT = int(os.environ.get("BENCH_TIMEOUT", "900"))
ROOT = Path(__file__).resolve().parent.parent

_EXT_LANG = {".py": "python", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
             ".hpp": "cpp", ".java": "java", ".ts": "typescript", ".js": "typescript"}

# built-in labeled corpus: (path, language, expect) where expect is
# "clean" (0 violations wanted) or "bad" (>=1 wanted)
CORPUS = [
    ("tests/lang/clean.c", "c", "clean"),
    ("tests/lang/clean.cpp", "cpp", "clean"),
    ("tests/lang/Clean.java", "java", "clean"),
    ("tests/lang/clean.ts", "typescript", "clean"),
    ("tests/stress/clean_tricky.py", "python", "clean"),
    ("tests/stress/clean_async.py", "python", "clean"),
    ("tests/stress/c_clean_tricky.c", "c", "clean"),
    ("tests/stress/cpp_clean.cpp", "cpp", "clean"),
    ("tests/lang/bad.c", "c", "bad"),
    ("tests/lang/bad.cpp", "cpp", "bad"),
    ("tests/lang/Bad.java", "java", "bad"),
    ("tests/lang/bad.ts", "typescript", "bad"),
    ("tests/stress/subtle_bugs.py", "python", "bad"),
    ("tests/stress/c_memory_bugs.c", "c", "bad"),
    ("tests/stress/cpp_memory_bugs.cpp", "cpp", "bad"),
]


def _client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    tok = os.environ.get("KOOSYS_TOKEN")
    if tok:
        c.post("/api/login", json={"username": "bench", "password": tok})
    return c


def review(c: httpx.Client, code: str, filename: str, language: str) -> dict:
    jid = c.post("/api/review",
                 json={"code": code, "filename": filename, "language": language}
                 ).json()["job_id"]
    start = time.time()
    while time.time() - start < TIMEOUT:
        time.sleep(3)
        j = c.get(f"/api/job/{jid}").json()
        if j["state"] in ("done", "error"):
            j["_elapsed"] = round(time.time() - start, 1)
            return j
    return {"state": "timeout", "violations": [], "_elapsed": TIMEOUT}


def main() -> int:
    c = _client()
    h = c.get("/api/health").json()
    print(f"target={BASE} llm={h.get('llm_available')} auth={h.get('auth_mode')}\n")

    fp_total = 0
    recall_hits = recall_total = 0
    consistency = []

    print(f"{'file':<40}{'lang':<7}{'exp':<6}{'runs(violations)':<22}{'verdict'}")
    print("-" * 90)
    items = [(ROOT / p, lang, exp) for p, lang, exp in CORPUS]

    # optional: user's own files (unlabeled — reported, not scored)
    for arg in sys.argv[1:]:
        d = Path(arg)
        files = [d] if d.is_file() else sorted(d.rglob("*"))
        for f in files:
            if f.suffix.lower() in _EXT_LANG and f.is_file():
                items.append((f, _EXT_LANG[f.suffix.lower()], "user"))

    for path, lang, exp in items:
        if not path.exists():
            continue
        code = path.read_text(encoding="utf-8", errors="replace")
        counts = []
        elapsed = []
        for _ in range(REPEAT if exp != "user" else 1):
            j = review(c, code, path.name, lang)
            counts.append(len(j.get("violations", [])))
            elapsed.append(j.get("_elapsed", 0))
        n0 = counts[0]
        verdict = ""
        if exp == "clean":
            fp = max(counts)  # worst-case FP across runs
            fp_total += fp
            verdict = "OK (0 FP)" if fp == 0 else f"** {fp} FALSE POSITIVE(S) **"
        elif exp == "bad":
            recall_total += 1
            if n0 > 0:
                recall_hits += 1
            verdict = "detected" if n0 > 0 else "** MISSED **"
        else:
            verdict = f"{n0} findings ({statistics.mean(elapsed):.0f}s)"
        if exp != "user" and len(set(counts)) > 1:
            consistency.append((path.name, counts))
        runs = ",".join(map(str, counts))
        print(f"{path.name:<40}{lang:<7}{exp:<6}{runs:<22}{verdict}")

    print("\n" + "=" * 60)
    print(f"FALSE POSITIVES on clean files : {fp_total}   (target 0)")
    if recall_total:
        print(f"RECALL on planted-bug files    : {recall_hits}/{recall_total}")
    if consistency:
        print(f"CONSISTENCY: {len(consistency)} file(s) varied across {REPEAT} runs:")
        for name, cs in consistency:
            print(f"   {name}: {cs}")
    else:
        print(f"CONSISTENCY: identical counts across {REPEAT} runs on every file")
    ok = fp_total == 0
    print("BENCHMARK:", "PASS (no false positives)" if ok else "FAIL (false positives present)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
