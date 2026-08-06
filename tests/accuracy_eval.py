"""Accuracy evaluation — precision/recall/fix-coverage against a LIVE stack.

    python -m tests.accuracy_eval

This is the Phase-1 acceptance tool: run it against the GPU stack (Kaggle T4
or on-prem) to measure what the USER experiences — how many planted bugs the
pipeline reports (recall), how much noise appears on clean files (false
positives), and how many findings ship with a validated inline fix.

Environment:
  KOOSYS_URL           stack to test (default http://127.0.0.1:8000)
  KOOSYS_EVAL_USER     login username when the stack requires auth
  KOOSYS_EVAL_PASSWORD login password/token when the stack requires auth
  EVAL_TIMEOUT         seconds per review (default 900)
  EVAL_REPORT          JSON report path (default tests/eval/last_report.json)
  EVAL_MIN_LLM_RECALL  optional 0..1 gate — fail below this LLM recall
  EVAL_MAX_LLM_FPS     optional gate — fail if LLM FPs on clean files exceed it

Exit 1 on: API failures, a missed REQUIRED deterministic finding, ANY
deterministic finding on a clean file, or a breached optional LLM gate.
LLM metrics without gates are informational (they depend on the model);
with the LLM offline (mock/dev stack), LLM expectations are reported as
skipped and only the deterministic contract is enforced.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = int(os.environ.get("EVAL_TIMEOUT", "900"))
TESTS_DIR = Path(__file__).parent
REPORT_PATH = Path(os.environ.get("EVAL_REPORT",
                                  TESTS_DIR / "eval" / "last_report.json"))

HARD_FAILS: list[str] = []


def fail(msg: str) -> None:
    HARD_FAILS.append(msg)
    print(f"  FAIL       {msg}")


class Stack:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=BASE, timeout=60)

    def login_if_needed(self) -> dict:
        health = self.client.get("/api/health").json()
        if health.get("auth_mode", "none") != "none":
            user = os.environ.get("KOOSYS_EVAL_USER", "")
            pw = os.environ.get("KOOSYS_EVAL_PASSWORD", "")
            if not user and not pw:
                print("stack requires auth: set KOOSYS_EVAL_USER / "
                      "KOOSYS_EVAL_PASSWORD")
                sys.exit(2)
            r = self.client.post("/api/login",
                                 json={"username": user, "password": pw})
            if r.status_code != 200:
                print(f"login failed: {r.status_code} {r.text}")
                sys.exit(2)
        return health

    def review(self, code: str, filename: str, language: str) -> dict:
        r = self.client.post("/api/review", json={
            "code": code, "filename": filename, "language": language})
        r.raise_for_status()
        job_id = r.json()["job_id"]
        start = time.time()
        while time.time() - start < TIMEOUT:
            time.sleep(2)
            j = self.client.get(f"/api/job/{job_id}").json()
            if j.get("state") in ("done", "error"):
                j["_elapsed"] = round(time.time() - start, 1)
                return j
        raise TimeoutError(f"{filename} did not finish within {TIMEOUT}s")


def match_expects(expects: list[dict], violations: list[dict],
                  default_tol: int) -> tuple[list[dict], list[dict], list[dict]]:
    """One-to-one matching, closest line wins. Returns
    (matched, missed, unmatched_violations); matched entries carry the
    matched violation under 'violation'.

    layer 'any' means any SUBSTANTIVE detector (lint/security/llm) — a
    hardcode magic-number or spell finding that happens to sit on the same
    line must never satisfy a planted-bug expectation (it would mask a miss)."""
    remaining = list(violations)
    matched, missed = [], []
    for exp in expects:
        tol = int(exp.get("tolerance", default_tol))
        want_layer = exp.get("layer", "any")
        candidates = []
        for v in remaining:
            if want_layer == "any":
                if v["layer"] not in ("lint", "security", "llm"):
                    continue
            elif v["layer"] != want_layer:
                continue
            delta = abs(v["line"] - int(exp["line"]))
            if delta <= tol:
                candidates.append((delta, remaining.index(v), v))
        if candidates:
            _, _, hit = min(candidates)
            remaining.remove(hit)
            matched.append({**exp, "violation": hit})
        else:
            missed.append(exp)
    return matched, missed, remaining


def main() -> None:
    manifest = yaml.safe_load(
        (TESTS_DIR / "eval" / "manifest.yaml").read_text(encoding="utf-8"))
    default_tol = int(manifest.get("defaults", {}).get("tolerance", 2))

    stack = Stack()
    health = stack.login_if_needed()
    llm_on = bool(health.get("llm_available"))
    print(f"stack: {BASE}  version={health.get('version', '?')}  "
          f"llm={'ONLINE' if llm_on else 'offline (deterministic only)'}")

    report: dict = {"stack": BASE, "llm_online": llm_on,
                    "version": health.get("version"), "files": []}
    llm_expected = llm_matched = 0
    det_required = det_found = 0
    llm_fps_clean = 0
    total_findings = total_with_fix = 0
    llm_findings = llm_with_fix = 0

    for entry in manifest["files"]:
        rel = entry["path"]
        code = (TESTS_DIR / rel).read_text(encoding="utf-8")
        name = Path(rel).name
        print(f"\n== {rel} ==")
        job = stack.review(code, name, entry.get("language", ""))
        if job.get("state") != "done":
            fail(f"{name}: review errored: {job.get('error')}")
            continue
        violations = job.get("violations", [])
        vsum = [(v["layer"], v["rule"], v["line"]) for v in violations]
        print(f"  {job['_elapsed']}s, {len(violations)} finding(s): {vsum}")

        frep: dict = {"path": rel, "seconds": job["_elapsed"],
                      "findings": len(violations)}
        total_findings += len(violations)
        total_with_fix += sum(1 for v in violations
                              if (v.get("fix") or {}).get("validated"))
        llm_vs = [v for v in violations if v["layer"] == "llm"]
        llm_findings += len(llm_vs)
        llm_with_fix += sum(1 for v in llm_vs
                            if (v.get("fix") or {}).get("validated"))

        if entry.get("clean"):
            det_fps = [v for v in violations if v["layer"] != "llm"]
            for v in det_fps:
                fail(f"{name}: deterministic FP on clean file: "
                     f"{v['layer']}/{v['rule']} line {v['line']}: {v['message'][:80]}")
            if llm_vs:
                llm_fps_clean += len(llm_vs)
                for v in llm_vs:
                    print(f"  LLM-FP     line {v['line']}: {v['message'][:90]}")
            if not violations:
                print("  PASS       clean file is silent")
            frep.update(clean=True, det_fps=len(det_fps), llm_fps=len(llm_vs))
        else:
            expects = entry.get("expect", [])
            matched, missed, unexpected = match_expects(
                expects, violations, default_tol)
            for m in matched:
                v = m["violation"]
                has_fix = bool((v.get("fix") or {}).get("validated"))
                print(f"  found      {m['name']} (line {v['line']}, "
                      f"{v['layer']}/{v['rule']}"
                      f"{', fix ✓' if has_fix else ', no fix'})")
            for x in missed:
                required = bool(x.get("required"))
                is_llm_target = x.get("layer") == "llm" or \
                    (x.get("layer") == "any" and not required)
                if required:
                    fail(f"{name}: MISSED required: {x['name']} (line {x['line']})")
                elif is_llm_target and not llm_on:
                    print(f"  skipped    {x['name']} (LLM offline)")
                else:
                    print(f"  missed     {x['name']} (line {x['line']}) — recall")
            for v in unexpected:
                print(f"  extra      {v['layer']}/{v['rule']} line {v['line']}: "
                      f"{v['message'][:80]}")
            for x in expects:
                required = bool(x.get("required"))
                if required:
                    det_required += 1
                elif llm_on:
                    llm_expected += 1
            det_found += sum(1 for m in matched if m.get("required"))
            if llm_on:
                llm_matched += sum(1 for m in matched if not m.get("required"))
            frep.update(matched=[m["name"] for m in matched],
                        missed=[x["name"] for x in missed],
                        extra=len(unexpected))
        report["files"].append(frep)

    # ------------------------------------------------------------- summary
    print("\n" + "=" * 62)
    print(f"deterministic required findings: {det_found}/{det_required}")
    fix_cov = f"{total_with_fix}/{total_findings}" if total_findings else "n/a"
    print(f"validated-fix coverage (all layers): {fix_cov}")
    summary = {"det_required": det_required, "det_found": det_found,
               "fix_coverage": [total_with_fix, total_findings]}
    if llm_on:
        recall = llm_matched / llm_expected if llm_expected else 0.0
        llm_fix = f"{llm_with_fix}/{llm_findings}" if llm_findings else "n/a"
        print(f"LLM recall on planted bugs: {llm_matched}/{llm_expected} "
              f"({recall:.0%})")
        print(f"LLM false positives on clean files: {llm_fps_clean}")
        print(f"LLM findings with validated fix: {llm_fix}")
        summary.update(llm_recall=[llm_matched, llm_expected],
                       llm_fps_clean=llm_fps_clean,
                       llm_fix_coverage=[llm_with_fix, llm_findings])
        min_recall = os.environ.get("EVAL_MIN_LLM_RECALL")
        if min_recall and llm_expected and recall < float(min_recall):
            fail(f"LLM recall {recall:.0%} below gate {min_recall}")
        max_fps = os.environ.get("EVAL_MAX_LLM_FPS")
        if max_fps and llm_fps_clean > int(max_fps):
            fail(f"LLM FPs on clean files {llm_fps_clean} above gate {max_fps}")
    else:
        print("LLM offline — semantic recall/FP not measured on this stack")
    report["summary"] = summary

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report written: {REPORT_PATH}")

    if HARD_FAILS:
        print(f"\nACCURACY EVAL: FAILED ({len(HARD_FAILS)} problem(s))")
        for h in HARD_FAILS:
            print(f"  - {h}")
        sys.exit(1)
    print("\nACCURACY EVAL: PASSED")


if __name__ == "__main__":
    main()
