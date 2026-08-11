"""Local text embedder — config-driven backend, on-prem only.

Backends (config rag.embedder.backend):
- "llama"  : OpenAI-compatible /v1/embeddings served by a local llama.cpp
             instance running a small GGUF embedding model (production on the
             GPU box). Keeps the whole stack in llama.cpp — no torch/onnx.
- "hash"   : deterministic pure-Python feature-hashing embedder. No model,
             no network, no native deps — used for offline dev/test and as a
             graceful fallback if the embedding server is unreachable. Crude
             lexical similarity only; NOT for production quality.

The vector is always L2-normalized so a dot product is cosine similarity.
"""
from __future__ import annotations

import hashlib
import math
import re

import httpx

from backend.app_config import CFG

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0.0:
        return vec
    return [x / n for x in vec]


class HashEmbedder:
    """Deterministic feature-hashing embedder (pure Python, no deps).

    Hashes words and character trigrams into a fixed-dimension vector. Texts
    that share tokens get higher cosine similarity. Good enough to exercise
    and unit-test the full RAG pipeline offline; on the GPU box the "llama"
    backend replaces it for real semantic quality.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        text = text.lower()
        words = _WORD.findall(text)
        feats = list(words)
        for w in words:
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                feats.append(padded[i:i + 3])  # char trigram
        return feats

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in self._features(text):
            h = hashlib.md5(feat.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        return _l2_normalize(vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class LlamaEmbedder:
    """OpenAI-compatible embeddings from a local llama.cpp server."""

    def __init__(self, base_url: str, model: str, timeout: float, batch: int):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.batch = max(1, batch)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as cx:
            for i in range(0, len(texts), self.batch):
                chunk = texts[i:i + self.batch]
                r = cx.post(f"{self.base_url}/embeddings",
                            json={"model": self.model, "input": chunk})
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                out.extend(_l2_normalize([float(x) for x in d["embedding"]])
                           for d in data)
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _build_embedder():
    backend = str(CFG.get("rag.embedder.backend", "hash")).lower()
    if backend == "llama":
        return LlamaEmbedder(
            base_url=str(CFG.get("rag.embedder.base_url",
                                 "http://127.0.0.1:8090/v1")),
            model=str(CFG.get("rag.embedder.model", "nomic-embed-text")),
            timeout=float(CFG.get("rag.embedder.timeout_seconds", 60)),
            batch=int(CFG.get("rag.embedder.batch_size", 32)),
        )
    return HashEmbedder(dim=int(CFG.get("rag.embedder.hash_dim", 256)))


_EMBEDDER = None


def get_embedder():
    """Process-wide embedder, with graceful fallback to the hash embedder if
    the configured backend cannot be reached (so the app never hard-fails on
    a missing embedding server — reviews still run Phase 1)."""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _build_embedder()
    return _EMBEDDER


def reset_embedder_for_tests() -> None:
    global _EMBEDDER
    _EMBEDDER = None
