"""Deterministic magic-number scanner for C-family code.

Accurate where the LLM is not: finds raw numeric literals used directly in
executable code, and correctly SKIPS
  - literals inside #define / preprocessor lines (that IS the named constant)
  - literals in comments and string/char literals
  - named constants and macros (they are not literals at all)
  - an allow-list of trivial values (0, 1, ...)
This is how "no hardcoding" is enforced precisely, instead of the LLM guessing.
"""
from __future__ import annotations

import re

from backend.app_config import CFG
from backend.models import Layer, Severity, Violation

# integer / hex / float literal with optional C suffixes (U/L/F)
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])"
                     r"(0[xX][0-9a-fA-F]+|\d+\.\d+|\.\d+|\d+)"
                     r"[uUlLfF]*"
                     r"(?![A-Za-z0-9_.])")


def _strip_comments_strings(code: str) -> str:
    """Blank out comments and string/char literals, preserving newlines and
    length so line numbers stay correct."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        two = code[i:i + 2]
        if two == "/*":
            j = code.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in code[i:j]))
            i = j
        elif two == "//":
            j = code.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif c in "\"'":
            q, j = c, i + 1
            while j < n and code[j] != q:
                if code[j] == "\\":
                    j += 1
                j += 1
            j = min(j + 1, n)
            out.append("".join("\n" if ch == "\n" else " " for ch in code[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan_magic_numbers(code: str, lang: str) -> list[Violation]:
    if not CFG.get(f"{lang}.magic_numbers.enabled", True):
        return []
    allowed = {str(x) for x in CFG.get(f"{lang}.magic_numbers.allowed",
                                       [0, 1, 2, -1])}
    raw_lines = code.split("\n")
    clean = _strip_comments_strings(code).split("\n")

    violations: list[Violation] = []
    counter = 0
    for idx, line in enumerate(clean, 1):
        stripped = line.strip()
        # skip preprocessor lines — #define/#if is where constants are DEFINED
        if stripped.startswith("#"):
            continue
        for m in _NUM_RE.finditer(line):
            digits = m.group(1)
            # normalize hex/int for the allow check
            norm = digits
            try:
                val = int(digits, 0)
                norm = str(val)
            except ValueError:
                norm = digits
            if norm in allowed or digits in allowed:
                continue
            # skip array-index/bit positions that are clearly 0/1 already handled;
            # report the rest as hardcoded magic numbers
            counter += 1
            src = raw_lines[idx - 1].strip() if idx - 1 < len(raw_lines) else ""
            violations.append(Violation(
                id=f"magic-{counter}", layer=Layer.HARDCODE, rule="magic-number",
                severity=Severity.LOW, line=idx, col=m.start() + 1,
                snippet=src[:200],
                message=f"Hardcoded numeric literal {m.group(0)} in executable code",
                suggestion="Replace with a named constant (#define / const / enum)",
                # naming a magic number is a domain decision — an auto-generated
                # #define could invent a wrong/undefined name and (these files
                # don't parse standalone, so the syntax gate can't catch it)
                # break the build. Advise, don't fabricate a fix.
                no_autofix=True,
            ))
    return violations
