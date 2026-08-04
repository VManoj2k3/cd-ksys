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

    def tmp_name(self, filename: str) -> str:
        """Temp filename guaranteed to carry a valid extension for this
        language's tools — routing may pick a plugin whose extension the
        supplied filename doesn't match (e.g. pasted code named snippet.py)."""
        from pathlib import Path

        p = Path(filename or "")
        if p.suffix.lower() in self.extensions and p.name:
            return p.name
        return f"snippet{self.extensions[0]}"


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
