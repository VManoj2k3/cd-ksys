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
verifier on/off, retry counts, capacity/rate limits. Secrets go in `.env`
(see `.env.example`). A deployment overrides only its deltas via
`KOOSYS_CONFIG_OVERLAY` (deep-merged over the base — see `config.deploy.yaml`).
Config is validated at startup; the service refuses to boot on a broken one.

## Production readiness (Phase 1)

Hardened for internal production use — see `DEPLOYMENT.md` for the runbook:

- **Fail-fast config validation**, structured logging (plain or JSON lines).
- **Bounded everything:** concurrent reviews + queue caps, per-user rate
  limits, request/upload size caps, job-store eviction, per-tool timeouts.
- **Auth hardening:** login throttling (per-user and per-IP windows), signed
  HTTP-only session cookies, strict security headers (CSP, frame-deny,
  nosniff), audit log without source code.
- **Observability:** `/api/health` (container healthcheck), `/api/version`,
  Prometheus-text `/api/metrics` behind a bearer token.
- **Clean lifecycle:** graceful shutdown marks in-flight reviews, supervised
  llama-server + backend in one container, Docker healthcheck + log rotation.
- **CI:** ruff gate + all four offline suites on every push
  (`.github/workflows/ci.yml`); dependencies are pinned.

```bash
python -m tests.test_local        # layer verification
python -m tests.test_languages    # all language plugins
python -m tests.test_production   # hardening suite (auth, limits, metrics)
python -m tests.test_llm_pipeline # anti-FP gates vs a scripted adversarial model
python -m tests.stress_test       # against a live server (KOOSYS_URL)
python -m tests.accuracy_eval     # precision/recall report (KOOSYS_URL)
```

## Accuracy: how "very few false positives" is enforced

Detection and fixes pass through layered gates, each one **proven by
`tests/test_llm_pipeline.py`** against a scripted adversarial model
(hallucinated quotes, bad categories, no-op / destructive / rule-breaking
patches — all must be rejected while planted real findings survive):

1. **Findings**: category whitelist → line-anchor validation (the model must
   quote the code; whitespace-tolerant, content-exact) → cross-chunk dedup →
   adversarial verify pass. Unconfirmed → dropped, never shown.
2. **Inline fixes**: bounds + proximity → no-op rejection → destructive-fix
   guard (never deletes control flow / accumulation) → syntax gate →
   **detector re-run**: the violation must be gone AND no new deterministic
   finding may appear anywhere in the patched file → adversarial fix-verify
   pass for semantic fixes. A failed gate = retry, then "manual fix required"
   — an unvalidated patch is never displayed.

The **model-dependent** half of accuracy (raw recall/FP of Qwen on the GPU)
is measured with `tests/accuracy_eval.py` against the live stack: a labeled
corpus (planted bugs with exact lines + clean FP-bait files) produces a
precision/recall/fix-coverage report (`tests/eval/last_report.json`).
Optional gates `EVAL_MIN_LLM_RECALL` / `EVAL_MAX_LLM_FPS` turn the report
into a pass/fail acceptance test. Extend `tests/eval/manifest.yaml` with
your own representative files before wide rollout.

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
