"""FastAPI app: job-based review API + static pure-web frontend."""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

_jobs: dict[str, ReviewJob] = {}
_job_times: dict[str, float] = {}


class ReviewRequest(BaseModel):
    code: str
    filename: str = "snippet.py"


def _gc_jobs() -> None:
    now = time.time()
    for jid in [j for j, t in _job_times.items() if now - t > _TTL]:
        _jobs.pop(jid, None)
        _job_times.pop(jid, None)


def _start_job(code: str, filename: str) -> ReviewJob:
    _gc_jobs()
    if len(code.encode()) > _MAX_KB * 1024:
        raise HTTPException(413, f"File exceeds {_MAX_KB} KB limit")
    if not code.strip():
        raise HTTPException(400, "Empty code")
    job = ReviewJob(job_id=uuid.uuid4().hex, filename=filename, code=code)
    _jobs[job.job_id] = job
    _job_times[job.job_id] = time.time()

    async def _run():
        try:
            await run_review(job)
        except Exception as exc:  # noqa: BLE001 — surface, never crash server
            job.state = "error"
            job.error = str(exc)

    asyncio.get_event_loop().create_task(_run())
    return job


@app.post("/api/review")
async def submit_review(req: ReviewRequest):
    job = _start_job(req.code, req.filename)
    return {"job_id": job.job_id}


@app.post("/api/review/upload")
async def submit_upload(file: UploadFile = File(...)):
    name = file.filename or "upload.py"
    if not name.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(400, f"Supported file types: {', '.join(_ALLOWED_EXT)}")
    raw = await file.read()
    try:
        code = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File is not valid UTF-8 text") from None
    job = _start_job(code, name)
    return {"job_id": job.job_id, "code": code, "filename": name}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job")
    return job.model_dump(exclude={"code"})


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "llm_available": await CLIENT.health(),
        "languages": language_names(),
        "extensions": list(_ALLOWED_EXT),
    }


@app.get("/")
async def index():
    return FileResponse(_FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=str(CFG.get("server.host")),
        port=int(CFG.get("server.port")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
