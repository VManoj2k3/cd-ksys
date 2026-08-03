"""L1 — Deterministic lint layer: ruff (style/correctness) + bandit (security).

These tools are AST-based: their findings carry exact line numbers and are
zero-hallucination by construction. Ruff's own machine-generated edits are
applied for fixes when available (and re-validated with ast.parse).
"""
from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from pathlib import Path

from backend.app_config import CFG
from backend.models import Fix, Layer, Severity, Violation

_SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH"]


def _ruff_severity(code: str) -> Severity:
    if code.startswith("E9") or code in ("F821", "F811", "F823"):
        return Severity.HIGH
    if code.startswith(("F", "B", "PLE")):
        return Severity.MEDIUM
    return Severity.LOW


def _apply_ruff_edits(code: str, edits: list[dict]) -> str | None:
    """Apply ruff JSON edits (1-based row/col) to source text."""
    lines = code.splitlines(keepends=True)
    # compute absolute offsets
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    def pos(row: int, col: int) -> int:
        if row - 1 >= len(offsets) - 1:
            return offsets[-1]
        return offsets[row - 1] + (col - 1)

    try:
        for edit in sorted(
            edits,
            key=lambda e: (e["location"]["row"], e["location"]["column"]),
            reverse=True,
        ):
            start = pos(edit["location"]["row"], edit["location"]["column"])
            end = pos(edit["end_location"]["row"], edit["end_location"]["column"])
            code = code[:start] + (edit.get("content") or "") + code[end:]
        return code
    except (KeyError, IndexError):
        return None


def run_ruff(code: str, filename: str) -> list[Violation]:
    if not CFG.get("lint.ruff.enabled", True):
        return []
    select = CFG.get("lint.ruff.select", [])
    ignore = CFG.get("lint.ruff.ignore", [])
    lines = code.splitlines()

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / (filename or "snippet.py")
        f.write_text(code, encoding="utf-8")
        cmd = ["ruff", "check", "--output-format", "json", "--no-cache", "--isolated"]
        if select:
            cmd += ["--select", ",".join(select)]
        if ignore:
            cmd += ["--ignore", ",".join(ignore)]
        cmd.append(str(f))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        try:
            results = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []

    violations: list[Violation] = []
    for i, r in enumerate(results, 1):
        row = r.get("location", {}).get("row", 1)
        fix = None
        rf = r.get("fix")
        if rf and rf.get("edits"):
            patched = _apply_ruff_edits(code, rf["edits"])
            if patched is not None:
                try:
                    ast.parse(patched)
                    from backend.diffutil import display_diff

                    fix = Fix(
                        start_line=row, end_line=r.get("end_location", {}).get("row", row),
                        replacement=display_diff(code, patched),
                        file_wide=True, fixed_code=patched,
                        validated=True,
                        validation_notes=f"ruff auto-fix ({rf.get('applicability', 'unknown')}); AST parse OK",
                    )
                except SyntaxError:
                    fix = None
        violations.append(Violation(
            id=f"ruff-{i}", layer=Layer.LINT, rule=r.get("code") or "ruff",
            severity=_ruff_severity(r.get("code") or ""),
            line=row, col=r.get("location", {}).get("column"),
            snippet=(lines[row - 1].strip() if 0 < row <= len(lines) else ""),
            message=r.get("message", ""),
            suggestion=(rf or {}).get("message") or "",
            fix=fix,
        ))
    return violations


def run_bandit(code: str, filename: str) -> list[Violation]:
    if not CFG.get("lint.bandit.enabled", True):
        return []
    min_sev = str(CFG.get("lint.bandit.min_severity", "LOW")).upper()
    min_conf = str(CFG.get("lint.bandit.min_confidence", "MEDIUM")).upper()
    lines = code.splitlines()

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / (filename or "snippet.py")
        f.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            ["bandit", "-f", "json", "-q", str(f)], capture_output=True, text=True
        )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []

    sev_map = {"LOW": Severity.LOW, "MEDIUM": Severity.MEDIUM, "HIGH": Severity.HIGH}
    violations: list[Violation] = []
    for i, r in enumerate(payload.get("results", []), 1):
        sev = str(r.get("issue_severity", "LOW")).upper()
        conf = str(r.get("issue_confidence", "LOW")).upper()
        if _SEVERITY_ORDER.index(sev) < _SEVERITY_ORDER.index(min_sev):
            continue
        if _SEVERITY_ORDER.index(conf) < _SEVERITY_ORDER.index(min_conf):
            continue
        row = r.get("line_number", 1)
        violations.append(Violation(
            id=f"bandit-{i}", layer=Layer.SECURITY, rule=r.get("test_id") or "bandit",
            severity=sev_map.get(sev, Severity.MEDIUM),
            line=row, col=r.get("col_offset"),
            snippet=(lines[row - 1].strip() if 0 < row <= len(lines) else ""),
            message=f"{r.get('test_name', '')}: {r.get('issue_text', '')}",
            suggestion=r.get("issue_cwe", {}).get("link", "") if isinstance(r.get("issue_cwe"), dict) else "",
        ))
    return violations
