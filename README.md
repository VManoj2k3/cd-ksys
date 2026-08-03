# Cd koosys — Code Review System (Phase 1)

Standalone, pure-web code review for Python with llama-server. Accuracy comes
from a layered pipeline, not from trusting the model: deterministic tools find
what they can prove, and every LLM claim is mechanically checked before it is
shown.

## Architecture

```
Browser (pure HTML/JS — paste or upload, violation cards, inline fix diffs)
   │  cloudflared tunnel (on Kaggle)
FastAPI orchestrator  (backend/)
   ├─ L0 spell     codespell dictionary + camelCase/snake_case identifier splitting
   ├─ L1 lint      ruff (style/correctness) + bandit (security) — AST-exact, zero hallucination
   ├─ L1b hardcode AST scanner (DISABLED in config per project decision; flip hardcode.enabled)
   ├─ L2 LLM       llama-server review, numbered lines, JSON-schema-constrained output
   ├─ L3 verify    adversarial re-check of every LLM finding; unconfirmed → dropped
   └─ fixes        every fix applied → ast.parse → detector re-run before display
```

Anti-false-positive mechanisms:
1. **Capability split** — the LLM only hunts semantic issues linters can't see.
2. **Grammar-constrained JSON** — malformed output is impossible at the token level.
3. **Line-anchor validation** — the model must quote the offending line; the backend
   verifies the quote against the real file and rejects mismatches.
4. **Adversarial verifier** — a skeptic prompt re-judges each finding in isolation.
5. **Validated fixes** — a fix is only shown after it parses and the original
   detector confirms the violation is gone.

## Run locally (no GPU — deterministic layers only)

```bash
pip install -r requirements.txt
python -m backend.main            # open http://localhost:8000
python -m tests.test_local        # layer verification suite
```

The UI shows "LLM: offline" and runs spell + lint + security layers only.

## Run on Kaggle (2x T4)

1. Push this folder to a private Kaggle **Dataset** named `cd-koosys`
   (or a git repo).
2. Open `deploy/kaggle_koosys.ipynb` as a Kaggle notebook.
   Settings: **GPU T4 x2**, **Internet ON**.
3. Run cells top to bottom. First run downloads the ~12 GB GGUF and the
   llama.cpp server (prebuilt CUDA binary, or ~20 min source build — cached).
4. The tunnel cell prints a `https://….trycloudflare.com` URL — that's your app.

## Configuration

Everything tunable lives in `config.yaml` (no hardcoded values in code):
model/quant, GPU split, ports, rule selection, allowlists, chunk sizes,
verifier on/off, retry counts. Secrets go in `.env` (see `.env.example`).

## Known limits (honest ones)

- Zero FP/FN is not a property any reviewer has. Deterministic layers are
  FP-free by construction; the LLM layer is precision-biased (the verifier
  trades a little recall for it).
- Phase 1 is Python-only. The layer interfaces are language-agnostic; adding
  a language = adding its linter + extending `languages.enabled`.
- Applying one fix invalidates other fixes' line numbers — the UI forces a
  re-review after each apply (correctness over convenience).

## Phase 2 candidates

More languages (ESLint/TS), multi-file review, diff/PR mode, severity
policies, project-custom rules fed to the LLM, GPU-aware queueing.
