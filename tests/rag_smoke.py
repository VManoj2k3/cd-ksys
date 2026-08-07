"""RAG guideline smoke test against a LIVE stack (real LLM).

    python -m tests.rag_smoke

Runs in-process on the same box as a running backend (same config/data_dir):
seeds a collection with real coding-guideline rules, then submits — through
the live HTTP API with the RAG toggle on — code that VIOLATES a rule and code
that COMPLIES, and prints the guideline findings + their rule citations.

Purpose: confirm the REAL model does guideline detection with low false
positives (clean code stays quiet) and correct citations — the Phase-2
analogue of tests/accuracy_eval.py. Env: KOOSYS_URL (default :8000).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RAG_SMOKE_USER", "anonymous")

RULES = [(1,
    "Rule 1 (Security): The eval() and exec() builtins must never be called "
    "on any externally-derived input. Use a safe parser instead.\n\n"
    "Rule 2 (Reliability): Every opened file must be closed on all paths; "
    "prefer a context manager (the 'with' statement).\n\n"
    "Rule 3 (Concurrency): Global mutable state must not be modified without "
    "a lock.\n\n"
    "Rule 4 (Style): Public functions must validate their arguments before use.")]

VIOLATING = '''def handle(request):
    cmd = request.get("cmd")
    return eval(cmd)
'''

COMPLIANT = '''def read_config(path):
    """Read and return the config file contents."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()
'''


def review(client, code, cids):
    r = client.post(f"{BASE}/api/review", json={
        "code": code, "filename": "m.py", "language": "py",
        "rag_enabled": True, "collection_ids": cids})
    r.raise_for_status()
    jid = r.json()["job_id"]
    for _ in range(300):
        time.sleep(2)
        j = client.get(f"{BASE}/api/job/{jid}").json()
        if j["state"] in ("done", "error"):
            return j
    raise TimeoutError("review did not finish")


def main() -> None:
    # seed a collection directly in the store (same box/config as the server)
    from backend.rag import ingest, store
    health = httpx.get(f"{BASE}/api/health", timeout=10).json()
    if not health.get("rag_enabled"):
        print("rag not enabled on this stack")
        sys.exit(2)
    if not health.get("llm_available"):
        print("LLM offline — guideline layer needs the model")
        sys.exit(2)

    col = store.create_collection(USER, "Smoke Guidelines")
    n = ingest.ingest_document(USER, col["id"], "team_guidelines.pdf", RULES)
    print(f"seeded collection {col['id']} with {n} chunk(s)\n")

    client = httpx.Client(timeout=60)
    print("== VIOLATING code (uses eval on request input) — expect a cited hit ==")
    job = review(client, VIOLATING, [col["id"]])
    gv = [v for v in job.get("violations", []) if v["layer"] == "guideline"]
    for v in gv:
        cite = v.get("citation") or {}
        print(f"  L{v['line']} [{v['severity']}] {v['message'][:90]}")
        print(f"     cites: {(cite.get('quote') or '')[:80]}  (from {cite.get('source')})")
    print(f"  guideline findings: {len(gv)}")

    print("\n== COMPLIANT code (docstring + with-open) — expect SILENCE ==")
    job2 = review(client, COMPLIANT, [col["id"]])
    gv2 = [v for v in job2.get("violations", []) if v["layer"] == "guideline"]
    for v in gv2:
        print(f"  FP? L{v['line']}: {v['message'][:90]}")
    print(f"  guideline findings on compliant code: {len(gv2)} (want 0)")

    store.delete_collection(USER, col["id"])
    ok = len(gv) >= 1 and len(gv2) == 0
    print("\nRAG SMOKE:", "PASS" if ok else
          "REVIEW (violation found + clean silent not both met)")


if __name__ == "__main__":
    main()
