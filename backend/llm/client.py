"""OpenAI-compatible client for llama-server, with grammar-constrained JSON.

Tries, in order:
1. response_format json_schema  (token-level schema enforcement in llama.cpp)
2. response_format json_object
3. plain completion + robust JSON extraction

All settings come from config.yaml (llm.*). `mock=true` (or an unreachable
server) disables the LLM layers gracefully — deterministic layers still run.

Production notes: one shared httpx.AsyncClient (connection pooling) closed on
app shutdown; health results are cached briefly so UI polling can't hammer
llama-server with /models probes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from backend.app_config import CFG

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LlamaClient:
    def __init__(self) -> None:
        self.base_url = str(CFG.get("llm.base_url")).rstrip("/")
        self.model = CFG.get("llm.model")
        self.timeout = float(CFG.get("llm.timeout_seconds", 300))
        self.temperature = float(CFG.get("llm.temperature", 0.0))
        self.mock = bool(CFG.get("llm.mock", False))
        self._sem = asyncio.Semaphore(int(CFG.get("llm.max_parallel_requests", 2)))
        self._schema_mode: str | None = None  # cached working mode
        self._client: httpx.AsyncClient | None = None
        self._health_cache: tuple[float, bool] | None = None  # (checked_at, up)
        self._health_ttl = float(CFG.get("llm.health_cache_seconds", 5))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def health(self, force: bool = False) -> bool:
        if self.mock:
            return False
        now = time.monotonic()
        if not force and self._health_cache is not None:
            checked_at, up = self._health_cache
            if now - checked_at < self._health_ttl:
                return up
        try:
            r = await self._http().get(f"{self.base_url}/models", timeout=5)
            up = r.status_code < 500
        except httpx.HTTPError:
            up = False
        self._health_cache = (now, up)
        return up

    async def chat_json(
        self, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any] | None:
        """Send a prompt, get schema-conforming JSON back (or None on failure)."""
        if self.mock:
            return None
        modes = (
            [self._schema_mode]
            if self._schema_mode
            else ["json_schema", "json_object", "plain"]
        )
        async with self._sem:
            for mode in modes:
                body: dict[str, Any] = {
                    "model": self.model,
                    "temperature": self.temperature,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if mode == "json_schema":
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "result", "strict": True, "schema": schema},
                    }
                elif mode == "json_object":
                    body["response_format"] = {"type": "json_object"}
                try:
                    r = await self._http().post(
                        f"{self.base_url}/chat/completions", json=body)
                    if r.status_code >= 400:
                        continue  # try next mode
                    content = r.json()["choices"][0]["message"]["content"]
                    parsed = self._extract_json(content)
                    if parsed is not None:
                        self._schema_mode = mode
                        return parsed
                except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
                    continue
        return None

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = _JSON_RE.search(text)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
        return None


CLIENT = LlamaClient()
