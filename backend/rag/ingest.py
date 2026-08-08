"""Guideline ingestion: PDF -> text -> chunks -> embeddings -> store.

PDF text extraction uses pypdf, imported lazily inside extract_pdf_text so the
rest of the pipeline (and the tests) never depend on it. Chunking is
paragraph-aware with a token-ish size cap and overlap so a retrieved chunk
carries a whole rule, not a fragment.
"""
from __future__ import annotations

import re

from backend.app_config import CFG
from backend.rag import store
from backend.rag.embedder import get_embedder

_WS = re.compile(r"[ \t]+")


def extract_pdf_text(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """Return [(page_number, text)]. pypdf is imported here only."""
    from io import BytesIO

    from pypdf import PdfReader  # lazy: keeps core import-light and testable

    reader = PdfReader(BytesIO(pdf_bytes))
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — a bad page must not kill ingest
            txt = ""
        if txt.strip():
            pages.append((i, txt))
    return pages


def _clean(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    # collapse runs of blank lines to paragraph breaks
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def chunk_text(text: str, page: int | None) -> list[dict]:
    """Split into overlapping chunks of ~chunk_chars, preferring paragraph
    boundaries so a rule stays whole. Returns [{text, page}]."""
    chunk_chars = int(CFG.get("rag.ingest.chunk_chars", 900))
    overlap = int(CFG.get("rag.ingest.overlap_chars", 150))
    text = _clean(text)
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[dict] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 2 <= chunk_chars:
            buf = f"{buf}\n\n{para}".strip()
        else:
            if buf:
                chunks.append({"text": buf, "page": page})
            if len(para) <= chunk_chars:
                buf = para
            else:
                # a single huge paragraph: hard-split with overlap
                start = 0
                while start < len(para):
                    piece = para[start:start + chunk_chars]
                    chunks.append({"text": piece, "page": page})
                    start += max(1, chunk_chars - overlap)
                buf = ""
    if buf:
        chunks.append({"text": buf, "page": page})
    return chunks


def ingest_document(user: str, collection_id: str, filename: str,
                    pages: list[tuple[int, str]]) -> int:
    """Chunk + embed + store already-extracted page text. Returns #chunks."""
    all_chunks: list[dict] = []
    for page_no, page_text in pages:
        all_chunks.extend(chunk_text(page_text, page_no))
    max_chunks = int(CFG.get("rag.ingest.max_chunks_per_doc", 4000))
    if len(all_chunks) > max_chunks:
        all_chunks = all_chunks[:max_chunks]
    if not all_chunks:
        return 0
    vectors = get_embedder().embed([c["text"] for c in all_chunks])
    return store.add_chunks(user, collection_id, filename, all_chunks, vectors)


def ingest_pdf(user: str, collection_id: str, filename: str,
               pdf_bytes: bytes) -> int:
    return ingest_document(user, collection_id, filename,
                           extract_pdf_text(pdf_bytes))
