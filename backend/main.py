"""FastAPI app: job-based review API + static pure-web frontend.

Authenticated for internal deployment: every UI/API route except the login
page, static assets, and health requires a valid session (see backend.auth).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import audit, auth
from backend.app_config import CFG, PROJECT_ROOT
from backend.languages.base import all_extensions, language_names
from backend.llm.client import CLIENT
from backend.models import ReviewJob
from backend.orchestrator import run_review

app = FastAPI(title="Cd koosys — Code Review")

_FRONTEND = PROJECT_ROOT / CFG.get("paths.frontend_dir", "frontend")
_MAX_KB = int(CFG.get("server.max_file_size_kb", 512))
_TTL = int(CFG.get("server.job_ttl_seconds", 3600))
_ALLOWED_EXT = tuple(all_extensions())
_AUTH_MODE = str(CFG.get("auth.mode", "none")).lower()

_jobs: dict[str, ReviewJob] = {}
_job_times: dict[str, float] = {}
_job_owner: dict[str, str] = {}


class ReviewRequest(BaseModel):
    code: str
    filename: str = "snippet.py"
    language: str = ""   # explicit UI selection; overrides filename inference


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


# ---------------------------------------------------------------- auth guard
def require_user(request: Request) -> str:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    ok, msg = auth.authenticate(req.username, req.password)
    ip = request.client.host if request.client else "?"
    audit.record("login", req.username or "?", time.time(), ok=ok, ip=ip, detail=msg)
    if not ok:
        raise HTTPException(401, "Invalid credentials")
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
def _gc_jobs() -> None:
    now = time.time()
    for jid in [j for j, t in _job_times.items() if now - t > _TTL]:
        _jobs.pop(jid, None)
        _job_times.pop(jid, None)
        _job_owner.pop(jid, None)


def _start_job(code: str, filename: str, language: str, user: str) -> ReviewJob:
    _gc_jobs()
    if len(code.encode()) > _MAX_KB * 1024:
        raise HTTPException(413, f"File exceeds {_MAX_KB} KB limit")
    if not code.strip():
        raise HTTPException(400, "Empty code")
    job = ReviewJob(job_id=uuid.uuid4().hex, filename=filename, code=code,
                    requested_language=language)
    _jobs[job.job_id] = job
    _job_times[job.job_id] = time.time()
    _job_owner[job.job_id] = user
    audit.record("review", user, time.time(), job_id=job.job_id,
                 filename=filename, language=language, size_bytes=len(code.encode()))

    async def _run():
        try:
            await run_review(job)
            audit.record("review_done", user, time.time(), job_id=job.job_id,
                         violations=len(job.violations), state=job.state)
        except Exception as exc:  # noqa: BLE001 — surface, never crash server
            job.state = "error"
            job.error = str(exc)

    asyncio.get_event_loop().create_task(_run())
    return job


@app.post("/api/review")
async def submit_review(req: ReviewRequest, user: str = Depends(require_user)):
    job = _start_job(req.code, req.filename, req.language, user)
    return {"job_id": job.job_id}


@app.post("/api/review/upload")
async def submit_upload(file: UploadFile = File(...), user: str = Depends(require_user)):
    name = file.filename or "upload.py"
    if not name.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(400, f"Supported file types: {', '.join(_ALLOWED_EXT)}")
    raw = await file.read()
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
        "llm_available": await CLIENT.health(),
        "languages": language_names(),
        "extensions": list(_ALLOWED_EXT),
        "auth_mode": _AUTH_MODE,
    }


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


app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")


def main() -> None:
    import uvicorn

    host = str(CFG.get("server.host"))
    if host == "0.0.0.0" and _AUTH_MODE == "none":
        print("WARNING: binding on 0.0.0.0 with auth.mode=none — the service is "
              "OPEN to the whole network. Set auth.mode and a bind host for "
              "internal deployment.")
    uvicorn.run(
        "backend.main:app", host=host, port=int(CFG.get("server.port")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
