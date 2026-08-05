"""Append-only audit log for internal deployment.

Records who reviewed what and when — one JSON line per event. Never records
the reviewed source code, only metadata (filename, language, size, counts).
Path is config-driven (audit.log_path).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from backend.app_config import CFG, PROJECT_ROOT

_lock = threading.Lock()


def _path() -> Path:
    p = CFG.get("audit.log_path", "")
    path = Path(p) if p else PROJECT_ROOT / "logs" / "audit.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(event: str, user: str, ts: float, **fields) -> None:
    if not CFG.get("audit.enabled", True):
        return
    entry = {"ts": ts, "event": event, "user": user, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock, open(_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # audit must never break the request path
