"""Scriptable fake llama-server for pipeline testing (OpenAI-compatible).

Serves /v1/models (health) and /v1/chat/completions. Each incoming prompt is
classified by template markers (review / verify / fix / fix_verify) and routed
to a per-kind handler the test sets. Handlers return a dict (sent as JSON
content) or a raw string (sent verbatim — e.g. to simulate malformed output).

This lets tests emulate an adversarial model — hallucinated quotes, bad
categories, no-op or destructive fixes — and assert the pipeline's gates
reject the garbage while keeping the planted real findings.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Request

Handler = Callable[[str], dict | str]

_MARKERS = [
    ("review", "Report violations ONLY in these categories"),
    ("guideline_verify", "double-checking one claimed coding-guideline"),
    ("guideline", "checking code against SPECIFIC coding-guideline"),
    ("verify", "double-checking one candidate finding"),
    ("fix_verify", "judging a PROPOSED PATCH"),
    ("fix", "You fix one specific violation"),
]

DEFAULTS: dict[str, Handler] = {
    "review": lambda p: {"violations": []},
    "guideline": lambda p: {"violations": []},
    "guideline_verify": lambda p: {"confirmed": True, "reason": "default confirm"},
    "verify": lambda p: {"confirmed": True, "reason": "default confirm"},
    "fix_verify": lambda p: {"approved": True, "reason": "default approve"},
    "fix": lambda p: {"start_line": 1, "end_line": 1, "replacement": ""},
    "other": lambda p: {},
}


def _classify(prompt: str) -> str:
    for kind, marker in _MARKERS:
        if marker in prompt:
            return kind
    return "other"


class FakeLLM:
    def __init__(self, port: int):
        self.port = port
        self.handlers: dict[str, Handler] = {}
        self.calls: dict[str, int] = {}
        self.prompts: dict[str, list[str]] = {}
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

        app = FastAPI()

        @app.get("/v1/models")
        async def models() -> dict[str, Any]:
            return {"data": [{"id": "fake-model"}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> dict[str, Any]:
            body = await request.json()
            prompt = body["messages"][0]["content"]
            kind = _classify(prompt)
            self.calls[kind] = self.calls.get(kind, 0) + 1
            self.prompts.setdefault(kind, []).append(prompt)
            handler = self.handlers.get(kind, DEFAULTS[kind])
            payload = handler(prompt)
            content = payload if isinstance(payload, str) else json.dumps(payload)
            return {"choices": [{"message": {"content": content}}]}

        self._app = app

    def reset(self, **handlers: Handler) -> None:
        """Clear counters/prompts and install this scenario's handlers."""
        self.handlers = dict(handlers)
        self.calls = {}
        self.prompts = {}

    def start(self) -> None:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.3):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("fake llama-server did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
