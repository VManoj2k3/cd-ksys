"""Multi-language layer verification (no LLM/server needed).

Run from project root:  python tests/test_languages.py

For every language: planted violations in bad.* must be found, clean.* must
produce ZERO deterministic violations, and the syntax validator must accept
the clean file and reject garbage.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.languages.base import plugin_for  # noqa: E402
from backend.layers.spell import run_spell_layer  # noqa: E402

LANG_DIR = ROOT / "tests" / "lang"
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def run_all_layers(filename: str):
    code = (LANG_DIR / filename).read_text()
    plugin = plugin_for(filename)
    assert plugin is not None, filename
    lint = plugin.lint(code, filename)
    sec = plugin.security(code, filename)
    spell = run_spell_layer(code, plugin)
    return plugin, code, lint, sec, spell


def expect_rules(found, wanted: dict[str, str], label: str) -> None:
    got = {v.rule for v in found}
    for rule, what in wanted.items():
        check(any(rule in g for g in got), f"{label}: {rule} ({what})")


def expect_clean(filename: str) -> None:
    plugin, code, lint, sec, spell = run_all_layers(filename)
    all_v = lint + sec + spell
    for v in all_v:
        print(f"    UNEXPECTED {filename}: L{v.line} [{v.layer.value}/{v.rule}] {v.message[:70]}")
    check(not all_v, f"{filename}: zero deterministic violations (FP check)")
    ok, note = plugin.validate_syntax(code)
    check(ok, f"{filename}: syntax validator accepts clean file ({note[:60]})")


def main() -> int:
    print("== C ==")
    plugin, code, lint, sec, spell = run_all_layers("bad.c")
    print(f"  lint={sorted({v.rule for v in lint})}")
    print(f"  sec={sorted({v.rule for v in sec})}")
    print(f"  spell={[(v.rule, v.line) for v in spell]}")
    expect_rules(sec, {"strcpy": "unbounded copy", "sprintf": "sprintf"}, "bad.c")
    expect_rules(lint, {"uninitvar": "uninitialized total"}, "bad.c")
    check(any(v.rule == "spell-identifier" and "recieve" in v.message for v in spell),
          "bad.c: misspelled identifier found")
    check(any(v.rule == "spell-comment" and "coment" in v.message for v in spell),
          "bad.c: comment misspelling found")
    ok, _ = plugin.validate_syntax(code)
    check(ok, "bad.c: compiles (planted bugs are semantic, not syntax)")
    ok, _ = plugin.validate_syntax("int main( { return 0 }")
    check(not ok, "c: validator rejects garbage")
    expect_clean("clean.c")

    print("== C++ ==")
    plugin, code, lint, sec, spell = run_all_layers("bad.cpp")
    print(f"  lint={sorted({v.rule for v in lint})}")
    print(f"  spell={[(v.rule, v.line) for v in spell]}")
    if any("cstyle" in v.rule.lower() or "cast" in v.message.lower() for v in lint):
        print("  PASS  bad.cpp: C-style cast flagged")
    else:
        print("  note  bad.cpp: C-style cast not flagged by clang-tidy locally "
              "(needs system headers; LLM/AUTOSAR layer covers casts)")
    check(any("local" in v.message.lower() or "danglingLifetime" in v.rule
              or "return" in v.rule for v in lint),
          "bad.cpp: dangling reference to local flagged")
    check(any(v.rule == "spell-identifier" and "Recieved" in v.message for v in spell),
          "bad.cpp: misspelled class name found")
    expect_clean("clean.cpp")

    print("== Java ==")
    plugin, code, lint, sec, spell = run_all_layers("Bad.java")
    print(f"  lint={sorted({v.rule for v in lint})}")
    print(f"  spell={[(v.rule, v.line) for v in spell]}")
    expect_rules(lint, {"empty-catch": "swallowed exception",
                        "string-ref-compare": "== on String",
                        "unused-import": "unused import"}, "Bad.java")
    check(any(v.rule == "spell-identifier" and "recieve" in v.message.lower()
              for v in spell), "Bad.java: misspelled identifier found")
    ok, _ = plugin.validate_syntax("class X { void broken( }")
    check(not ok, "java: validator rejects garbage")
    expect_clean("Clean.java")

    print("== TypeScript ==")
    plugin, code, lint, sec, spell = run_all_layers("bad.ts")
    print(f"  lint={sorted({v.rule for v in lint})}")
    print(f"  sec={sorted({v.rule for v in sec})}")
    print(f"  spell={[(v.rule, v.line) for v in spell]}")
    check(any("no-unused-vars" in v.rule for v in lint), "bad.ts: unused var flagged")
    check(any("eval" in v.rule for v in sec), "bad.ts: eval flagged (security)")
    check(any(v.rule == "spell-identifier" and "recieve" in v.message.lower()
              for v in spell), "bad.ts: misspelled identifier found")
    check(any(v.rule == "spell-comment" and "coment" in v.message for v in spell),
          "bad.ts: comment misspelling found")
    expect_clean("clean.ts")

    print("== spell: embedded C types must NEVER be flagged (uint FP guard) ==")
    from backend.layers.spell import run_spell_layer
    c_types = ("uint8 a; uint16 b; uint32 c; sint16 d;\n"
               "typedef struct { uint8 x; } Foo_t;\n")
    cpl = plugin_for("x.c")
    type_hits = [v for v in run_spell_layer(c_types, cpl)
                 if "uint" in v.message.lower() or "sint" in v.message.lower()]
    check(len(type_hits) == 0,
          f"uint/sint types not flagged as typos (default dict) — got {[h.message[:40] for h in type_hits]}")

    print("== C magic-numbers: generated-file suppression ==")
    from backend.languages.cnumbers import scan_magic_numbers
    hand = "int scale(int x) {\n    return x * 700 + 8000;\n}\n"
    check(len(scan_magic_numbers(hand, "c")) >= 1,
          "hand-written C: magic numbers still flagged")
    gen = "/* Generator: MICROSAR RTE Generator */\n" + hand
    check(scan_magic_numbers(gen, "c") == [],
          "generated C: magic numbers suppressed (skip_on_generated)")

    print(f"\n{'ALL LANGUAGE CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURES'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
