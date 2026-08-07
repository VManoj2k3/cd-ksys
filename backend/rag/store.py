"""Per-user, disk-backed vector store for guideline collections.

On-prem and dependency-light: each collection is a directory of JSON files
under a per-user folder. Search is exact cosine similarity (vectors are
L2-normalized, so a dot product suffices) computed in pure Python, with an
optional numpy fast path when available. Collections persist across restarts.

Layout:
  <data_dir>/<user_hash>/<collection_id>/meta.json     # name, docs, signature
  <data_dir>/<user_hash>/<collection_id>/chunks.jsonl  # one chunk per line

Isolation: the user folder name is a hash of the username, and collection ids
are validated hex — a request can never reference another user's data or
escape the data root via path traversal.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from backend.app_config import CFG, PROJECT_ROOT

try:  # optional acceleration; the store works without it
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

_ID_RE = re.compile(r"^[0-9a-f]{8,40}$")
_lock = threading.RLock()
_cache: dict[str, tuple[float, list[dict]]] = {}  # path -> (mtime, chunks)


def _data_root() -> Path:
    base = CFG.get("rag.data_dir", "")
    root = Path(base) if base else PROJECT_ROOT / "data" / "rag"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user: str) -> Path:
    h = hashlib.sha256((user or "anonymous").encode("utf-8")).hexdigest()[:16]
    d = _data_root() / h
    d.mkdir(parents=True, exist_ok=True)
    return d


def _coll_dir(user: str, collection_id: str) -> Path:
    if not _ID_RE.match(collection_id or ""):
        raise ValueError("invalid collection id")
    return _user_dir(user) / collection_id


def embedder_signature() -> str:
    """Identifies the embedder that produced stored vectors. A collection
    built with a different embedder/dim is not comparable and is skipped."""
    backend = str(CFG.get("rag.embedder.backend", "hash")).lower()
    if backend == "llama":
        return f"llama:{CFG.get('rag.embedder.model', 'nomic-embed-text')}"
    return f"hash:{int(CFG.get('rag.embedder.hash_dim', 256))}"


# ------------------------------------------------------------------ CRUD
def create_collection(user: str, name: str) -> dict[str, Any]:
    with _lock:
        cid = uuid.uuid4().hex[:16]
        d = _coll_dir(user, cid)
        d.mkdir(parents=True, exist_ok=True)
        meta = {"id": cid, "name": (name or "untitled").strip()[:80],
                "created": int(time.time()), "docs": [], "chunks": 0,
                "signature": embedder_signature()}
        _write_meta(d, meta)
        (d / "chunks.jsonl").touch()
        return meta


def list_collections(user: str) -> list[dict[str, Any]]:
    out = []
    udir = _user_dir(user)
    for d in sorted(udir.iterdir()) if udir.exists() else []:
        meta_f = d / "meta.json"
        if d.is_dir() and meta_f.exists():
            try:
                out.append(json.loads(meta_f.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — skip corrupt entries
                continue
    return sorted(out, key=lambda m: m.get("created", 0), reverse=True)


def get_collection(user: str, collection_id: str) -> dict[str, Any] | None:
    d = _coll_dir(user, collection_id)
    meta_f = d / "meta.json"
    if not meta_f.exists():
        return None
    return json.loads(meta_f.read_text(encoding="utf-8"))


def delete_collection(user: str, collection_id: str) -> bool:
    import shutil
    with _lock:
        d = _coll_dir(user, collection_id)
        if not d.exists():
            return False
        _cache.pop(str(d / "chunks.jsonl"), None)
        shutil.rmtree(d, ignore_errors=True)
        return True


def _write_meta(d: Path, meta: dict) -> None:
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ chunks
def add_chunks(user: str, collection_id: str, source: str,
               chunks: list[dict], vectors: list[list[float]]) -> int:
    """Append embedded chunks (each: {text, page}) with their vectors."""
    with _lock:
        d = _coll_dir(user, collection_id)
        meta = get_collection(user, collection_id)
        if meta is None:
            raise ValueError("unknown collection")
        path = d / "chunks.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for ch, vec in zip(chunks, vectors):
                fh.write(json.dumps({
                    "id": uuid.uuid4().hex[:12], "source": source,
                    "page": ch.get("page"), "text": ch["text"], "vec": vec,
                }, ensure_ascii=False) + "\n")
        meta["chunks"] = meta.get("chunks", 0) + len(chunks)
        if source and source not in meta.get("docs", []):
            meta.setdefault("docs", []).append(source)
        meta["signature"] = embedder_signature()
        _write_meta(d, meta)
        _cache.pop(str(path), None)
        return len(chunks)


def _load_chunks(path: Path) -> list[dict]:
    key = str(path)
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    _cache[key] = (mtime, rows)
    return rows


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def search(user: str, collection_ids: list[str], query_vec: list[float],
           k: int) -> list[dict[str, Any]]:
    """Top-k chunks across the given collections by cosine similarity.
    Collections whose embedder signature doesn't match the current one are
    skipped (their vectors are not comparable)."""
    sig = embedder_signature()
    scored: list[tuple[float, dict]] = []
    for cid in collection_ids:
        try:
            d = _coll_dir(user, cid)
        except ValueError:
            continue
        meta = get_collection(user, cid)
        if meta is None or meta.get("signature") != sig:
            continue
        rows = _load_chunks(d / "chunks.jsonl")
        if not rows:
            continue
        if _np is not None:
            mat = _np.array([r["vec"] for r in rows], dtype=_np.float32)
            sims = mat @ _np.array(query_vec, dtype=_np.float32)
            for r, s in zip(rows, sims.tolist()):
                scored.append((s, {**r, "collection": cid,
                                   "collection_name": meta.get("name")}))
        else:
            for r in rows:
                s = _dot(query_vec, r["vec"])
                scored.append((s, {**r, "collection": cid,
                                   "collection_name": meta.get("name")}))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for s, r in scored[:k]:
        out.append({"text": r["text"], "source": r.get("source"),
                    "page": r.get("page"), "score": round(float(s), 4),
                    "collection": r["collection"],
                    "collection_name": r.get("collection_name")})
    return out


def reset_cache_for_tests() -> None:
    _cache.clear()
