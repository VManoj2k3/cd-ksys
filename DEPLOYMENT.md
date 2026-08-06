# Cd koosys — On-Prem Deployment (internal use)

Runs the code-review service entirely inside your network on a GPU server.
Source code never leaves your machines; no public tunnel.

## Target
- 2× GPU server (built/tested for RTX 50-series / Blackwell, `CUDA_ARCH=120`)
- NVIDIA driver ≥ 570, CUDA 12.8-capable, `nvidia-container-toolkit` installed
- Docker + Docker Compose
- LDAP/AD reachable from the server

## 1. Configure
```bash
git clone https://github.com/VManoj2k3/cd-ksys.git && cd cd-ksys
cp deploy/.env.example deploy/.env
# edit deploy/.env: set KOOSYS_SESSION_SECRET (openssl rand -hex 32),
# KOOSYS_METRICS_TOKEN (openssl rand -hex 24), CUDA_ARCH
```
**`config.deploy.yaml` is an overlay**: it is deep-merged over `config.yaml`
at startup (env `KOOSYS_CONFIG_OVERLAY`), so it only contains the keys that
differ in production. Anything not listed inherits the base config — new
tunables can never silently drift out of the deployment.

Edit `config.deploy.yaml` → `auth.ldap` for your directory:
- **Active Directory:** set `bind_dn_template: "{username}@company.internal"`,
  leave the search-bind fields empty.
- **OpenLDAP:** leave `bind_dn_template` empty; set `base_dn`,
  `user_attribute: uid`, and a read-only `search_bind_dn` +
  `search_bind_password` (put the password in the environment, not the file).
Set `server_uri` to your `ldaps://…:636`. Keep `tls_verify: true`.

Configuration is **validated at startup** — the service refuses to boot on a
broken config (bad auth mode, missing secret, invalid limits) and logs each
problem, instead of failing at first login.

## 2. Build & run
```bash
docker compose -f deploy/docker-compose.yml up -d --build
```
First run builds llama.cpp for your GPU (~15–25 min) and downloads the ~12 GB
model into a named volume (cached thereafter). Watch startup:
```bash
docker compose -f deploy/docker-compose.yml logs -f
```
Ready when you see `llama-server ready` then the backend startup line.

## 3. Access
The service binds to **127.0.0.1:8000 on the host** by default. Put your
internal reverse proxy (nginx/Traefik) in front with **HTTPS**, and set
`auth.cookie_secure: true` (already set in `config.deploy.yaml`). Users open
the proxied URL, land on `/login`, and sign in with their LDAP credentials.

`server.trust_proxy_headers: true` (set in the overlay) makes the audit log
record real client IPs from `X-Forwarded-For` instead of the proxy's address.

To expose directly on an internal interface instead of a proxy, change the
compose port mapping from `127.0.0.1:8000:8000` to `<internal-ip>:8000:8000`.

## 4. Security posture
- **Auth:** every UI/API route except `/login`, `/api/health`, `/api/version`,
  and static assets requires a valid session. Sessions are signed, HTTP-only
  cookies with an 8 h TTL. LDAP bind is live per login; passwords are never
  stored or logged.
- **Login throttling:** after `auth.login_max_attempts` failed attempts per
  (IP, username) within `auth.login_window_seconds` (default 5 in 5 min),
  sign-in returns 429 until the window rolls over; a broader per-IP cap slows
  credential spraying.
- **Headers:** every response carries a strict Content-Security-Policy,
  `X-Frame-Options: DENY`, `nosniff`, and `Referrer-Policy: no-referrer`;
  API responses are `Cache-Control: no-store`.
- **Bounded input:** request bodies beyond `server.max_request_kb` are
  rejected before parsing; files beyond `server.max_file_size_kb` are
  rejected without buffering the rest.
- **Audit:** `deploy/audit/audit.log` records logins (user, IP, success) and
  reviews (user, filename, language, size) as JSON lines — **never the source
  code**.
- **Network:** no outbound calls except the one-time model download (behind
  `HF_TOKEN` if you mirror it internally). The LLM runs locally; code is not
  sent to any external API.
- **Secrets:** `KOOSYS_SESSION_SECRET`, `KOOSYS_METRICS_TOKEN`, and the LDAP
  service password come from the environment / `.env`, which is git-ignored.
- **Container:** `no-new-privileges`, `init` for zombie reaping, loopback-only
  port binding, JSON log rotation (10 MB × 5).

## 5. Capacity & rate limits (config-driven)
| Key | Default | Meaning |
|---|---|---|
| `server.max_concurrent_reviews` | 2 | reviews running at once; more queue |
| `server.max_active_jobs` | 10 | queued+running beyond this → 429 |
| `server.max_jobs_in_memory` | 500 | oldest finished jobs evicted beyond this |
| `server.reviews_per_user_per_minute` | 12 | per-user submission cap → 429 |
| `review_max_seconds` | 1800 | hard per-review budget once it starts running |

Match `max_concurrent_reviews` to `llm.max_parallel_requests` and GPU
throughput. Queue time does **not** count against a review's time budget.

## 6. Operations
- **Health:** `curl http://127.0.0.1:8000/api/health` — reports version, LLM
  reachability, enabled languages. The compose file uses it as the container
  healthcheck (`docker ps` shows healthy/unhealthy).
- **Metrics:** `curl -H "Authorization: Bearer $KOOSYS_METRICS_TOKEN"
  http://127.0.0.1:8000/api/metrics` — Prometheus text format: review counts
  by result, durations, login/throttle counters, queue gauges, LLM up/down.
  Scrape config: plain `bearer_token` job pointed at `/api/metrics`.
- **Logs:** application logs are JSON lines on stdout
  (`docker compose logs`); audit is a separate file under `deploy/audit/`.
- **Update:** `git pull && docker compose -f deploy/docker-compose.yml up -d --build`
- **Rollback:** `git checkout <last-good-tag> && docker compose -f deploy/docker-compose.yml up -d --build`
  (the model volume is untouched either way).
- **Restart policy** `unless-stopped` restarts the service on crash/reboot;
  a SIGTERM shuts down cleanly — in-flight reviews are marked as errored
  rather than left dangling.
- **GPU/VRAM:** Qwen2.5-Coder-14B Q6 (~12 GB) fits one card; `TENSOR_SPLIT=1,1`
  spreads it across both for headroom + throughput. Drop to a Q4 GGUF if VRAM
  is tight.
- **Scaling note:** the job store is in-process memory — run exactly **one**
  backend process (the compose file does). Scale throughput via
  `llm.max_parallel_requests` / `server.max_concurrent_reviews`, not extra
  uvicorn workers.

## Known limits (before wide rollout)
- LLM findings are high-precision but not 100% recall; AUTOSAR is **guided,
  not certified** — not a compliance sign-off tool.
- Single-file review; no CI/PR integration yet (Phase 2).
- Zero-FP is validated on sample + real files, not guaranteed universally —
  run the accuracy pass (`tests/`) on a representative corpus before trusting
  it broadly.
- Jobs live in memory: a restart drops running reviews (they are marked as
  errored; users just resubmit). Fine for single-file interactive review.
