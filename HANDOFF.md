# Cd koosys — Session Handoff

Paste this into a fresh Cowork session to resume without losing context.

## What this project is
Standalone multi-language code review system. Layered pipeline (deterministic
tools + LLM) with a pure-web UI, running on Kaggle 2×T4 via llama-server
(Qwen2.5-Coder-14B GGUF). Main rule: **no hardcoding — detection and
false-positive suppression must be dynamic/config-driven.**

## Repo
- GitHub: https://github.com/VManoj2k3/cd-ksys  (public)
- Remote HEAD pushed: commit `06c8175` (routing fix)
- **One commit local-only, NOT pushed:** `7700785` (fix quality + verifier
  recall) — blocked by this session's git proxy. Delivered to Kaggle as a
  patch cell instead. In a fresh session, re-add the remote with a new
  classic token and `git push` will work again.

## Architecture (all config-driven via config.yaml)
- Languages: Python (ruff+bandit), C/C++ (cppcheck+clang-tidy+flawfinder,
  gcc/g++ syntax gate, AUTOSAR rule subset injected into LLM prompt),
  Java (javalang token checks + optional PMD), TypeScript/JS (eslint+tsc).
- Plugin framework: backend/languages/*.py, keyed by extension; UI dropdown
  selection is authoritative (sent as `language` in the review request).
- Layers: spell (dictionary + DYNAMIC project-vocabulary learning, no
  hardcoded allowlist), lint, security, hardcode(off), LLM review → anchor
  validation → adversarial verify → fix engine (every fix syntax-validated +
  detector-re-run + control-flow-deletion guard).
- Kaggle: deploy/bootstrap.py builds llama.cpp (CUDA), downloads model,
  starts llama-server + FastAPI + cloudflared tunnel. Main notebook cell
  boots it; a small restart cell reloads backend after a git pull.

## Validated
- All synthetic language tests pass (tests/test_languages.py, test_local.py).
- Stress suite passes (tests/stress_test.py): FP-bait clean files silent,
  planted bugs caught, chunking/concurrency/API robustness OK.
- Real production code (user's AUTOSAR .c + ROS .cpp): deterministic FP rate
  ~0 after dynamic-vocabulary tuning; caught genuine typos (TRESHOLD, Stoped,
  Initializaton) while auto-suppressing conventions (Shft) with no word list.

## Production hardening (Phase 1) — branch claude/phase-1-production-ready-*
Landed: startup config validation (fail-fast), config overlay merge
(config.deploy.yaml now holds only prod deltas — no more drift), structured
logging (json option), capacity caps (concurrent reviews / active jobs /
job-store eviction), per-user review rate limit, login throttling, request +
upload size bounds, security headers (CSP etc.), health caching, /api/version,
Prometheus /api/metrics (bearer token), graceful shutdown, compose healthcheck
+ log rotation + no-new-privileges, supervised entrypoint, pinned deps,
GitHub Actions CI, tests/test_production.py (offline suite).
Also fixed: lint dedup was collapsing ruff repeats (three `== None` lines
reported as one) — collapse is now per-language config
(`<lang>.collapse_repeated_lint`, on for c/cpp only).

## Phase-1 accuracy work (this branch) + GPU acceptance runbook
The detection→fix pipeline gates are now PROVEN offline by
tests/test_llm_pipeline.py (scripted adversarial model): anchor validation
(whitespace-tolerant), category whitelist, verifier drop path, chunk line
numbering + overlap dedup, same-line merge, JSON-mode fallback, and the fix
gates — no-op rejection, destructive guard, syntax gate + retry, NEW
detector gate (patch may not introduce any new deterministic finding,
line-shift aware), and a NEW adversarial fix-verify pass
(prompts/fix_verify.txt, llm.fix.verify_enabled).

**On the T4/on-prem stack, run the acceptance pass:**
```
KOOSYS_URL=<stack-url> python -m tests.accuracy_eval
```
It reports LLM recall on planted bugs (py_semantic_bugs.py + C/C++ corpus),
LLM FPs on clean FP-bait files, and validated-fix coverage; JSON report in
tests/eval/last_report.json (diff before/after tuning). Gates:
EVAL_MIN_LLM_RECALL=0.6 EVAL_MAX_LLM_FPS=0 (tighten as the model allows).
Knobs to iterate: llm.chunk_overlap_lines (try 30), verify.txt wording,
llm.fix.verify_enabled. Add real AUTOSAR/ROS files + expected lines to
tests/eval/manifest.yaml before trusting broadly.

## GPU-verified Phase-1 acceptance results (Kaggle P100, Qwen2.5-Coder-14B Q6)
Measured with tests/accuracy_eval.py via the headless acceptance kernel
(deploy/kaggle_acceptance.py, dataset-cached llama binary, ~15 min/run).
ALL FIVE LANGUAGES (run 7, py/C/C++/Java/TS labeled corpus):
- LLM recall on planted semantic bugs: 26/26 (100%) across all 5 languages
  — Java 4/4, TS 4/4, py 5/5+leak, C 7/7, C++ 5/5
- Deterministic required findings: 6/6; deterministic FPs on clean files: 0
  (5 clean FP-bait files incl. TS: all silent)
- LLM FPs on clean FP-bait files: 1 (borderline advice: sync open() inside
  an async function — technically true, debatable severity)
- Validated-fix coverage: 31/47 overall, 15/20 LLM findings (75%)
- Validated-fix coverage (final run): 23/36 findings overall, 9/12 LLM
  findings (75%), after indentation auto-repair + prompt guidance; every
  rejected fix logs which gate refused it (Violation.fix_notes). Remaining
  no-fixes are hard multi-edit cases (C++ rule-of-three class rewrite,
  C overflow guard, minimal mutation-while-iterating rewrite) or gates
  correctly refusing broken model patches — precision over coverage by
  design: 100% of DISPLAYED fixes passed every gate.

## Real production-file validation (user's AUTOSAR .c, deterministic pass)
Ran 3 real MICROSAR/RTE files (Com_Appl.c 2992L, PwrUState_Ovi.c 1723L,
WarningLamp.c 493L) through the deterministic layers locally (NOT uploaded
anywhere — proprietary code stays in-session; LLM pass is for the user's own
stack per the on-prem model):
- lint 0 / security 0 false positives on all three (external-call-heavy code)
- 0 identifier FPs: dynamic vocab suppressed all ShftSpd*/despumpspd naming
- caught REAL typos: VEH_SPEED_TRESHOLD, PwrUState_ShaftStoped, Initializaton,
  PumpStoped, and Curent inside an Rte_ name (flagged, marked no-autofix)
