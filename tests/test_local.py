"""Local verification of the deterministic layers (no GPU / LLM required).

Run from the project root:  python -m tests.test_local
Asserts:
- every planted violation in sample_bad.py is detected (no false negatives)
- sample_clean.py yields ZERO violations (no false positives)
- ruff auto-fixes and spell renames survive AST validation
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.layers.spell import run_spell_layer  # noqa: E402
from backend.layers.static_lint import run_bandit, run_ruff  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    bad = (ROOT / "tests" / "sample_bad.py").read_text()
    clean = (ROOT / "tests" / "sample_clean.py").read_text()

    print("== sample_bad.py — planted violations must be found ==")
    spell = run_spell_layer(bad)
    rules = [(v.rule, v.line) for v in spell]
    print(f"  spell found: {[(v.rule, v.line, v.message[:50]) for v in spell]}")
    check(any(v.rule == "spell-identifier" and "recieve" in v.message for v in spell),
          "spell: misspelled identifier 'recieve_data' detected")
    check(any("occured" in v.message for v in spell), "spell: 'occured' in comment detected")
    check(any("lenght" in v.message for v in spell), "spell: 'lenght' in comment detected")

    ident_fix = next((v.fix for v in spell if v.rule == "spell-identifier"), None)
    check(ident_fix is not None and ident_fix.validated and ident_fix.fixed_code is not None,
          "spell: identifier fix generated and AST-validated")
    if ident_fix and ident_fix.fixed_code:
        check("recieve" not in ident_fix.fixed_code and "receive_data" in ident_fix.fixed_code,
              "spell: file-wide rename applied everywhere")

    ruff = run_ruff(bad, "sample_bad.py")
    codes = {v.rule for v in ruff}
    print(f"  ruff found: {sorted((v.rule, v.line) for v in ruff)}")
    check("F401" in codes, "ruff: unused import (F401)")
    check("F841" in codes, "ruff: unused variable (F841)")
    check("E711" in codes, "ruff: '== None' comparison (E711)")
    f401_fix = next((v.fix for v in ruff if v.rule == "F401"), None)
    check(f401_fix is not None and f401_fix.validated, "ruff: F401 auto-fix validated")
    if f401_fix and f401_fix.fixed_code:
        check("import json" not in f401_fix.fixed_code, "ruff: F401 fix removes the import")
        ast.parse(f401_fix.fixed_code)

    bandit = run_bandit(bad, "sample_bad.py")
    bcodes = {v.rule for v in bandit}
    print(f"  bandit found: {sorted((v.rule, v.line) for v in bandit)}")
    check(any(c.startswith("B6") for c in bcodes), "bandit: shell injection family (B6xx)")
    check("B307" in bcodes, "bandit: eval usage (B307)")

    print("\n== sample_clean.py — must be silent (false-positive check) ==")
    fp = run_spell_layer(clean) + run_ruff(clean, "sample_clean.py") + run_bandit(clean, "sample_clean.py")
    for v in fp:
        print(f"  UNEXPECTED: L{v.line} [{v.layer.value}/{v.rule}] {v.message}")
    check(not fp, f"clean sample produced {len(fp)} violations (expected 0)")

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURES'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
