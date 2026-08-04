"""C and C++ plugins.

Deterministic layers:
- cppcheck (error/warning/style/performance/portability, XML output)
- clang-tidy with CERT + C++ Core Guidelines + bugprone checks (heavy AUTOSAR
  overlap; standalone mode, no compile DB required)
- flawfinder (C/C++ security: unsafe libc, buffer overflow, format string)

Fix gate: gcc/g++ -fsyntax-only.
AUTOSAR mode (config c_cpp.ruleset: autosar): a curated AUTOSAR C++14 rule
subset is injected into the LLM review prompt. This is AUTOSAR-GUIDED review,
not certified compliance checking.
"""
from __future__ import annotations

import csv
import io
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.app_config import CFG, PROJECT_ROOT
from backend.languages.base import LanguagePlugin, SpellToken
from backend.languages.ctokens import c_family_tokens
from backend.models import Layer, Severity, Violation

_CPPCHECK_SKIP_IDS = {
    "checkersReport", "missingInclude", "missingIncludeSystem",
    "unmatchedSuppression", "normalCheckLevelMaxBranches", "unusedFunction",
    "information", "noValidConfiguration",
    # style-level noise that fires on correct code (zero-FP priority)
    "constVariable", "constVariablePointer", "constParameter",
    "constParameterPointer", "constParameterReference", "useStlAlgorithm",
    "useInitializationList", "passedByValue", "unusedStructMember",
    # analysis-limitation artifacts on standalone files (NOT code bugs):
    # macros from headers we can't see, encoding bytes, missing includes.
    # Real syntax is validated separately by the gcc/g++ gate.
    "unknownMacro", "syntaxError", "preprocessorErrorDirective",
    "toomanyconfigs", "internalAstError", "cppcheckError",
}
_CPPCHECK_SEV = {
    "error": Severity.HIGH, "warning": Severity.MEDIUM,
    "performance": Severity.LOW, "portability": Severity.LOW,
    "style": Severity.LOW,
}
_TIDY_LINE_RE = re.compile(
    r"^.*?:(\d+):(\d+):\s+(warning|error):\s+(.*?)\s+\[([^\]]+)\]\s*$")