- magic numbers were high-volume on these generated templates; per user
  decision, added c/cpp.magic_numbers.skip_on_generated (default true) —
  suppresses magic-number findings ONLY on files detected as generated
  (backend/detect.py, config detection.generated_markers). Hand-written files
  still flagged. Regression-tested in test_languages.py.

## Open items / next steps
1. ~~Verifier recall (commit 7700785, needs T4 re-test)~~ **CONFIRMED on
   GPU**: rebalanced verify.txt keeps all conditional/edge-case bugs
   (18/18 recall incl. off-by-one + leak) with only 1 borderline FP.
2. **Uninitialized-var fixes:** now steered to fix the declaration, not the
   use-site; destructive fixes (deleting return/break) are rejected. Verify
   on T4 that the uninitvar fix now targets the declaration correctly.
3. Comment-only abbreviations (Shft/Strat appearing only in comments, not in
   ≥3 identifiers) still flag — minor residual; cross-file vocab would fix it.
4. AUTOSAR is GUIDED (clang-tidy CERT/core-guidelines + prompt rules), not
   certified compliance.

## How to run on Kaggle
Notebook, GPU T4 x2, Internet ON. Main cell: git clone/pull cd-ksys →
deploy.bootstrap → start llama-server + backend + tunnel → open the printed
trycloudflare URL. Select language in the dropdown BEFORE pasting/uploading.
