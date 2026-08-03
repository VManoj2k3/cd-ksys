"""L0 — Spell layer.

Dictionary-based (codespell dictionary): only *known* misspellings are
flagged, which keeps false positives near zero. Identifiers are split on
snake_case and camelCase boundaries so `recieveData` is caught, while
legitimate abbreviations (`cnt`, `idx`) are never flagged because they are
not in the misspelling dictionary.

Fixes:
- identifier misspelling -> file-wide rename of that identifier (all
  references), validated with ast.parse
- comment/string misspelling -> single-line word replacement
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from functools import lru_cache
from pathlib import Path

from backend.app_config import CFG
from backend.models import Fix, Layer, Severity, Violation

_WORD_RE = re.compile(r"[A-Za-z]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


@lru_cache(maxsize=1)
def _dictionary() -> dict[str, tuple[list[str], bool]]:
    """word -> (suggestions, autofix_ok). Lazy-loaded from codespell data."""
    import codespell_lib

    data = Path(codespell_lib.__file__).parent / "data" / "dictionary.txt"
    table: dict[str, tuple[list[str], bool]] = {}
    for raw in data.read_text(encoding="utf-8").splitlines():
        if "->" not in raw:
            continue
        wrong, correction = raw.split("->", 1)
        wrong = wrong.strip().lower()
        ambiguous = correction.endswith(",")
        parts = [p.strip() for p in correction.split(",") if p.strip()]
        if not wrong or not parts:
            continue
        autofix = (len(parts) == 1) and not ambiguous
        table[wrong] = (parts, autofix)
    return table


def _split_subwords(identifier: str) -> list[str]:
    out: list[str] = []
    for chunk in identifier.split("_"):
        out.extend(_CAMEL_RE.findall(chunk))
    return out


def _match_case(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _check_word(word: str, min_len: int, allow: set[str]) -> tuple[list[str], bool] | None:
    lw = word.lower()
    if len(lw) < min_len or lw in allow:
        return None
    return _dictionary().get(lw)


def run_spell_layer(code: str) -> list[Violation]:
    cfg = CFG
    if not cfg.get("spell.enabled", True):
        return []
    min_len = int(cfg.get("spell.min_word_length", 4))
    allow = {w.lower() for w in cfg.get("spell.allowlist", [])}
    single_only = bool(cfg.get("spell.autofix_single_suggestion_only", True))

    violations: list[Violation] = []
    seen_identifiers: set[str] = set()
    seen_words: set[tuple[int, str]] = set()
    lines = code.splitlines()

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        tokens = []

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"spell-{counter}"

    for tok in tokens:
        ttype, text, (srow, scol) = tok.type, tok.string, tok.start

        if ttype == tokenize.NAME and cfg.get("spell.check_identifiers", True):
            if text in seen_identifiers:
                continue
            for sub in _split_subwords(text):
                hit = _check_word(sub, min_len, allow)
                if not hit:
                    continue
                suggestions, autofix_ok = hit
                seen_identifiers.add(text)
                corrected_sub = _match_case(sub, suggestions[0])
                corrected_ident = text.replace(sub, corrected_sub)
                fix = None
                if autofix_ok or not single_only:
                    from backend.diffutil import display_diff

                    fixed_code = re.sub(rf"\b{re.escape(text)}\b", corrected_ident, code)
                    valid, note = _validate_python(fixed_code)
                    fix = Fix(
                        start_line=srow, end_line=srow,
                        replacement=display_diff(code, fixed_code), file_wide=True,
                        fixed_code=fixed_code if valid else None,
                        validated=valid, validation_notes=note,
                    )
                violations.append(Violation(
                    id=next_id(), layer=Layer.SPELL, rule="spell-identifier",
                    severity=Severity.LOW, line=srow, col=scol,
                    snippet=(lines[srow - 1].strip() if srow - 1 < len(lines) else text),
                    message=f"Identifier '{text}' contains misspelling '{sub}' -> {', '.join(suggestions)}",
                    suggestion=f"Rename to '{corrected_ident}' (all occurrences)",
                    fix=fix,
                ))
                break  # one report per identifier

        elif ttype in (tokenize.COMMENT, tokenize.STRING):
            if ttype == tokenize.COMMENT and not cfg.get("spell.check_comments", True):
                continue
            if ttype == tokenize.STRING and not cfg.get("spell.check_strings", True):
                continue
            for m in _WORD_RE.finditer(text):
                word = m.group(0)
                # split camelCase words inside comments/strings too
                for sub in _CAMEL_RE.findall(word):
                    hit = _check_word(sub, min_len, allow)
                    if not hit:
                        continue
                    suggestions, autofix_ok = hit
                    line_no = srow + text.count("\n", 0, m.start())
                    key = (line_no, sub.lower())
                    if key in seen_words:
                        continue
                    seen_words.add(key)
                    corrected = _match_case(sub, suggestions[0])
                    fix = None
                    if (autofix_ok or not single_only) and line_no - 1 < len(lines):
                        old_line = lines[line_no - 1]
                        new_line = re.sub(rf"\b{re.escape(sub)}\b", corrected, old_line)
                        if new_line != old_line:
                            fix = Fix(
                                start_line=line_no, end_line=line_no,
                                replacement=new_line, validated=True,
                                validation_notes="single-line word replacement",
                            )
                    kind = "comment" if ttype == tokenize.COMMENT else "string"
                    violations.append(Violation(
                        id=next_id(), layer=Layer.SPELL, rule=f"spell-{kind}",
                        severity=Severity.INFO, line=line_no,
                        snippet=(lines[line_no - 1].strip() if line_no - 1 < len(lines) else word),
                        message=f"Misspelling in {kind}: '{sub}' -> {', '.join(suggestions)}",
                        suggestion=f"Replace with '{corrected}'",
                        fix=fix,
                    ))
    return violations


def _validate_python(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code)
        return True, "renamed file-wide; AST parse OK"
    except SyntaxError as exc:  # pragma: no cover
        return False, f"rename produced syntax error: {exc}"
