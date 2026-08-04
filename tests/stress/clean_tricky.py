"""FP-bait: unusual but perfectly clean code. The system must stay silent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

MAX_RETRIES = 5
BACKOFF_BASE_MS = 250
CHUNK_SZ = 8192
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class RingBuf:
    """Fixed-size ring buffer with abbreviations linters love to hate."""

    cap: int
    buf: list[int | None] = field(default_factory=list)
    idx: int = 0
    cnt: int = 0

    def push(self, val: int) -> None:
        if len(self.buf) < self.cap:
            self.buf.append(val)
        else:
            self.buf[self.idx] = val
        self.idx = (self.idx + 1) % self.cap
        self.cnt = min(self.cnt + 1, self.cap)

    def drain(self) -> Iterator[int]:
        src = self.buf[self.idx:] + self.buf[: self.idx]
        yield from (v for v in src if v is not None)


def tokenize_ident(text: str) -> list[str]:
    """Walrus, comprehension, and slicing in one place."""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        if (tok := m.group(0)) and not tok.startswith("_"):
            out.append(tok)
    return out


def checksum(data: bytes) -> int:
    """Bit twiddling with named constants — not magic numbers."""
    acc = 0
    for chunk_start in range(0, len(data), CHUNK_SZ):
        window = data[chunk_start: chunk_start + CHUNK_SZ]
        acc = (acc ^ sum(window)) & 0xFFFFFFFF
    return acc
