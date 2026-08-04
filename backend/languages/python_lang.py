"""Python plugin — wraps the existing ruff/bandit/tokenizer/ast machinery."""
from __future__ import annotations

import ast
import io
import tokenize

from backend.app_config import CFG
from backend.languages.base import LanguagePlugin, SpellToken
from backend.layers.static_lint import run_bandit, run_ruff
from backend.models import Violation


class PythonPlugin(LanguagePlugin):
    name = "python"
    display = "Python"
    extensions = (".py",)

    def lint(self, code: str, filename: str) -> list[Violation]:
        out = run_ruff(code, filename)
        # collapse a syntax-error flood into ONE finding — a non-Python or
        # badly broken file otherwise yields hundreds of 'invalid-syntax' rows
        syn = [v for v in out if v.rule in ("invalid-syntax", "E999")]
        if len(syn) > 3:
            # file doesn't parse as Python — every other ruff finding is noise
            first = syn[0]
            first.message = (f"File does not parse as Python ({len(syn)} syntax "
                             f"errors). If this isn't Python, pick the correct "
                             f"language from the dropdown.")
            return [first]
        return out

    def security(self, code: str, filename: str) -> list[Violation]:
        return run_bandit(code, filename)

    def spell_tokens(self, code: str) -> list[SpellToken]:
        out: list[SpellToken] = []
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
            return out
        for tok in tokens:
            if tok.type == tokenize.NAME:
                out.append(SpellToken("identifier", tok.string, tok.start[0], tok.start[1]))
            elif tok.type == tokenize.COMMENT:
                out.append(SpellToken("comment", tok.string, tok.start[0], tok.start[1]))
            elif tok.type == tokenize.STRING:
                out.append(SpellToken("string", tok.string, tok.start[0], tok.start[1]))
        return out

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        try:
            ast.parse(code)
            return True, "AST parse OK"
        except SyntaxError as exc:
            return False, f"syntax error: {exc}"

    def llm_categories(self) -> list[str]:
        return CFG.get("review.llm_categories_python",
                       CFG.get("review.llm_categories", []))
