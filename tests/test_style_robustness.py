"""Coding-style robustness — same bug / same clean code, many styles.

    python -m tests.test_style_robustness

Answers "does it work regardless of coding style?" for the DETERMINISTIC
layers (no GPU): recall must hold and false positives must stay at zero when
the SAME logic is written in different brace styles, indentation, spacing,
and naming conventions. The LLM layer's style robustness is measured on a
live stack via tests/accuracy_eval.py (style-variant entries in the manifest).

Why this matters: AST-based layers (ruff/bandit/cppcheck) are style-invariant
by construction, but the spell layer splits identifiers by case convention,
and anchoring/fix gates handle whitespace — those are the parts style can
actually break, so they get the most variants here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.languages.base import plugin_by_language  # noqa: E402
from backend.layers.spell import run_spell_layer  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


PY = plugin_by_language("py")
C = plugin_by_language("c")


def py_rules(code: str):
    return {v.rule for v in PY.lint(code, "s.py")} | \
           {v.rule for v in PY.security(code, "s.py")}


def spell_hits(code: str, plugin):
    return run_spell_layer(code, plugin)


# ============================================================ recall by style
print("== E711 (== None) survives Python formatting styles ==")
e711_styles = {
    "pep8":        "def f(x):\n    if x == None:\n        return 1\n    return 2\n",
    "compact":     "def f(x):\n    if x==None: return 1\n    return 2\n",
    "extra space": "def f(x):\n    if x   ==   None :\n        return 1\n    return 2\n",
    "tab indent":  "def f(x):\n\tif x == None:\n\t\treturn 1\n\treturn 2\n",
}
for name, code in e711_styles.items():
    check("E711" in py_rules(code), f"E711 caught — {name}")

print("== bandit (eval / weak hash) survives styles ==")
eval_styles = {
    "spaced":  "def run(s):\n    return eval(s)\n",
    "compact": "def run(s):return eval(s)\n",
    "nested":  "def run(s):\n    if s:\n        return eval( s )\n",
}
for name, code in eval_styles.items():
    check("B307" in py_rules(code), f"eval (B307) caught — {name}")

print("== C magic number survives brace/indent styles ==")
c_styles = {
    "K&R tabs":      "int f(int x){\n\treturn x + 700;\n}\n",
    "Allman spaces": "int f(int x)\n{\n    return x + 700;\n}\n",
    "one-liner":     "int f(int x){return x+700;}\n",
    "wide spacing":  "int  f( int x )\n{\n        return   x + 700 ;\n}\n",
}
for name, code in c_styles.items():
    hits = [v for v in C.hardcode(code, "f.c") if v.rule == "magic-number"]
    check(any("700" in v.message for v in hits), f"magic 700 caught — {name}")

# ============================================================ spell by naming
print("== misspelled identifier caught across naming conventions ==")
# 'recieve' (should be receive) in every common convention
naming = {
    "snake_case":     "def recieve_data():\n    return 1\n",
    "camelCase":      "def recieveData():\n    return 1\n",
    "PascalCase":     "class RecieveData:\n    pass\n",
    "SCREAMING":      "RECIEVE_TIMEOUT = 5\nx = RECIEVE_TIMEOUT\n",
    "mixed_Snake":    "def Recieve_Handler():\n    return 1\n",
}
for name, code in naming.items():
    hits = spell_hits(code, PY)
    caught = any("recieve" in v.message.lower() for v in hits)
    check(caught, f"'recieve' typo caught — {name}")

# ============================================================ FP by style
print("== clean code stays SILENT across styles (false-positive check) ==")
clean_c = {
    "K&R tabs": (
        "#include <stddef.h>\n"
        "int sum(const int *a, size_t n){\n"
        "\tint t = 0;\n"
        "\tfor (size_t i = 0; i < n; i++){\n"
        "\t\tt += a[i];\n"
        "\t}\n"
        "\treturn t;\n"
        "}\n"),
    "Allman spaces": (
        "#include <stddef.h>\n"
        "int sum(const int *a, size_t n)\n"
        "{\n"
        "    int t = 0;\n"
        "    for (size_t i = 0; i < n; i++)\n"
        "    {\n"
        "        t += a[i];\n"
        "    }\n"
        "    return t;\n"
        "}\n"),
}
for name, code in clean_c.items():
    det = (C.lint(code, "c.c") + C.security(code, "c.c") +
           C.hardcode(code, "c.c") + spell_hits(code, C))
    check(len(det) == 0, f"clean C silent — {name} (got {[v.rule for v in det]})")

clean_py = {
    "verbose": (
        "def average(values):\n"
        "    if not values:\n"
        "        return 0.0\n"
        "    total = 0\n"
        "    for value in values:\n"
        "        total += value\n"
        "    return total / len(values)\n"),
    "compact": (
        "def average(values):\n"
        "    return sum(values) / len(values) if values else 0.0\n"),
}
for name, code in clean_py.items():
    det = (PY.lint(code, "p.py") + PY.security(code, "p.py") +
           spell_hits(code, PY))
    check(len(det) == 0, f"clean Python silent — {name} (got {[v.rule for v in det]})")

# ============================================================ correct-word FP
print("== correctly-spelled identifiers never flagged (naming FP check) ==")
# these are correct words in various conventions — must NOT be flagged
ok_names = {
    "snake":     "def receive_buffer():\n    return 1\n",
    "camel":     "def receiveBuffer():\n    return 1\n",
    "screaming": "MAXIMUM_RETRIES = 3\nx = MAXIMUM_RETRIES\n",
}
for name, code in ok_names.items():
    hits = spell_hits(code, PY)
    check(len(hits) == 0, f"correct spelling silent — {name} (got {[h.message[:30] for h in hits]})")

print(f"\n{'ALL STYLE ROBUSTNESS CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}")
if FAILURES:
    for f in FAILURES:
        print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
