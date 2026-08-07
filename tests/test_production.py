"""Production-hardening test suite — runs fully offline (LLM mocked, no GPU).

    python -m tests.test_production

Covers: config validation + overlay merge, auth (token mode) + session gating,
login throttling, per-user review rate limits, capacity caps, request/upload
size limits, security headers, job ownership, /api/version + /api/metrics,
and an end-to-end deterministic review through the real HTTP API.

Style matches the other suites: plain asserts, PASS lines, exit 1 on failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# ---- test configuration BEFORE importing backend (config loads at import) ----
_REPO = Path(__file__).resolve().parent.parent
_TMPDIR = tempfile.mkdtemp(prefix="koosys-test-")

_OVERLAY = f"""
server:
  host: 127.0.0.1
  max_file_size_kb: 4
  max_request_kb: 64
  max_concurrent_reviews: 1
  max_active_jobs: 2
  max_jobs_in_memory: 50
  reviews_per_user_per_minute: 3
  gc_interval_seconds: 3600
auth:
  mode: token
  login_max_attempts: 3
  login_window_seconds: 2
  login_ip_max_attempts: 100
audit:
  enabled: true
  log_path: {_TMPDIR}/audit.log
logging:
  level: warning
  format: plain
llm:
  mock: true
  health_cache_seconds: 1
