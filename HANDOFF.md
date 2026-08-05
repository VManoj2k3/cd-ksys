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

## Open items / next steps
1. **Verifier recall (commit 7700785, needs T4 re-test):** the adversarial
   verifier was rejecting real bugs (off-by-one, memory leak) on real C.
   Rebalanced verify.txt to keep conditional/edge-case bugs. MUST re-review a
   C snippet on the T4 stack to confirm off-by-one + leak now survive AND no
   new false positives appear. This is the precision/recall knob to watch.
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
