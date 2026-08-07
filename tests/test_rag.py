"""Phase 2 (RAG) offline test — the full guideline pipeline, no GPU.

    python -m tests.test_rag

Drives the REAL orchestrator + guideline layer with the hash embedder (no
model) and the scripted fake llama-server (tests/fake_llm.py). Proves:
- collections CRUD + per-user isolation + PDF-less ingest + retrieval
- toggle is additive: OFF = pure Phase 1; ON = Phase 1 + guideline findings
- guideline findings carry a citation to the exact uploaded rule
- anti-FP gates apply: bad rule_index dropped, hallucinated quote dropped,
  adversarial verifier drops unconfirmed guideline findings
- HTTP surface: collections API auth + isolation via TestClient
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="koosys-rag-")
_PORT = 8934
_OVERLAY = f"""
logging: {{level: warning}}
server: {{host: 127.0.0.1}}
auth: {{mode: token}}
llm:
  base_url: http://127.0.0.1:{_PORT}/v1
  mock: false
  timeout_seconds: 30
  fix: {{max_retries: 0}}
audit: {{enabled: false}}
rag:
  enabled: true
  data_dir: {_TMP}/data
  embedder: {{backend: hash, hash_dim: 256}}
"""
_ov = os.path.join(_TMP, "ov.yaml")
open(_ov, "w").write(_OVERLAY)
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["KOOSYS_CONFIG"] = os.path.join(_REPO, "config.yaml")
os.environ["KOOSYS_CONFIG_OVERLAY"] = _ov
os.environ["KOOSYS_SESSION_SECRET"] = "s" * 64
os.environ["KOOSYS_SHARED_TOKEN"] = "tok"

from backend.models import Layer, ReviewJob  # noqa: E402
from backend.orchestrator import run_review  # noqa: E402
from backend.rag import ingest, store  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402

FAIL: list[str] = []


def check(ok, label):
    print(f"  {'PASS ' if ok else 'FAIL '} {label}")
    if not ok:
        FAIL.append(label)


_LOOP = asyncio.new_event_loop()


def review(code, user, rag=False, cids=None):
    job = ReviewJob(job_id="t", filename="m.py", code=code,
                    requested_language="py", user=user, rag_enabled=rag,
                    collection_ids=cids or [])
    _LOOP.run_until_complete(run_review(job))
    assert job.state == "done", job.error
    return job


# a guideline the code will violate: "no eval()"; code uses eval
GUIDE = [(1, "Rule A: The eval function must never be used on untrusted input; "
             "it is a security risk.\n\n"
             "Rule B: Every public function must have a docstring.\n\n"
             "Rule C: Prefer list comprehensions over map() for readability.")]
CODE = "def run(user_input):\n    return eval(user_input)\n"
EVAL_LINE = 2


# ---------------------------------------------------------------- store unit
print("== store: CRUD + isolation + retrieval (hash embedder) ==")
c = store.create_collection("alice", "Team Guidelines")
n = ingest.ingest_document("alice", c["id"], "guide.pdf", GUIDE)
check(n >= 1, f"ingested {n} chunk(s)")
check(len(store.list_collections("alice")) == 1, "alice has 1 collection")
check(len(store.list_collections("bob")) == 0, "bob isolated (0 collections)")
from backend.rag.embedder import get_embedder  # noqa: E402
qv = get_embedder().embed_one("eval untrusted input security")
hits = store.search("alice", [c["id"]], qv, 3)
check(bool(hits) and "eval" in hits[0]["text"].lower(),
      "retrieval finds the eval rule for an eval-ish query")
check(store.search("bob", [c["id"]], qv, 3) == [],
      "bob cannot retrieve from alice's collection")

fake = FakeLLM(_PORT)
fake.start()
try:
    def guideline_hit(p):
        return {"violations": [{"line": EVAL_LINE, "snippet": "return eval(user_input)",
                                "rule_index": 1, "severity": "high",
                                "message": "Uses eval() on user input — violates Rule A."}]}

    print("== toggle OFF: pure Phase 1 (no guideline layer) ==")
    fake.reset(review=lambda p: {"violations": []}, guideline=guideline_hit)
    job = review(CODE, "alice", rag=False)
    check(all(v.layer != Layer.GUIDELINE for v in job.violations),
          "no guideline findings when toggle off")
    check(not any(ls.name == "guideline" for ls in job.layers),
          "guideline layer not run when off")

    print("== toggle ON: additive guideline finding with citation ==")
    fake.reset(review=lambda p: {"violations": []},
               guideline=guideline_hit,
               guideline_verify=lambda p: {"confirmed": True, "reason": "eval on input"})
    job = review(CODE, "alice", rag=True, cids=[c["id"]])
    gv = [v for v in job.violations if v.layer == Layer.GUIDELINE]
    check(len(gv) == 1, f"one guideline violation ({len(gv)})")
    if gv:
        check(gv[0].line == EVAL_LINE and gv[0].verified, "anchored + verified")
        check(gv[0].citation is not None and "eval" in gv[0].citation.quote.lower(),
              "cites the eval rule text")
        check(gv[0].citation.source == "guide.pdf", "citation names the source PDF")

    print("== anti-FP: hallucinated quote dropped ==")
    fake.reset(review=lambda p: {"violations": []},
               guideline=lambda p: {"violations": [{"line": 2, "snippet": "os.system(x)",
                    "rule_index": 1, "severity": "high", "message": "x"}]})
    job = review(CODE, "alice", rag=True, cids=[c["id"]])
    check(not [v for v in job.violations if v.layer == Layer.GUIDELINE],
          "guideline finding with non-matching quote rejected by anchor")

    print("== anti-FP: bad rule_index dropped ==")
    fake.reset(review=lambda p: {"violations": []},
               guideline=lambda p: {"violations": [{"line": EVAL_LINE,
                    "snippet": "return eval(user_input)", "rule_index": 99,
                    "severity": "high", "message": "cites a rule that doesn't exist"}]})
    job = review(CODE, "alice", rag=True, cids=[c["id"]])
    check(not [v for v in job.violations if v.layer == Layer.GUIDELINE],
          "finding citing an out-of-range rule index rejected")

    print("== anti-FP: adversarial verifier drops unconfirmed ==")
    fake.reset(review=lambda p: {"violations": []}, guideline=guideline_hit,
               guideline_verify=lambda p: {"confirmed": False, "reason": "rule doesn't apply"})
    job = review(CODE, "alice", rag=True, cids=[c["id"]])
    check(not [v for v in job.violations if v.layer == Layer.GUIDELINE],
          "unconfirmed guideline finding dropped by verifier")

    print("== HTTP surface: collections API auth + isolation ==")
    from fastapi.testclient import TestClient
    import backend.main as appmod
    with TestClient(appmod.app) as client:
        # token-mode login as two users
        def login(u):
            r = client.post("/api/login", json={"username": u, "password": "tok"})
            tok = r.cookies.get("koosys_session")
            client.cookies.clear()
            return {"Cookie": f"koosys_session={tok}"}
        # health advertises rag
        check(client.get("/api/health").json().get("rag_enabled") is True,
              "health reports rag_enabled")
        ah = login("apiuser")
        r = client.post("/api/collections", json={"name": "My Rules"}, headers=ah)
        check(r.status_code == 200, "create collection via API")
        cid = r.json()["id"]
        check(client.get("/api/collections", headers=ah).json()["collections"][0]["id"] == cid,
              "list shows the created collection")
        # a different user cannot see or delete it
        bh = login("otheruser")
        check(len(client.get("/api/collections", headers=bh).json()["collections"]) == 0,
              "other user sees none (isolation over HTTP)")
        check(client.delete(f"/api/collections/{cid}", headers=bh).status_code == 404,
              "other user cannot delete someone else's collection")
        # unauth blocked
        check(client.get("/api/collections").status_code == 401,
              "collections API requires auth")
finally:
    fake.stop()

print()
if FAIL:
    print(f"{len(FAIL)} CHECK(S) FAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL RAG CHECKS PASSED")
