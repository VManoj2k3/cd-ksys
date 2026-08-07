"""In-memory fixed-window rate limiting.

Two uses:
- login throttling: count FAILED logins per (ip, username) and per ip;
  too many failures inside the window -> 429 until the window rolls over.
- review submissions: per-user requests-per-minute cap.

Process-local by design (single-worker service). Entries self-expire; a
prune pass runs on every hit so the maps cannot grow unbounded.
"""
from __future__ import annotations

import threading
import time


class FixedWindow:
    """Count events per key inside a rolling fixed window."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = int(limit)
        self.window = int(window_seconds)
        self._hits: dict[object, tuple[float, int]] = {}  # key -> (window_start, count)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [k for k, (start, _) in self._hits.items()
                   if now - start > self.window]
        for k in expired:
            del self._hits[k]

    def hit(self, key: object) -> bool:
        """Record one event. Returns True if the event is within the limit."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            start, count = self._hits.get(key, (now, 0))
            if now - start > self.window:
                start, count = now, 0
            count += 1
            self._hits[key] = (start, count)
            return count <= self.limit

    def blocked(self, key: object) -> bool:
        """True if the key already reached the limit in the current window."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._hits.get(key)
            if entry is None:
                return False
            start, count = entry
            return now - start <= self.window and count >= self.limit

    def retry_after(self, key: object) -> int:
        """Seconds until the key's window expires (for the Retry-After header)."""
        with self._lock:
            entry = self._hits.get(key)
            if entry is None:
                return 0
            start, _ = entry
            return max(0, int(self.window - (time.monotonic() - start)) + 1)

    def reset(self, key: object) -> None:
        with self._lock:
            self._hits.pop(key, None)
