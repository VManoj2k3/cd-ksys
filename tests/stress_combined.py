"""Combined LIVE-server stress: Phase 1 (review) + Phase 2 (collections/RAG).

Boots its own backend (token auth + mock LLM + RAG on), then hammers the HTTP
surface with adversarial inputs, cross-user isolation checks, a real end-to-end
PDF ingest, and concurrent mixed load. HARD FAIL on any crash (unexpected 5xx),
wrong status code, or isolation breach.
"""
from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "stress-secret-token"
try:  # server-side PDF extraction needs pypdf; if unavailable, skip that check
    import pypdf  # noqa: F401
    _PDF_OK = True
except Exception:  # noqa: BLE001
    _PDF_OK = False
FAILS: list[str] = []
NOTES: list[str] = []
# every response flows through here so an unexpected 5xx anywhere is caught
UNEXPECTED_5XX: list[str] = []


def fail(m: str) -> None:
    FAILS.append(m)
    print(f"  HARD FAIL  {m}")


def ok(m: str) -> None:
    print(f"  PASS       {m}")


def note(m: str) -> None:
    NOTES.append(m)
    print(f"  note       {m}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_pdf(text: str) -> bytes:
    """Minimal single-page PDF with extractable text (no external deps)."""
    stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    for i, o in enumerate(objs, 1):
        out += b"%d 0 obj" % i + o + b"endobj\n"
    out += b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF"
    return out


class Client:
    """A logged-in user session (its own cookie jar)."""

    def __init__(self, base: str, username: str):
        self.base = base
        self.username = username
        self.c = httpx.Client(base_url=base, timeout=30)

    def login(self) -> None:
        r = self.c.post("/api/login",
                        json={"username": self.username, "password": TOKEN})
        r.raise_for_status()

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self.c.request(method, path, **kw)
        except httpx.HTTPError as e:
            UNEXPECTED_5XX.append(
                f"{self.username} {method} {path} -> TRANSPORT {type(e).__name__}")
            req_obj = httpx.Request(method, self.base + path)
            return httpx.Response(599, request=req_obj)
        if r.status_code >= 500:
            UNEXPECTED_5XX.append(f"{self.username} {method} {path} -> {r.status_code}")
        return r

    def review(self, code: str, filename: str, rag=False, cids=None) -> dict:
        r = self.req("POST", "/api/review", json={
            "code": code, "filename": filename,
            "rag_enabled": rag, "collection_ids": cids or []})
        r.raise_for_status()
        jid = r.json()["job_id"]
        for _ in range(60):
            time.sleep(0.3)
            j = self.req("GET", f"/api/job/{jid}")
            if j.status_code != 200:
                raise RuntimeError(f"job fetch {j.status_code}")
            body = j.json()
            if body["state"] in ("done", "error"):
                body["_job_id"] = jid
                return body
        raise TimeoutError(filename)


def boot(port: int) -> subprocess.Popen:
    workdir = Path(tempfile.mkdtemp())
    overlay = workdir / "ov.yaml"
    overlay.write_text(f"""
server:
  host: 127.0.0.1
  port: {port}
  max_active_jobs: 60
  max_concurrent_reviews: 8
  max_jobs_in_memory: 400
  reviews_per_user_per_minute: 500
logging:
  level: warning
  access_log: false
auth:
  mode: token
  shared_token: "{TOKEN}"
  login_max_attempts: 5
  login_window_seconds: 300
llm:
  mock: true
rag:
  enabled: true
  data_dir: {workdir / "rag-data"}
  max_collections_per_user: 5
  max_pdf_mb: 1
  embedder:
    backend: hash
""")
    env = dict(os.environ, KOOSYS_CONFIG_OVERLAY=str(overlay),
               PYTHONPATH=str(ROOT),
               KOOSYS_SESSION_SECRET="0" * 64)
    logf = open(workdir / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "backend.main"],
                            cwd=str(ROOT), env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise RuntimeError("server exited during boot")
        time.sleep(0.25)
    raise RuntimeError("server did not become healthy")


# ------------------------------------------------------------------ scenarios
def s_auth(base: str) -> None:
    print("\n== auth surface ==")
    anon = httpx.Client(base_url=base, timeout=15)
    r = anon.get("/api/collections")
    (ok if r.status_code == 401 else fail)(f"no-cookie /api/collections -> {r.status_code} (want 401)")
    # /api/me is intentionally public so the SPA can detect auth state
    r = anon.get("/api/me")
    good = r.status_code == 200 and r.json().get("user") is None
    (ok if good else fail)(f"no-cookie /api/me -> {r.status_code} user={r.json().get('user')!r} (want 200/null)")
    r = anon.post("/api/login", json={"username": "x", "password": "wrong"})
    (ok if r.status_code == 401 else fail)(f"wrong token -> {r.status_code} (want 401)")
    # protected review without auth
    r = anon.post("/api/review", json={"code": "x=1", "filename": "a.py"})
    (ok if r.status_code == 401 else fail)(f"unauth review -> {r.status_code} (want 401)")


def s_phase1_adversarial(u: Client) -> None:
    print("\n== Phase 1 adversarial ==")
    r = u.req("POST", "/api/review", json={"code": "", "filename": "e.py"})
    (ok if r.status_code == 400 else fail)(f"empty code -> {r.status_code} (want 400)")
    r = u.req("POST", "/api/review", json={"code": "   \n\t\n", "filename": "w.py"})
    (ok if r.status_code == 400 else fail)(f"whitespace code -> {r.status_code} (want 400)")
    big = "x = 1\n" * 120_000
    r = u.req("POST", "/api/review", json={"code": big, "filename": "big.py"})
    (ok if r.status_code == 413 else fail)(f"oversize -> {r.status_code} (want 413)")
    r = u.req("GET", "/api/job/does-not-exist")
    (ok if r.status_code == 404 else fail)(f"unknown job -> {r.status_code} (want 404)")
    # a real review still completes and finds the planted E711
    job = u.review("def f(x):\n    if x == None:\n        return 0\n", "bug.py")
    rules = {v["rule"] for v in job["violations"]}
    (ok if job["state"] == "done" else fail)(f"planted-bug review state={job['state']}")
    (ok if "E711" in rules else note)(f"planted E711 caught (rules={sorted(rules)})")


def s_phase2_adversarial(u: Client) -> None:
    print("\n== Phase 2 adversarial (collections/upload) ==")
    # path-traversal & malformed ids must never 5xx, never escape. These all
    # reach the {collection_id} handler (or route to 404) — none may crash.
    bad_ids = [
        "zz",              # too short, non-matching
        "abcd",            # too short
        "gggggggg",        # 8 chars but non-hex
        "0" * 50,          # too long (>40)
        "AAAA1111",        # uppercase (regex is lowercase hex)
        "%2e%2e%2fetc",    # encoded ../etc — decodes inside one segment
        "../etc",          # literal traversal (URL-normalizes to /api/etc)
        "abc/def",         # extra segment
    ]
    for bad in bad_ids:
        r = u.req("DELETE", f"/api/collections/{bad}")
        (ok if r.status_code in (400, 404) else fail)(
            f"delete bad id {bad!r} -> {r.status_code} (want 400/404)")
        r = u.req("POST", f"/api/collections/{bad}/upload",
                  files={"file": ("g.pdf", make_pdf("x"), "application/pdf")})
        (ok if r.status_code in (400, 404) else fail)(
            f"upload bad id {bad!r} -> {r.status_code} (want 400/404)")
    # create one real collection
    r = u.req("POST", "/api/collections", json={"name": "std"})
    (ok if r.status_code == 200 else fail)(f"create collection -> {r.status_code}")
    cid = r.json()["id"]
    # blank name rejected
    r = u.req("POST", "/api/collections", json={"name": "   "})
    (ok if r.status_code == 400 else fail)(f"blank name -> {r.status_code} (want 400)")
    # non-PDF rejected
    r = u.req("POST", f"/api/collections/{cid}/upload",
              files={"file": ("notes.txt", b"hello", "text/plain")})
    (ok if r.status_code == 400 else fail)(f"non-PDF upload -> {r.status_code} (want 400)")
    # oversize PDF rejected (limit is 1 MB in overlay)
    r = u.req("POST", f"/api/collections/{cid}/upload",
              files={"file": ("big.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 50),
                              "application/pdf")})
    (ok if r.status_code == 413 else fail)(f"oversize PDF -> {r.status_code} (want 413)")
    # unreadable 'pdf' -> graceful 400 (ingest exception), never 500
    r = u.req("POST", f"/api/collections/{cid}/upload",
              files={"file": ("junk.pdf", b"not really a pdf at all", "application/pdf")})
    good = r.status_code in (400,)
    (ok if good else fail)(f"garbage PDF -> {r.status_code} (want 400)")
    # upload to a well-formed-but-missing id -> 404
    r = u.req("POST", "/api/collections/deadbeefdeadbeef/upload",
              files={"file": ("g.pdf", make_pdf("x"), "application/pdf")})
    (ok if r.status_code == 404 else fail)(f"upload missing coll -> {r.status_code} (want 404)")
    return cid


def s_real_pdf(u: Client, cid: str) -> None:
    print("\n== real end-to-end PDF ingest ==")
    pdf = make_pdf("Rule GOTO-1: do not use goto statements in production code")
    r = u.req("POST", f"/api/collections/{cid}/upload",
              files={"file": ("guide.pdf", pdf, "application/pdf")})
    if not _PDF_OK:
        # environment can't extract PDFs (pypdf/cryptography missing) — the
        # server must still fail cleanly (4xx), never crash.
        (ok if 400 <= r.status_code < 500 else fail)(
            f"pypdf unavailable — PDF upload rejected cleanly ({r.status_code})")
        note("pypdf unavailable: skipped real-PDF ingest assertion")
        return
    if r.status_code != 200:
        fail(f"real PDF upload -> {r.status_code} {r.text[:120]}")
        return
    n = r.json().get("chunks", 0)
    (ok if n >= 1 else fail)(f"real PDF indexed into {n} chunk(s)")
    # a RAG review referencing this collection must complete (mock LLM -> no
    # findings, but the retrieval/guideline path runs for real)
    job = u.review("int main(){ goto end; end: return 0; }", "m.c",
                   rag=True, cids=[cid])
    (ok if job["state"] == "done" else fail)(f"RAG review state={job['state']}")


def s_isolation(a: Client, b: Client, a_cid: str) -> None:
    print("\n== cross-user isolation ==")
    # b lists collections: must NOT contain a's collection
    r = b.req("GET", "/api/collections")
    ids = {c["id"] for c in r.json()["collections"]}
    (ok if a_cid not in ids else fail)("b cannot see a's collection in list")
    # b deletes a's collection -> 404 (unknown to b), and a's must survive
    r = b.req("DELETE", f"/api/collections/{a_cid}")
    (ok if r.status_code == 404 else fail)(f"b delete a's coll -> {r.status_code} (want 404)")
    r = a.req("GET", "/api/collections")
    still = a_cid in {c["id"] for c in r.json()["collections"]}
    (ok if still else fail)("a's collection survived b's delete attempt")
    # b uploads to a's collection -> 404
    r = b.req("POST", f"/api/collections/{a_cid}/upload",
              files={"file": ("g.pdf", make_pdf("x"), "application/pdf")})
    (ok if r.status_code == 404 else fail)(f"b upload to a's coll -> {r.status_code} (want 404)")
    # b reads a's job -> 403
    ajob = a.review("y = 1\n", "a_priv.py")
    r = b.req("GET", f"/api/job/{ajob['_job_id']}")
    (ok if r.status_code == 403 else fail)(f"b read a's job -> {r.status_code} (want 403)")


def s_limit(u: Client) -> None:
    print("\n== collection limit (max 5) ==")
    # u already has 'std' (1). create up to the cap, then expect 400.
    made = 1
    last = None
    for i in range(10):
        r = u.req("POST", "/api/collections", json={"name": f"c{i}"})
        if r.status_code == 200:
            made += 1
        else:
            last = r.status_code
            break
    (ok if made == 5 and last == 400 else fail)(
        f"limit enforced at 5 (created={made}, stop_code={last})")


def s_malformed_bodies(u: Client) -> None:
    print("\n== malformed request bodies (must 4xx, never 5xx) ==")
    cases = [
        ("/api/review", {"code": 123, "filename": "a.py"}),            # code not str
        ("/api/review", {"filename": "a.py"}),                         # missing code
        ("/api/review", {"code": "x=1", "collection_ids": "notalist"}),
        ("/api/review", {"code": "x=1", "collection_ids": [1, 2, 3]}),  # non-str ids
        ("/api/review", {"code": "x=1", "collection_ids": ["a"] * 500}),  # huge list
        ("/api/collections", {"name": 123}),                           # name not str
        ("/api/collections", {}),                                      # missing name
    ]
    for path, body in cases:
        r = u.req("POST", path, json=body)
        (ok if r.status_code < 500 else fail)(
            f"POST {path} {str(body)[:40]} -> {r.status_code} (must be <500)")
    r = u.req("POST", "/api/review", content=b"{not valid json",
              headers={"content-type": "application/json"})
    (ok if r.status_code < 500 else fail)(f"non-JSON review body -> {r.status_code} (<500)")


def s_same_collection(base: str) -> None:
    print("\n== concurrent writes to ONE collection (append integrity) ==")
    owner = Client(base, "writer")
    owner.login()
    cid = owner.req("POST", "/api/collections", json={"name": "shared"}).json()["id"]

    def up(i: int) -> int:
        cl = Client(base, "writer")  # same user, own connection
        cl.login()
        r = cl.req("POST", f"/api/collections/{cid}/upload",
                   files={"file": (f"g{i}.pdf",
                                   make_pdf(f"Rule R{i}: forbid token_{i} everywhere"),
                                   "application/pdf")})
        return r.json().get("chunks", 0) if r.status_code == 200 else -1

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        per = list(pool.map(up, range(16)))
    (ok if all(n >= 1 for n in per) else fail)(
        f"16 concurrent uploads all succeeded (per-call chunks={per})")
    final = None
    for c in owner.req("GET", "/api/collections").json()["collections"]:
        if c["id"] == cid:
            final = c["chunks"]
    expected = sum(n for n in per if n > 0)
    (ok if final == expected else fail)(
        f"no lost appends: meta.chunks={final} == sum(uploads)={expected}")


def s_concurrency(base: str) -> None:
    print("\n== concurrency: 8 users x mixed review+collection ops ==")
    errors: list[str] = []

    def workload(k: int) -> None:
        try:
            u = Client(base, f"conc{k}")
            u.login()
            for i in range(4):
                c = u.req("POST", "/api/collections", json={"name": f"w{i}"})
                if c.status_code != 200:
                    errors.append(f"conc{k} create {c.status_code}")
                    continue
                cid = c.json()["id"]
                up = u.req("POST", f"/api/collections/{cid}/upload",
                           files={"file": ("g.pdf",
                                           make_pdf(f"Rule {k}-{i}: forbid tok_{k}_{i}"),
                                           "application/pdf")})
                if up.status_code != 200:
                    errors.append(f"conc{k} upload {up.status_code}")
                job = u.review(f"x = {k}\ny = {i}\n", f"c{k}_{i}.py",
                               rag=(i % 2 == 0), cids=[cid])
                if job["state"] != "done":
                    errors.append(f"conc{k} job {job['state']}")
            # isolation: exactly the 4 we created, all ours
            lst = u.req("GET", "/api/collections").json()["collections"]
            if len(lst) != 4:
                errors.append(f"conc{k} sees {len(lst)} collections (want 4)")
        except Exception as e:  # noqa: BLE001
            errors.append(f"conc{k} crash {type(e).__name__}: {str(e)[:100]}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(workload, range(8)))
    (ok if not errors else fail)(f"8 concurrent users clean ({errors[:3]})")


def main() -> int:
    port = free_port()
    print(f"booting backend on :{port} (token auth + mock LLM + RAG) ...")
    proc = boot(port)
    base = f"http://127.0.0.1:{port}"
    try:
        h = httpx.get(f"{base}/api/health", timeout=10).json()
        print(f"health: auth={h['auth_mode']} rag={h['rag_enabled']} "
              f"llm={h['llm_available']} langs={len(h['languages'])}")
        if h["auth_mode"] != "token" or not h["rag_enabled"]:
            fail(f"unexpected server config: {h}")

        s_auth(base)
        alice = Client(base, "alice")
        alice.login()
        bob = Client(base, "bob")
        bob.login()
        s_phase1_adversarial(alice)
        a_cid = s_phase2_adversarial(alice)
        s_real_pdf(alice, a_cid)
        s_isolation(alice, bob, a_cid)
        s_limit(alice)
        s_malformed_bodies(alice)
        s_same_collection(base)
        s_concurrency(base)

        if UNEXPECTED_5XX:
            for m in UNEXPECTED_5XX:
                fail(f"UNEXPECTED 5XX: {m}")
        else:
            ok("zero unexpected 5xx across all requests")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{'=' * 60}")
    print(f"HARD FAILURES: {len(FAILS)}")
    for m in FAILS:
        print("  -", m)
    if NOTES:
        print(f"NOTES: {len(NOTES)}")
        for m in NOTES:
            print("  -", m)
    print("COMBINED LIVE STRESS: " + ("PASSED" if not FAILS else "FAILED"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