class _CFamilyPlugin(LanguagePlugin):
    std_flag: str = ""
    compiler: str = ""
    gcc_lang: str = ""

    # ---------------- lint: cppcheck + clang-tidy ----------------
    def lint(self, code: str, filename: str) -> list[Violation]:
        out = self._cppcheck(code, filename)
        if CFG.get(f"{self.name}.clang_tidy.enabled", True):
            out += self._clang_tidy(code, filename, start_id=len(out) + 1)
        return out

    def _cppcheck(self, code: str, filename: str) -> list[Violation]:
        if not CFG.get(f"{self.name}.cppcheck.enabled", True):
            return []
        lines = code.splitlines()
        enable = CFG.get(f"{self.name}.cppcheck.enable", "warning,portability")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / self.tmp_name(filename)
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                ["cppcheck", f"--enable={enable}",
                 "--inline-suppr", "--xml", "--xml-version=2", str(f)],
                capture_output=True, text=True,
            )
        violations: list[Violation] = []
        try:
            root = ET.fromstring(proc.stderr or "<results/>")
        except ET.ParseError:
            return []
        i = 0
        for err in root.iter("error"):
            eid = err.get("id", "")
            sev = err.get("severity", "style")
            if eid in _CPPCHECK_SKIP_IDS or sev == "information":
                continue
            loc = err.find("location")
            if loc is None:
                continue
            line = int(loc.get("line", "1") or 1)
            i += 1
            violations.append(Violation(
                id=f"cppcheck-{i}", layer=Layer.LINT, rule=eid,
                severity=_CPPCHECK_SEV.get(sev, Severity.LOW),
                line=line, col=int(loc.get("column", "0") or 0) or None,
                snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                message=err.get("msg", ""),
            ))
        return violations

    def _clang_tidy(self, code: str, filename: str, start_id: int) -> list[Violation]:
        checks = ",".join(CFG.get(f"{self.name}.clang_tidy.checks", [
            "bugprone-*", "cert-*", "cppcoreguidelines-*",
            "-cppcoreguidelines-avoid-magic-numbers",
        ]))
        lines = code.splitlines()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / self.tmp_name(filename)
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                ["clang-tidy", str(f), f"-checks=-*,{checks}", "--quiet",
                 "--", self.std_flag, "-w"],
                capture_output=True, text=True, timeout=120,
            )
        violations: list[Violation] = []
        seen: set[tuple[int, str]] = set()
        i = start_id
        for raw in (proc.stdout or "").splitlines():
            m = _TIDY_LINE_RE.match(raw.strip())
            if not m:
                continue
            line, _col, kind, msg, check = (
                int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5))
            if (line, check) in seen or check.startswith("clang-diagnostic"):
                continue
            seen.add((line, check))
            violations.append(Violation(
                id=f"tidy-{i}", layer=Layer.LINT, rule=check,
                severity=Severity.MEDIUM if kind == "warning" else Severity.HIGH,
                line=line,
                snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                message=msg,
            ))
            i += 1
        return violations

    # ---------------- security: flawfinder ----------------
    def security(self, code: str, filename: str) -> list[Violation]:
        if not CFG.get(f"{self.name}.flawfinder.enabled", True):
            return []
        min_level = int(CFG.get(f"{self.name}.flawfinder.min_level", 2))
        skip_phrases = CFG.get(f"{self.name}.flawfinder.skip_warnings", [
            "Statically-sized arrays",  # every fixed-size buffer fires this
        ])
        lines = code.splitlines()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / self.tmp_name(filename)
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                ["flawfinder", "--csv", str(f)], capture_output=True, text=True)
        violations: list[Violation] = []
        for i, row in enumerate(csv.DictReader(io.StringIO(proc.stdout or "")), 1):
            try:
                level = int(row.get("Level", "0"))
                line = int(row.get("Line", "1"))
            except ValueError:
                continue
            if level < min_level:
                continue
            if any(p in row.get("Warning", "") for p in skip_phrases):
                continue
            sev = (Severity.CRITICAL if level >= 4
                   else Severity.HIGH if level == 3 else Severity.MEDIUM)
            violations.append(Violation(
                id=f"flawfinder-{i}", layer=Layer.SECURITY,
                rule=f"FF-{row.get('Name', 'unknown')}", severity=sev, line=line,
                snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                message=f"{row.get('Category', '')}: {row.get('Warning', '')}",
                suggestion=row.get("Suggestion", ""),
            ))
        return violations

    # ---------------- shared bits ----------------
    def spell_tokens(self, code: str) -> list[SpellToken]:
        return c_family_tokens(code)

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / f"check{self.extensions[0]}"
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                [self.compiler, "-fsyntax-only", "-w", self.std_flag,
                 "-x", self.gcc_lang, str(f)],
                capture_output=True, text=True, timeout=60,
            )
        if proc.returncode == 0:
            return True, f"{self.compiler} -fsyntax-only OK"
        return False, (proc.stderr or "compile check failed").strip()[:300]

    def llm_categories(self) -> list[str]:
        return CFG.get(f"{self.name}.llm_categories", [
            "memory_safety", "undefined_behavior", "resource_leak",
            "concurrency", "security", "api_misuse", "error_handling",
            "logic_bug",
        ])

    def llm_extra_rules(self) -> str:
        if CFG.get(f"{self.name}.ruleset", "") != "autosar":
            return ""
        path = PROJECT_ROOT / CFG.get("paths.prompts_dir", "prompts") / "autosar_subset.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class CPlugin(_CFamilyPlugin):
    name = "c"
    display = "C"
    extensions = (".c", ".h")
    compiler = "gcc"
    gcc_lang = "c"

    @property
    def std_flag(self) -> str:  # type: ignore[override]
        return f"-std={CFG.get('c.std', 'c11')}"


class CppPlugin(_CFamilyPlugin):
    name = "cpp"
    display = "C++"
    extensions = (".cpp", ".cc", ".cxx", ".hpp", ".hh")
    compiler = "g++"
    gcc_lang = "c++"

    @property
    def std_flag(self) -> str:  # type: ignore[override]
        return f"-std={CFG.get('cpp.std', 'c++14')}"  # AUTOSAR targets C++14
