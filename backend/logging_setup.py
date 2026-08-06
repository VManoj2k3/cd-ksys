"""Application logging. Config-driven (logging.level, logging.format).

`format: plain` — human-readable lines for a terminal.
`format: json`  — one JSON object per line for log shippers (no extra deps).

Uvicorn's own loggers are re-pointed at the same handler so the whole
process emits one consistent stream.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from backend.app_config import CFG


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": round(time.time(), 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    level = getattr(logging, str(CFG.get("logging.level", "info")).upper(),
                    logging.INFO)
    fmt = str(CFG.get("logging.format", "plain")).lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # unify uvicorn's loggers into the same stream/format
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