"""

_overlay_path = Path(_TMPDIR) / "overlay.yaml"
_overlay_path.write_text(_OVERLAY, encoding="utf-8")
os.environ["KOOSYS_CONFIG"] = str(_REPO / "config.yaml")
os.environ["KOOSYS_CONFIG_OVERLAY"] = str(_overlay_path)
os.environ["KOOSYS_SESSION_SECRET"] = "s" * 64
os.environ["KOOSYS_SHARED_TOKEN"] = "test-shared-token"
os.environ["KOOSYS_METRICS_TOKEN"] = "test-metrics-token"

from backend.app_config import CFG, Cfg, _deep_merge, validate_config  # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'PASS ' if ok else 'FAIL '} {label}")
    if not ok:
        FAILURES.append(label)


# ================================================================ config unit
print("== config: overlay merge + validation ==")

merged = _deep_merge({"a": {"x": 1, "y": 2}, "keep": [1, 2]},
                     {"a": {"y": 9}, "new": True})
check(merged == {"a": {"x": 1, "y": 9}, "keep": [1, 2], "new": True},
      "deep merge: overlay wins, siblings survive")

check(CFG.get("server.max_file_size_kb") == 4,
      "overlay applied over base config")
check(CFG.get("spell.dynamic_vocabulary") is True,
      "base config keys survive the overlay (no drift)")

errs, _ = validate_config(CFG)
check(errs == [], f"test config validates clean (errors: {errs})")

bad = Cfg({"server": {"host": "", "port": 99999},
           "auth": {"mode": "bogus"},
           "languages": {"enabled": ["cobol"]},
           "logging": {"level": "loud"}})
errs, _ = validate_config(bad)


def _mentions(fragment: str) -> bool:
    return any(fragment in e for e in errs)


check(_mentions("server.host"), "validation catches empty host")
check(_mentions("server.port"), "validation catches bad port")
check(_mentions("auth.mode"), "validation catches unknown auth mode")
check(_mentions("languages.enabled"), "validation catches unknown language")
check(_mentions("logging.level"), "validation catches bad log level")

_saved_secret = os.environ.pop("KOOSYS_SESSION_SECRET")
_saved_token = os.environ.pop("KOOSYS_SHARED_TOKEN")
errs, _ = validate_config(Cfg({"auth": {"mode": "token"}}))
check(_mentions("session_secret"), "validation requires session secret for auth")
check(_mentions("shared_token"), "validation requires shared token in token mode")
errs, _ = validate_config(Cfg({"auth": {"mode": "ldap"}}))
check(_mentions("server_uri"), "validation requires ldap server_uri")
os.environ["KOOSYS_SESSION_SECRET"] = _saved_secret
os.environ["KOOSYS_SHARED_TOKEN"] = _saved_token

# ================================================================ HTTP suite
from fastapi.testclient import TestClient  # noqa: E402

import backend.main as appmod  # noqa: E402


def login(client: TestClient, username: str) -> str:
    r = client.post("/api/login", json={"username": username,
                                        "password": "test-shared-token"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.cookies.get("koosys_session")
    client.cookies.clear()   # we pass cookies explicitly per request
    return tok


def cookie(tok: str) -> dict[str, str]:
    return {"Cookie": f"koosys_session={tok}"}


def wait_done(client: TestClient, hdrs: dict, job_id: str, timeout: float = 120):
    start = time.time()
    while time.time() - start < timeout:
        j = client.get(f"/api/job/{job_id}", headers=hdrs).json()
        if j["state"] in ("done", "error"):
            return j
        time.sleep(0.3)
    raise TimeoutError(f"job {job_id} did not finish")


with TestClient(appmod.app) as client:
    print("== security headers ==")
    r = client.get("/api/health")
    check(r.status_code == 200, "health endpoint reachable")
    check(r.headers.get("X-Content-Type-Options") == "nosniff",
          "nosniff header set")
    check(r.headers.get("X-Frame-Options") == "DENY", "frame-deny header set")
    check("script-src 'self'" in r.headers.get("Content-Security-Policy", ""),
          "CSP header set")
    check(r.headers.get("Cache-Control") == "no-store", "API is no-store")
    check(r.json()["auth_mode"] == "token", "auth mode active in health")
    check(r.json()["llm_available"] is False, "LLM mocked off")

    r = client.get("/login")
    check("Content-Security-Policy" in r.headers, "headers on pages too")

    print("== version ==")
    r = client.get("/api/version")
    check(r.status_code == 200 and r.json()["version"] == appmod._VERSION,
          f"version endpoint reports {appmod._VERSION}")

    print("== auth gating ==")
    r = client.get("/api/job/nope")
    check(r.status_code == 401, "job endpoint requires session")
    r = client.post("/api/review", json={"code": "x = 1", "filename": "a.py"})
    check(r.status_code == 401, "review endpoint requires session")
    r = client.get("/", follow_redirects=False)
    check(r.status_code == 307 and r.headers["location"] == "/login",
          "index redirects to login without session")

    r = client.post("/api/login", json={"username": "eve", "password": "wrong"})
    check(r.status_code == 401, "wrong token rejected")
    client.cookies.clear()

    alice = login(client, "alice")
    bob = login(client, "bob")
    r = client.get("/api/me", headers=cookie(alice))
    check(r.json()["user"] == "alice", "session cookie identifies user")

    print("== login throttling ==")
    for _ in range(3):
        client.post("/api/login", json={"username": "mallory", "password": "bad"})
        client.cookies.clear()
    r = client.post("/api/login", json={"username": "mallory",
                                        "password": "test-shared-token"})
    check(r.status_code == 429, "4th attempt throttled even with right creds")
    check("Retry-After" in r.headers, "throttle sends Retry-After")
    client.cookies.clear()
    time.sleep(2.2)  # window rolls over
    r = client.post("/api/login", json={"username": "mallory",
                                        "password": "test-shared-token"})
    check(r.status_code == 200, "throttle lifts after the window")
    client.cookies.clear()

    print("== size limits ==")
    big = "# " + "a" * (5 * 1024)  # > 4 KB file cap, < 64 KB request cap
    r = client.post("/api/review", headers=cookie(alice),
                    json={"code": big, "filename": "big.py"})
    check(r.status_code == 413, "oversized paste rejected 413")
    huge = "x" * (70 * 1024)  # > 64 KB request cap
    r = client.post("/api/review", headers=cookie(alice),
                    json={"code": huge, "filename": "huge.py"})
    check(r.status_code == 413, "oversized request body rejected early")
    r = client.post("/api/review/upload", headers=cookie(alice),
                    files={"file": ("big.py", big.encode(), "text/x-python")})
    check(r.status_code == 413, "oversized upload rejected 413")
    r = client.post("/api/review/upload", headers=cookie(alice),
                    files={"file": ("notes.docx", b"hello", "application/octet-stream")})
    check(r.status_code == 400, "unsupported extension rejected")
    r = client.post("/api/review", headers=cookie(alice),
                    json={"code": "   ", "filename": "empty.py"})
    check(r.status_code == 400, "empty code rejected")

    print("== end-to-end deterministic review (LLM mocked) ==")
    bad_code = (_REPO / "tests" / "sample_bad.py").read_text(encoding="utf-8")
    r = client.post("/api/review", headers=cookie(alice),
                    json={"code": bad_code, "filename": "sample_bad.py"})
    check(r.status_code == 200, "review accepted")
    job = wait_done(client, cookie(alice), r.json()["job_id"])
    check(job["state"] == "done", f"review finished (state={job['state']})")
    check(len(job["violations"]) > 0,
          f"violations found ({len(job['violations'])})")
    layer_states = {ls["name"]: ls["state"] for ls in job["layers"]}
    check(layer_states.get("llm_review") == "skipped", "LLM layer skipped (mock)")
    check(layer_states.get("lint") == "done", "lint layer ran")
    check("code" not in job, "source code not echoed in job responses")

    print("== job ownership ==")
    jid = r.json()["job_id"]
    r2 = client.get(f"/api/job/{jid}", headers=cookie(bob))
    check(r2.status_code == 403, "user cannot read another user's job")
    r2 = client.get(f"/api/job/{jid}", headers=cookie(alice))
    check(r2.status_code == 200, "owner can read their job")

    print("== per-user review rate limit ==")
    # drain each job before the next submit so the CAPACITY cap never trips —
    # this isolates the per-user rate window (60 s, so all 4 fall inside it)
    carol = login(client, "carol")
    codes = []
    last = None
    for i in range(4):
        r = client.post("/api/review", headers=cookie(carol),
                        json={"code": f"x = {i}\n", "filename": "t.py"})
        codes.append(r.status_code)
        last = r
        if r.status_code == 200:
            wait_done(client, cookie(carol), r.json()["job_id"], timeout=60)
    check(codes[:3] == [200, 200, 200] and codes[3] == 429,
          f"4th submission in a minute rejected (codes: {codes})")
    check(last is not None and "minute" in last.json().get("detail", ""),
          "rejection names the rate limit, not capacity")

    print("== capacity cap (max_active_jobs) ==")
    # wait for carol's jobs to drain so active count starts at 0
    deadline = time.time() + 120
    while appmod._active_count() > 0 and time.time() < deadline:
        time.sleep(0.3)
    check(appmod._active_count() == 0, "queue drained before capacity test")

    real_run_review = appmod.run_review

    async def slow_review(job):
        import asyncio
        job.state = "running"
        await asyncio.sleep(1.5)
        job.state = "done"

    appmod.run_review = slow_review
    try:
        dave = login(client, "dave")
        erin = login(client, "erin")
        r1 = client.post("/api/review", headers=cookie(dave),
                         json={"code": "a = 1\n", "filename": "a.py"})
        r2 = client.post("/api/review", headers=cookie(erin),
                         json={"code": "b = 2\n", "filename": "b.py"})
        frank = login(client, "frank")
        r3 = client.post("/api/review", headers=cookie(frank),
                         json={"code": "c = 3\n", "filename": "c.py"})
        check(r1.status_code == 200 and r2.status_code == 200,
              "submissions up to the cap accepted")
        check(r3.status_code == 429, "submission beyond max_active_jobs rejected")
        wait_done(client, cookie(dave), r1.json()["job_id"], timeout=30)
        wait_done(client, cookie(erin), r2.json()["job_id"], timeout=30)
    finally:
        appmod.run_review = real_run_review

    print("== metrics ==")
    r = client.get("/api/metrics")
    check(r.status_code == 401, "metrics require the bearer token")
    r = client.get("/api/metrics",
                   headers={"Authorization": "Bearer test-metrics-token"})
    check(r.status_code == 200, "metrics served with token")
    body = r.text
    check('koosys_build_info{version=' in body, "build info exported")
    check('koosys_reviews_total{result="done"}' in body,
          "review counter exported")
    check('koosys_logins_total{result="throttled"}' in body,
          "throttle counter exported")
    check("koosys_jobs_in_memory" in body, "job gauge exported")
    check("koosys_review_duration_seconds_sum" in body,
          "duration sum exported")

    print("== audit log ==")
    audit_file = Path(_TMPDIR) / "audit.log"
    audit_text = audit_file.read_text(encoding="utf-8") if audit_file.exists() else ""
    check('"event": "login"' in audit_text, "logins audited")
    check('"event": "review"' in audit_text, "reviews audited")
    check("test-shared-token" not in audit_text, "credentials never audited")
    check(bad_code[:40] not in audit_text, "source code never audited")

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PRODUCTION CHECKS PASSED")
