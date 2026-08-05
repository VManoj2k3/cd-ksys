"""Language plugin protocol + registry.

Each language supplies: a lint layer, a security layer, spell tokens,
syntax validation (used to gate every fix), and LLM review parameters.
Everything else — LLM review/verify, anchoring, fix engine, UI — is shared.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.app_config import CFG
from backend.models import Violation


@dataclass
class SpellToken:
    kind: str          # "identifier" | "comment" | "string"
    text: str
    line: int          # 1-based line of the token start
    col: int = 0


class LanguagePlugin:
    name: str = "base"
    display: str = "base"
    extensions: tuple[str, ...] = ()

    def lint(self, code: str, filename: str) -> list[Violation]:
        return []

    def security(self, code: str, filename: str) -> list[Violation]:
        return []

    def spell_tokens(self, code: str) -> list[SpellToken]:
        return []

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        """Gate for fixes: patched code must still parse/compile."""
        return True, "no syntax validator for this language"

    def llm_categories(self) -> list[str]:
        return CFG.get("review.llm_categories", [])

    def llm_extra_rules(self) -> str:
        """Extra ruleset text injected into the review prompt (e.g. AUTOSAR)."""
        return ""

    def enclosing_functions(self, code: str) -> list[tuple[str, int, int]]:
        """Return [(name, start_line, end_line)] for functions/methods, so
        the UI can group findings by function. Default: brace-based scan for
        the C family (C/C++/Java/TS). Python overrides with AST."""
        return c_family_functions(code)

    def tmp_name(self, filename: str) -> str:
        """Temp filename guaranteed to carry a valid extension for this
        language's tools — routing may pick a plugin whose extension the
        supplied filename doesn't match (e.g. pasted code named snippet.py)."""
        from pathlib import Path

        p = Path(filename or "")
        if p.suffix.lower() in self.extensions and p.name:
            return p.name
        return f"snippet{self.extensions[0]}"


_FUNC_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "do", "else", "return", "sizeof",
    "typedef", "struct", "enum", "union", "class", "namespace", "case",
    "default", "new", "delete", "throw", "template",
})


def c_family_functions(code: str) -> list[tuple[str, int, int]]:
    """Brace-based function/method finder for C-family languages. Matches
    `name(params) {` (name not a control keyword) at any nesting depth, then
    brace-matches to the end. Robust on real code; never raises."""
    import bisect
    import re as _re

    n = len(code)
    line_starts = [0]
    for idx, ch in enumerate(code):
        if ch == "\n":
            line_starts.append(idx + 1)

    def line_of(pos: int) -> int:
        return bisect.bisect_right(line_starts, pos)

    funcs: list[tuple[str, int, int]] = []
    for m in _re.finditer(r"([A-Za-z_]\w*)\s*\(", code):
        name = m.group(1)
        if name in _FUNC_KEYWORDS:
            continue
        # match the parameter parens
        p = m.end() - 1
        d = 0
        j = p
        while j < n:
            if code[j] == "(":
                d += 1
            elif code[j] == ")":
                d -= 1
                if d == 0:
                    break
            j += 1
        if j >= n:
            continue
        # after ')' expect '{' before ';' (a definition, not a declaration/call)
        seg = code[j + 1: j + 160]
        brace = seg.find("{")
        semi = seg.find(";")
        if brace == -1 or (semi != -1 and semi < brace):
            continue
        open_brace = j + 1 + brace
        d = 0
        r = open_brace
        while r < n:
            if code[r] == "{":
                d += 1
            elif code[r] == "}":
                d -= 1
                if d == 0:
                    break
            r += 1
        if r >= n:
            continue
        funcs.append((name, line_of(m.start()), line_of(r)))
    return funcs


def assign_functions(violations, functions) -> None:
    """Set each violation's `.function` to the smallest enclosing function."""
    for v in violations:
        best = None
        best_span = None
        for name, s, e in functions:
            if s <= v.line <= e:
                span = e - s
                if best_span is None or span < best_span:
                    best, best_span = name, span
        if best:
            v.function = best


_REGISTRY: list[LanguagePlugin] = []


def _plugins() -> list[LanguagePlugin]:
    if _REGISTRY:
        return _REGISTRY
    from backend.languages.c_cpp import CPlugin, CppPlugin
    from backend.languages.java import JavaPlugin
    from backend.languages.python_lang import PythonPlugin
    from backend.languages.typescript import TypeScriptPlugin

    enabled = set(CFG.get("languages.enabled", []))
    for plugin in (PythonPlugin(), CPlugin(), CppPlugin(), JavaPlugin(),
                   TypeScriptPlugin()):
        if plugin.name in enabled:
            _REGISTRY.append(plugin)
    return _REGISTRY


def plugin_for(filename: str) -> LanguagePlugin | None:
    ext = Path(filename or "").suffix.lower()
    for plugin in _plugins():
        if ext in plugin.extensions:
            return plugin
    return None


# UI dropdown value -> plugin name
_UI_LANG_MAP = {
    "py": "python", "python": "python",
    "c": "c", "h": "c",
    "cpp": "cpp", "c++": "cpp",
    "java": "java",
    "ts": "typescript", "tsx": "typescript", "typescript": "typescript",
    "js": "typescript", "jsx": "typescript", "javascript": "typescript",
}


def plugin_by_language(name: str) -> LanguagePlugin | None:
    """Resolve an explicit language selection (UI dropdown) to a plugin."""
    target = _UI_LANG_MAP.get((name or "").strip().lower())
    if target is None:
        return None
    for plugin in _plugins():
        if plugin.name == target:
            return plugin
    return None


def all_extensions() -> list[str]:
    out: list[str] = []
    for plugin in _plugins():
        out.extend(plugin.extensions)
    return out


def language_names() -> list[str]:
    return [p.name for p in _plugins()]
