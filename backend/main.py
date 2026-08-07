"""FastAPI app: job-based review API + static pure-web frontend.

Authenticated for internal deployment: every UI/API route except the login
page, static assets, and health requires a valid session (see backend.auth).

Production hardening:
- config is validated at startup; the process refuses to start on bad config
- bounded work: max concurrent reviews (queue beyond that), max active jobs,
  max stored jobs, request/upload size caps — all config-driven
- login throttling (failed-attempt window) and per-user review rate limits
- security headers on every response; no-store on API responses
- structured logging, /api/version, Prometheus-text /api/metrics
- graceful shutdown: running reviews are cancelled and marked, LLM client closed

NOTE: the job store is in-process memory — run exactly ONE worker process.
Scale up on one box via llm.max_parallel_requests / review concurrency, not
via multiple uvicorn workers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import audit, auth, metrics
from backend.app_config import CFG, PROJECT_ROOT, validate_config
from backend.languages.base import all_extensions, language_names
from backend.llm.client import CLIENT
from backend.logging_setup import setup_logging
from backend.models import ReviewJob
from backend.orchestrator import run_review
from backend.ratelimit import FixedWindow

log = logging.getLogger("koosys")

_FRONTEND = PROJECT_ROOT / CFG.get("paths.frontend_dir", "frontend")
_MAX_KB = int(CFG.get("server.max_file_size_kb", 512))
_MAX_REQUEST_BYTES = int(CFG.get("server.max_request_kb", 2048)) * 1024
_TTL = int(CFG.get("server.job_ttl_seconds", 3600))
_ALLOWED_EXT = tuple(all_extensions())
_AUTH_MODE = str(CFG.get("auth.mode", "none")).lower()

_MAX_CONCURRENT = int(CFG.get("server.max_concurrent_reviews", 2))
_MAX_ACTIVE = int(CFG.get("server.max_active_jobs", 10))
_MAX_STORED = int(CFG.get("server.max_jobs_in_memory", 500))
_GC_INTERVAL = int(CFG.get("server.gc_interval_seconds", 60))

_VERSION = "unknown"
_version_file = PROJECT_ROOT / "VERSION"
if _version_file.exists():
    _VERSION = _version_file.read_text(encoding="utf-8").strip() or "unknown"

_jobs: dict[str, ReviewJob] = {}
_job_times: dict[str, float] = {}
_job_owner: dict[str, str] = {}
_job_tasks: dict[str, asyncio.Task] = {}
_review_sem = asyncio.Semaphore(_MAX_CONCURRENT)

_login_user_window = FixedWindow(
    int(CFG.get("auth.login_max_attempts", 5)),
    int(CFG.get("auth.login_window_seconds", 300)))
_login_ip_window = FixedWindow(
    int(CFG.get("auth.login_ip_max_attempts", 20)),
    int(CFG.get("auth.login_window_seconds", 300)))
_review_rate = FixedWindow(
    int(CFG.get("server.reviews_per_user_per_minute", 12)), 60)

metrics.describe("koosys_build_info", "gauge", "Build/version info (value is 1)")
metrics.describe("koosys_reviews_total", "counter", "Reviews by final result")
metrics.describe("koosys_review_duration_seconds", "counter",
                 "Total review wall-clock seconds and count")
metrics.describe("koosys_logins_total", "counter", "Login attempts by result")
metrics.describe("koosys_rate_limited_total", "counter",
                 "Requests rejected by rate limiting, by scope")
metrics.describe("koosys_jobs_in_memory", "gauge", "Jobs currently stored")
metrics.describe("koosys_reviews_active", "gauge",
                 "Reviews currently queued or running")
metrics.describe("koosys_llm_available", "gauge",
                 "1 if llama-server is reachable, else 0")


_RAG_ENABLED = bool(CFG.get("rag.enabled", False))
_MAX_COLLECTIONS = int(CFG.get("rag.max_collections_per_user", 20))
_MAX_PDF_BYTES = int(CFG.get("rag.max_pdf_mb", 25)) * 1024 * 1024


class ReviewRequest(BaseModel):
    code: str
    filename: str = "snippet.py"
    language: str = ""   # explicit UI selection; overrides filename inference
    rag_enabled: bool = False           # Phase 2 toggle
    collection_ids: list[str] = []      # selected guideline collections


class CollectionCreate(BaseModel):
    name: str = ""


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


# ---------------------------------------------------------------- lifecycle
def _fail_on_bad_config() -> None:
    errors, warnings = validate_config(CFG)
    for w in warnings:
        log.warning("config: %s", w)
    if errors:
        for e in errors:
            log.error("config: %s", e)
        raise RuntimeError(
            f"refusing to start: {len(errors)} configuration error(s) — "
            "fix config.yaml (see log lines above)")


async def _gc_loop() -> None:
    while True:
        await asyncio.sleep(_GC_INTERVAL)
        try:
            _gc_jobs()
        except Exception:  # noqa: BLE001 — GC must never die
            log.exception("job GC failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    _fail_on_bad_config()
    metrics.set_gauge("koosys_build_info", 1, {"version": _VERSION})
    log.info("koosys %s starting: auth=%s languages=%s max_concurrent_reviews=%d",
             _VERSION, _AUTH_MODE, ",".join(language_names()), _MAX_CONCURRENT)
    gc_task = asyncio.create_task(_gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        running = [t for t in _job_tasks.values() if not t.done()]
        for t in running:
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
            log.info("shutdown: cancelled %d in-flight review(s)", len(running))
        for job in _jobs.values():
            if job.state in ("queued", "running"):
                job.state = "error"
                job.error = "Server shut down while the review was in progress"
        await CLIENT.aclose()
        log.info("koosys stopped")


app = FastAPI(title="Cd koosys — Code Review", version=_VERSION,
              lifespan=lifespan)


# ---------------------------------------------------------------- middleware
@app.middleware("http")
async def hardening_middleware(request: Request, call_next):
    # early request-size guard (normal clients always send Content-Length)
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_REQUEST_BYTES:
                return JSONResponse(
                    {"detail": f"Request exceeds {_MAX_REQUEST_BYTES // 1024} KB"},
                    status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Bad Content-Length"}, status_code=400)

    response: Response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'; object-src 'none'")
    if bool(CFG.get("server.hsts_enabled", False)):
        response.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------- auth guard
def require_user(request: Request) -> str:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    user_key = (ip, req.username.lower())
    if _login_user_window.blocked(user_key) or _login_ip_window.blocked(ip):
        retry = max(_login_user_window.retry_after(user_key),
                    _login_ip_window.retry_after(ip))
        metrics.inc("koosys_logins_total", {"result": "throttled"})
        metrics.inc("koosys_rate_limited_total", {"scope": "login"})
        audit.record("login_throttled", req.username or "?", time.time(), ip=ip)
        raise HTTPException(
            429, "Too many failed sign-in attempts — try again later",
            headers={"Retry-After": str(retry)})

    ok, msg = auth.authenticate(req.username, req.password)
    audit.record("login", req.username or "?", time.time(), ok=ok, ip=ip, detail=msg)
    if not ok:
        _login_user_window.hit(user_key)
        _login_ip_window.hit(ip)
        metrics.inc("koosys_logins_total", {"result": "fail"})
        raise HTTPException(401, "Invalid credentials")

    _login_user_window.reset(user_key)
    metrics.inc("koosys_logins_total", {"result": "ok"})
    resp = JSONResponse({"ok": True, "user": req.username or "user"})
    resp.set_cookie(
        auth.COOKIE_NAME, auth.issue_session(req.username or "user"),
        httponly=True, samesite="lax",
        secure=bool(CFG.get("auth.cookie_secure", False)),
        max_age=int(CFG.get("auth.session_ttl_seconds", 28800)),
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------------------------------------------------------------- jobs
def _active_count() -> int:
    return sum(1 for j in _jobs.values() if j.state in ("queued", "running"))


def _gc_jobs() -> None:
    now = time.time()
    for jid in [j for j, t in _job_times.items() if now - t > _TTL]:
        task = _job_tasks.get(jid)
        if task is not None and not task.done():
            continue  # never GC a live review
        _jobs.pop(jid, None)
        _job_times.pop(jid, None)
        _job_owner.pop(jid, None)
        _job_tasks.pop(jid, None)


def _evict_if_full() -> None:
    """Keep the store under max_jobs_in_memory by dropping oldest FINISHED jobs."""
    if len(_jobs) < _MAX_STORED:
        return
    finished = sorted(
        (jid for jid, j in _jobs.items() if j.state in ("done", "error")),
        key=lambda jid: _job_times.get(jid, 0.0))
    for jid in finished[:max(0, len(_jobs) - _MAX_STORED + 1)]:
        _jobs.pop(jid, None)
        _job_times.pop(jid, None)
        _job_owner.pop(jid, None)
        _job_tasks.pop(jid, None)


def _start_job(code: str, filename: str, language: str, user: str,
               rag_enabled: bool = False,
               collection_ids: list[str] | None = None) -> ReviewJob:
    _gc_jobs()
    if len(code.encode()) > _MAX_KB * 1024:
        raise HTTPException(413, f"File exceeds {_MAX_KB} KB limit")
    if not code.strip():
        raise HTTPException(400, "Empty code")

    if not _review_rate.hit(user):
        metrics.inc("koosys_rate_limited_total", {"scope": "review"})
        raise HTTPException(
            429, "Too many reviews in the last minute — wait and retry",
            headers={"Retry-After": str(_review_rate.retry_after(user))})
    if _active_count() >= _MAX_ACTIVE:
        metrics.inc("koosys_rate_limited_total", {"scope": "capacity"})
        raise HTTPException(
            429, "Server is at review capacity — try again shortly",
            headers={"Retry-After": "30"})
    _evict_if_full()
    if len(_jobs) >= _MAX_STORED:
        raise HTTPException(503, "Server job store is full — try again shortly")

    rag_on = bool(rag_enabled) and _RAG_ENABLED
    cids = [c for c in (collection_ids or []) if isinstance(c, str)][:10]
    job = ReviewJob(job_id=uuid.uuid4().hex, filename=filename, code=code,
                    requested_language=language, user=user,
                    rag_enabled=rag_on, collection_ids=cids if rag_on else [])
    _jobs[job.job_id] = job
    _job_times[job.job_id] = time.time()
    _job_owner[job.job_id] = user
    audit.record("review", user, time.time(), job_id=job.job_id,
                 filename=filename, language=language, size_bytes=len(code.encode()),
                 rag=rag_on, collections=len(cids) if rag_on else 0)

    max_s = int(CFG.get("review_max_seconds", 600))

    async def _run():
        started = time.monotonic()
        try:
            async with _review_sem:  # queue position; budget starts when running
                started = time.monotonic()
                await asyncio.wait_for(run_review(job), timeout=max_s)
            metrics.inc("koosys_reviews_total", {"result": job.state})
            audit.record("review_done", user, time.time(), job_id=job.job_id,
                         violations=len(job.violations), state=job.state)
        except asyncio.TimeoutError:
            job.state = "error"
            job.error = f"Review exceeded {max_s}s and was cancelled"
            metrics.inc("koosys_reviews_total", {"result": "timeout"})
            audit.record("review_timeout", user, time.time(), job_id=job.job_id)
        except asyncio.CancelledError:
            job.state = "error"
            job.error = "Review cancelled (server shutdown)"
            metrics.inc("koosys_reviews_total", {"result": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 — surface, never crash server
            job.state = "error"
            job.error = str(exc)
            metrics.inc("koosys_reviews_total", {"result": "error"})
            log.exception("review %s failed", job.job_id)
        finally:
            metrics.observe("koosys_review_duration_seconds",
                            time.monotonic() - started)

    task = asyncio.create_task(_run())
    _job_tasks[job.job_id] = task
    return job


@app.post("/api/review")
async def submit_review(req: ReviewRequest, user: str = Depends(require_user)):
    job = _start_job(req.code, req.filename, req.language, user,
                     rag_enabled=req.rag_enabled, collection_ids=req.collection_ids)
    return {"job_id": job.job_id}


# ---------------------------------------------------------------- collections
def _require_rag() -> None:
    if not _RAG_ENABLED:
        raise HTTPException(404, "Guideline collections are disabled")


@app.get("/api/collections")
async def list_collections(user: str = Depends(require_user)):
    _require_rag()
    from backend.rag import store
    return {"collections": store.list_collections(user)}


@app.post("/api/collections")
async def create_collection(req: CollectionCreate, user: str = Depends(require_user)):
    _require_rag()
    from backend.rag import store
    if len(store.list_collections(user)) >= _MAX_COLLECTIONS:
        raise HTTPException(400, f"Collection limit reached ({_MAX_COLLECTIONS})")
    if not req.name.strip():
        raise HTTPException(400, "Collection name required")
    meta = store.create_collection(user, req.name)
    audit.record("collection_create", user, time.time(), collection=meta["id"])
    return meta


@app.delete("/api/collections/{collection_id}")
async def delete_collection(collection_id: str, user: str = Depends(require_user)):
    _require_rag()
    from backend.rag import store
    try:
        ok = store.delete_collection(user, collection_id)
    except ValueError:
        raise HTTPException(400, "Invalid collection id") from None
    if not ok:
        raise HTTPException(404, "Unknown collection")
    audit.record("collection_delete", user, time.time(), collection=collection_id)
    return {"ok": True}


@app.post("/api/collections/{collection_id}/upload")
async def upload_guideline(collection_id: str, file: UploadFile = File(...),
                           user: str = Depends(require_user)):
    _require_rag()
    from backend.rag import ingest, store
    if store.get_collection(user, collection_id) is None:
        raise HTTPException(404, "Unknown collection")
    name = file.filename or "guideline.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF guideline documents are supported")
    raw = await file.read(_MAX_PDF_BYTES + 1)
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF exceeds {_MAX_PDF_BYTES // (1024*1024)} MB limit")
    try:
        n = await asyncio.to_thread(ingest.ingest_pdf, user, collection_id, name, raw)
    except Exception as exc:  # noqa: BLE001 — surface a clean error
        log.exception("ingest failed")
        raise HTTPException(400, f"Could not index PDF: {str(exc)[:150]}") from None
    if n == 0:
        raise HTTPException(400, "No extractable text found in the PDF "
                                 "(is it a scanned image?)")
    audit.record("collection_upload", user, time.time(),
                 collection=collection_id, filename=name, chunks=n)
    return {"ok": True, "chunks": n, "collection": store.get_collection(user, collection_id)}


@app.post("/api/review/upload")
async def submit_upload(file: UploadFile = File(...), user: str = Depends(require_user)):
    name = file.filename or "upload.py"
    if not name.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(400, f"Supported file types: {', '.join(_ALLOWED_EXT)}")
    raw = await file.read(_MAX_KB * 1024 + 1)  # bounded read — never slurp more
    if len(raw) > _MAX_KB * 1024:
        raise HTTPException(413, f"File exceeds {_MAX_KB} KB limit")
    try:
        code = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File is not valid UTF-8 text") from None
    job = _start_job(code, name, "", user)
    return {"job_id": job.job_id, "code": code, "filename": name}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str, user: str = Depends(require_user)):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job")
    # users can only read their own jobs (anonymous mode shares)
    if _AUTH_MODE != "none" and _job_owner.get(job_id) not in (user, None):
        raise HTTPException(403, "Not your job")
    return job.model_dump(exclude={"code"})


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": _VERSION,
        "llm_available": await CLIENT.health(),
        "languages": language_names(),
        "extensions": list(_ALLOWED_EXT),
        "auth_mode": _AUTH_MODE,
        "rag_enabled": _RAG_ENABLED,
    }


@app.get("/api/version")
async def version():
    return {"name": "cd-koosys", "version": _VERSION}


def _metrics_authorized(request: Request) -> bool:
    token = os.environ.get("KOOSYS_METRICS_TOKEN", "") or \
        str(CFG.get("server.metrics_token", "") or "")
    if token:
        supplied = request.headers.get("authorization", "")
        return supplied == f"Bearer {token}"
    if _AUTH_MODE == "none":
        return True
    return auth.current_user(request) is not None


@app.get("/api/metrics")
async def metrics_endpoint(request: Request):
    if not _metrics_authorized(request):
        raise HTTPException(401, "Metrics require a bearer token or session")
    metrics.set_gauge("koosys_jobs_in_memory", len(_jobs))
    metrics.set_gauge("koosys_reviews_active", _active_count())
    metrics.set_gauge("koosys_llm_available", 1 if await CLIENT.health() else 0)
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4")


@app.get("/api/me")
async def me(request: Request):
    return {"user": auth.current_user(request), "auth_mode": _AUTH_MODE}


# ---------------------------------------------------------------- pages
@app.get("/login")
async def login_page():
    return FileResponse(_FRONTEND / "login.html")


@app.get("/")
async def index(request: Request):
    if _AUTH_MODE != "none" and auth.current_user(request) is None:
        return RedirectResponse("/login")
    return FileResponse(_FRONTEND / "index.html")


@app.get("/collections")
async def collections_page(request: Request):
    if not _RAG_ENABLED:
        return RedirectResponse("/")
    if _AUTH_MODE != "none" and auth.current_user(request) is None:
        return RedirectResponse("/login")
    return FileResponse(_FRONTEND / "collections.html")


app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")


def main() -> None:
    import uvicorn

    setup_logging()
    try:
        _fail_on_bad_config()
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)

    trust_proxy = bool(CFG.get("server.trust_proxy_headers", False))
    uvicorn.run(
        "backend.main:app",
        host=str(CFG.get("server.host", "127.0.0.1")),
        port=int(CFG.get("server.port", 8000)),
        log_level=str(CFG.get("logging.level", "info")).lower(),
        proxy_headers=trust_proxy,
        forwarded_allow_ips="*" if trust_proxy else None,
        access_log=bool(CFG.get("logging.access_log", True)),
    )


if __name__ == "__main__":
    main()
