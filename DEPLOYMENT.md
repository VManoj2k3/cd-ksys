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
# edit deploy/.env: set KOOSYS_SESSION_SECRET (openssl rand -hex 32), CUDA_ARCH
```
Edit **`config.deploy.yaml`** → `auth.ldap` for your directory:
- **Active Directory:** set `bind_dn_template: "{username}@company.internal"`,
  leave the search-bind fields empty.
- **OpenLDAP:** leave `bind_dn_template` empty; set `base_dn`,
  `user_attribute: uid`, and a read-only `search_bind_dn` +
  `search_bind_password` (put the password in the environment, not the file).
Set `server_uri` to your `ldaps://…:636`. Keep `tls_verify: true`.

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

To expose directly on an internal interface instead of a proxy, change the
compose port mapping from `127.0.0.1:8000:8000` to `<internal-ip>:8000:8000`.

## 4. Security posture
- **Auth:** every UI/API route except `/login`, `/api/health`, and static
  assets requires a valid session. Sessions are signed, HTTP-only cookies with
  an 8 h TTL. LDAP bind is live per login; passwords are never stored or logged.
- **Audit:** `deploy/audit/audit.log` records logins (user, IP, success) and
  reviews (user, filename, language, size) as JSON lines — **never the source
  code**.
- **Network:** no outbound calls except the one-time model download (behind
  `HF_TOKEN` if you mirror it internally). The LLM runs locally; code is not
  sent to any external API.
- **Secrets:** `KOOSYS_SESSION_SECRET` and the LDAP service password come from
  the environment / `.env`, which is git-ignored.

## 5. Operations
- Update: `git pull && docker compose -f deploy/docker-compose.yml up -d --build`
- Restart policy `unless-stopped` restarts the service on crash/reboot.
- GPU/VRAM: Qwen2.5-Coder-14B Q6 (~12 GB) fits one card; `TENSOR_SPLIT=1,1`
  spreads it across both for headroom + throughput. Drop to a Q4 GGUF if VRAM
  is tight.
- Health: `curl http://127.0.0.1:8000/api/health`

## Known limits (before wide rollout)
- LLM findings are high-precision but not 100% recall; AUTOSAR is **guided,
  not certified** — not a compliance sign-off tool.
- Single-file review; no CI/PR integration yet (Phase 2).
- Zero-FP is validated on sample + real files, not guaranteed universally —
  run the accuracy pass (`tests/`) on a representative corpus before trusting
  it broadly.
