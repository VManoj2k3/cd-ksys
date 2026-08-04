"""TypeScript / JavaScript plugin.

Deterministic layers:
- ESLint flat config: @eslint/js recommended + typescript-eslint recommended
  + eslint-plugin-security (security/* rules are routed to the SECURITY layer)
- Fix gate: tsc --noEmit per-file syntax/parse check

Node toolchain is installed once into paths.tools_dir/eslint (npm; the Kaggle
bootstrap pre-installs it).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from backend.app_config import CFG, PROJECT_ROOT
from backend.languages.base import LanguagePlugin, SpellToken
from backend.languages.ctokens import c_family_tokens
from backend.models import Layer, Severity, Violation

_ESLINT_CONFIG = """\
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import security from "eslint-plugin-security";

export default [
  js.configs.recommended,
  ...tseslint.configs.recommended,
  security.configs.recommended,
  {
    languageOptions: { globals: { console: "readonly", process: "readonly",
      require: "readonly", module: "readonly", window: "readonly",
      document: "readonly", setTimeout: "readonly", fetch: "readonly" } },
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
];
"""


def tools_dir() -> Path:
    base = CFG.get("paths.tools_dir", "")
    root = Path(base) if base else PROJECT_ROOT / ".tools"
    return root / "eslint"


def ensure_eslint() -> Path | None:
    """Install the ESLint toolchain once; returns its dir or None."""
    d = tools_dir()
    eslint_bin = d / "node_modules" / ".bin" / "eslint"
    cfg = d / "eslint.config.mjs"
    if not cfg.exists():
        d.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_ESLINT_CONFIG, encoding="utf-8")
        (d / "package.json").write_text('{"name":"koosys-tools","type":"module"}',
                                        encoding="utf-8")
    if not eslint_bin.exists():
        proc = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--silent",
             "eslint", "@eslint/js", "typescript", "typescript-eslint",
             "eslint-plugin-security"],
            cwd=d, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0 or not eslint_bin.exists():
            return None
    return d


class TypeScriptPlugin(LanguagePlugin):
    name = "typescript"
    display = "TypeScript/JS"
    extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs")

    def _run_eslint(self, code: str, filename: str) -> list[dict]:
        d = ensure_eslint()
        if d is None:
            return []
        # eslint flat config ignores files outside its base path, so the temp
        # file must live under the tools dir
        scratch = d / "scratch"
        scratch.mkdir(exist_ok=True)
        ext = Path(filename or "snippet.ts").suffix or ".ts"
        f = scratch / f"snippet{ext}"
        f.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [str(d / "node_modules" / ".bin" / "eslint"),
                 "--config", str(d / "eslint.config.mjs"),
                 "--format", "json", "--no-warn-ignored", str(f)],
                cwd=d, capture_output=True, text=True, timeout=120,
            )
            try:
                results = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError:
                return []
            return results[0].get("messages", []) if results else []
        finally:
            f.unlink(missing_ok=True)

    def _messages_to_violations(self, code: str, msgs: list[dict],
                                want_security: bool) -> list[Violation]:
        lines = code.splitlines()
        out: list[Violation] = []
        i = 0
        for m in msgs:
            rule = m.get("ruleId") or "parse-error"
            is_sec = rule.startswith("security/")
            if is_sec != want_security:
                continue
            line = int(m.get("line", 1) or 1)
            i += 1
            layer = Layer.SECURITY if is_sec else Layer.LINT
            sev = (Severity.MEDIUM if is_sec
                   else Severity.HIGH if rule == "parse-error"
                   else Severity.MEDIUM if m.get("severity") == 2 else Severity.LOW)
            out.append(Violation(
                id=f"eslint{'-sec' if is_sec else ''}-{i}", layer=layer,
                rule=rule, severity=sev, line=line, col=m.get("column"),
                snippet=(lines[line - 1].strip() if 0 < line <= len(lines) else ""),
                message=m.get("message", ""),
            ))
        return out

    def lint(self, code: str, filename: str) -> list[Violation]:
        self._msg_cache = self._run_eslint(code, filename)
        return self._messages_to_violations(code, self._msg_cache, want_security=False)

    def security(self, code: str, filename: str) -> list[Violation]:
        msgs = getattr(self, "_msg_cache", None)
        if msgs is None:
            msgs = self._run_eslint(code, filename)
        return self._messages_to_violations(code, msgs, want_security=True)

    def spell_tokens(self, code: str) -> list[SpellToken]:
        return c_family_tokens(code)

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        d = ensure_eslint()
        if d is None:
            return True, "tsc unavailable — syntax not verified"
        tsc = d / "node_modules" / ".bin" / "tsc"
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "check.ts"
            f.write_text(code, encoding="utf-8")
            proc = subprocess.run(
                [str(tsc), "--noEmit", "--skipLibCheck", "--allowJs",
                 "--target", "es2020", str(f)],
                cwd=d, capture_output=True, text=True, timeout=120,
            )
        # only parse-level errors (TS1xxx) gate fixes; type errors don't
        parse_errors = [ln for ln in (proc.stdout or "").splitlines()
                        if "error TS1" in ln]
        if parse_errors:
            return False, parse_errors[0][:200]
        return True, "tsc parse OK"

    def llm_categories(self) -> list[str]:
        return CFG.get("typescript.llm_categories", [
            "logic_bug", "security", "api_misuse", "resource_leak",
            "concurrency", "error_handling",
        ])
