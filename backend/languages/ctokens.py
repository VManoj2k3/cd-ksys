"""Shared tokenizer for C-family syntax (C, C++, Java, TypeScript, JS).

Extracts identifiers, // and /* */ comments, and string literals with
1-based line numbers, for the spell layer. Regex-based: robust on broken
code, never raises.
"""
from __future__ import annotations

import re

from backend.languages.base import SpellToken

_TOKEN_RE = re.compile(
    r"""
    (?P<block_comment>/\*.*?\*/)
  | (?P<line_comment>//[^\n]*)
  | (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
  | (?P<identifier>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE | re.DOTALL,
)

# keywords across the C family — never spell-checked as identifiers
_KEYWORDS = frozenset("""
auto break case char const continue default do double else enum extern float
for goto if inline int long register restrict return short signed sizeof static
struct switch typedef union unsigned void volatile while bool true false class
public private protected virtual override final new delete this nullptr
namespace using template typename operator friend explicit constexpr noexcept
throw try catch import export from function var let async await yield typeof
instanceof interface implements extends package abstract boolean byte
synchronized transient native strictfp assert super null undefined number
string any never unknown readonly declare module type of in is as
""".split())


def c_family_tokens(code: str) -> list[SpellToken]:
    out: list[SpellToken] = []
    for m in _TOKEN_RE.finditer(code):
        kind = m.lastgroup or ""
        text = m.group(0)
        line = code.count("\n", 0, m.start()) + 1
        if kind == "identifier":
            if text.lower() in _KEYWORDS or len(text) < 3:
                continue
            out.append(SpellToken("identifier", text, line))
        elif kind in ("block_comment", "line_comment"):
            out.append(SpellToken("comment", text, line))
        elif kind == "string":
            out.append(SpellToken("string", text, line))
    return out
