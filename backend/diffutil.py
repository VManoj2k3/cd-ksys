"""Human-readable minimal diff for file-wide fixes (display only)."""
from __future__ import annotations

import difflib

from backend.app_config import CFG


def display_diff(old: str, new: str) -> str:
    """Changed lines only, in unified-diff style, capped for the UI."""
    max_lines = int(CFG.get("server.diff_display_max_lines", 12))
    out: list[str] = []
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=0
    ):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(line)
        if len(out) >= max_lines:
            out.append("… (more changes)")
            break
    return "\n".join(out)
