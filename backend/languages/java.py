"""Java plugin.

Deterministic layers:
- javalang AST checks (always available): empty catch blocks, string
  comparison with ==, unused single-type imports
- PMD quickstart ruleset (when the pmd binary is available — the Kaggle
  bootstrap downloads it from Maven Central; config java.pmd.path)

Fix gate: javalang full-file parse.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from backend.app_config import CFG
from backend.languages.base import LanguagePlugin, SpellToken
from backend.languages.ctokens import c_family_tokens
from backend.models import Layer, Severity, Violation

_PMD_SEV = {1: Severity.HIGH, 2: Severity.MEDIUM, 3: Severity.MEDIUM,
            4: Severity.LOW, 5: Severity.INFO}


def _find_pmd() -> str | None:
    configured = CFG.get("java.pmd.path", "")
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("pmd")
    if found:
        return found
    for cand in sorted(Path("/kaggle/working/tools").glob("pmd-bin-*/bin/pmd")) \
            if Path("/kaggle/working/tools").exists() else []:
        return str(cand)
    return None


class JavaPlugin(LanguagePlugin):
    name = "java"
    display = "Java"
    extensions = (".java",)

    # ---------------- lint: javalang AST checks + PMD ----------------
    def lint(self, code: str, filename: str) -> list[Violation]:
        out = self._ast_checks(code)
        if CFG.get("java.pmd.enabled", True):
            out += self._pmd(code, filename, start_id=len(out) + 1)
        return out

    def _ast_checks(self, code: str) -> list[Violation]:
        """Token-based checks — javalang node positions are unreliable, but
        the tokenizer gives an exact (line, column) for every token."""
        import javalang

        lines = code.splitlines()
        violations: list[Violation] = []
        counter = 0

        def add(line: int, rule: str, sev: Severity, msg: str, sugg: str = ""):
            nonlocal counter
            counter += 1
            violations.append(Violation(
                id=f"jast-{counter}", layer=Layer.LINT, rule=rule, severity=sev,
                line=line,
                snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                message=msg, suggestion=sugg,
            ))

        try:
            toks = list(javalang.tokenizer.tokenize(code))
        except Exception:  # noqa: BLE001 — unparseable: skip
            return violations

        def is_string(t) -> bool:
            return type(t).__name__ == "String"

        for i, tok in enumerate(toks):
            val = tok.value
            line = tok.position[0] if tok.position else 1

            # empty catch: catch (...) { } — tokenizer drops comments, so a
            # comment-only body still collapses to adjacent { }
            if val == "catch":
                j = i + 1
                depth = 0
                while j < len(toks):  # skip the (...) parameter list
                    if toks[j].value == "(":
                        depth += 1
                    elif toks[j].value == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                if j < len(toks) - 1 and toks[j].value == "{" and toks[j + 1].value == "}":
                    add(line, "empty-catch", Severity.MEDIUM,
                        "Empty catch block silently swallows the exception",
                        "Log, rethrow, or handle the exception")

            # String reference comparison: "str" == x  or  x == "str"
            elif val in ("==", "!="):
                prev_t = toks[i - 1] if i > 0 else None
                next_t = toks[i + 1] if i + 1 < len(toks) else None
                if (prev_t and is_string(prev_t)) or (next_t and is_string(next_t)):
                    add(line, "string-ref-compare", Severity.HIGH,
                        "String compared with == / != (reference comparison, not value)",
                        "Use .equals() / !a.equals(b)")

        # unused single-type imports via AST (imports do carry positions)
        try:
            tree = javalang.parse.parse(code)
        except Exception:  # noqa: BLE001
            return violations
        body_text = "\n".join(
            ln for ln in lines if not ln.strip().startswith("import "))
        for imp in tree.imports:
            if imp.wildcard or imp.static:
                continue
            simple = imp.path.rsplit(".", 1)[-1]
            if not re.search(rf"\b{re.escape(simple)}\b", body_text):
                iline = imp.position.line if getattr(imp, "position", None) else 1
                add(iline, "unused-import", Severity.LOW,
                    f"Import '{imp.path}' appears unused",
                    "Remove the unused import")
        return violations

    def _pmd(self, code: str, filename: str, start_id: int) -> list[Violation]:
        pmd = _find_pmd()
        if not pmd:
            return []
        ruleset = CFG.get("java.pmd.ruleset", "rulesets/java/quickstart.xml")
        lines = code.splitlines()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / self.tmp_name(filename)
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                [pmd, "check", "-d", str(f), "-R", ruleset, "-f", "json",
                 "--no-cache", "--no-progress"],
                capture_output=True, text=True, timeout=180,
            )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        violations: list[Violation] = []
        i = start_id
        for file_entry in payload.get("files", []):
            for v in file_entry.get("violations", []):
                line = int(v.get("beginline", 1))
                violations.append(Violation(
                    id=f"pmd-{i}", layer=Layer.LINT, rule=v.get("rule", "pmd"),
                    severity=_PMD_SEV.get(int(v.get("priority", 3)), Severity.LOW),
                    line=line,
                    snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                    message=v.get("description", ""),
                ))
                i += 1
        return violations

    # ---------------- shared bits ----------------
    def spell_tokens(self, code: str) -> list[SpellToken]:
        return c_family_tokens(code)

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        import javalang

        try:
            javalang.parse.parse(code)
            return True, "javalang parse OK"
        except Exception as exc:  # noqa: BLE001
            return False, f"parse error: {str(exc)[:200]}"

    def llm_categories(self) -> list[str]:
        return CFG.get("java.llm_categories", [
            "logic_bug", "security", "api_misuse", "resource_leak",
            "concurrency", "error_handling",
        ])
