"""Kaggle bootstrap for Cd koosys — all runtime values come from config.yaml.

Used by the notebook: each function is one deployment step.
Designed for a Kaggle GPU notebook with 2x T4 and internet enabled.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from backend.app_config import CFG  # noqa: E402

WORK = Path(CFG.get("kaggle.model_dir", "/kaggle/working/models")).parent
BIN_DIR = WORK / "llama-bin"
LOG_DIR = WORK / "logs"
GH_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"


def sh(cmd: str, **kw) -> subprocess.CompletedProcess:
    print(f"$ {cmd}")
    return subprocess.run(cmd, shell=True, text=True, **kw)


def install_deps() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sh(f"pip install -q -r {APP_DIR / 'requirements.txt'}")
    print("deps installed")


# ---------------------------------------------------------------- llama-server
def _release_assets(tag: str) -> tuple[str, list[dict]]:
    def fetch(url: str) -> dict:
        with urllib.request.urlopen(url) as r:
            return json.load(r)

    if tag and tag != "latest":
        try:
            data = fetch(f"{GH_API}/tags/{tag}")
        except Exception:  # noqa: BLE001 — pinned tag missing: fall back to latest
            print(f"release tag '{tag}' not found; falling back to latest")
            data = fetch(f"{GH_API}/latest")
    else:
        data = fetch(f"{GH_API}/latest")
    return data["tag_name"], data.get("assets", [])


def get_llama_server() -> Path:
    """Prebuilt CUDA binary if a matching release asset exists, else build."""
    server = BIN_DIR / "llama-server"
    if server.exists():
        print(f"llama-server cached at {server}")
        return server
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    tag = str(CFG.get("kaggle.llama_cpp_release", "latest"))
    try:
        tag, assets = _release_assets(tag)
        cand = [
            a for a in assets
            if re.search(r"ubuntu.*(cuda|cu\d+).*x64.*\.zip$", a["name"], re.I)
            or re.search(r"(cuda|cu\d+).*ubuntu.*x64.*\.zip$", a["name"], re.I)
        ]
        if cand:
            asset = cand[0]
            zpath = BIN_DIR / asset["name"]
            print(f"downloading prebuilt {asset['name']} ({tag})")
            urllib.request.urlretrieve(asset["browser_download_url"], zpath)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(BIN_DIR)
            found = next(BIN_DIR.rglob("llama-server"), None)
            if found:
                if found != server:
                    shutil.copy2(found, server)
                    for lib in found.parent.glob("*.so*"):
                        shutil.copy2(lib, BIN_DIR / lib.name)
                server.chmod(0o755)
                if _server_runs(server):
                    return server
                print("prebuilt binary failed to run; falling back to source build")
    except Exception as exc:  # noqa: BLE001
        print(f"prebuilt lookup failed ({exc}); building from source")
    return _build_from_source(tag, server)


def _server_runs(server: Path) -> bool:
    env = dict(os.environ, LD_LIBRARY_PATH=f"{server.parent}:{os.environ.get('LD_LIBRARY_PATH', '')}")
    p = subprocess.run([str(server), "--version"], capture_output=True, text=True, env=env)
    ok = p.returncode == 0
    print(p.stdout or p.stderr)
    return ok


def _build_from_source(tag: str, server: Path) -> Path:
    src = WORK / "llama.cpp"
    if not (src / "CMakeLists.txt").exists():
        shutil.rmtree(src, ignore_errors=True)
        cloned = False
        if tag and tag != "latest":
            p = sh(f"git clone --depth 1 --branch {tag} https://github.com/ggml-org/llama.cpp {src}")
            cloned = p.returncode == 0
        if not cloned:
            shutil.rmtree(src, ignore_errors=True)
            sh(f"git clone --depth 1 https://github.com/ggml-org/llama.cpp {src}", check=True)
    sh(
        f"cmake -S {src} -B {src}/build -DGGML_CUDA=ON -DLLAMA_CURL=OFF "
        f"-DCMAKE_CUDA_ARCHITECTURES=75 -DCMAKE_BUILD_TYPE=Release",
        check=True,
    )
    sh(f"cmake --build {src}/build --target llama-server -j {os.cpu_count()}", check=True)
    built = next((src / "build").rglob("llama-server"))
    shutil.copy2(built, server)
    for lib in built.parent.parent.rglob("*.so*"):
        shutil.copy2(lib, BIN_DIR / lib.name)
    server.chmod(0o755)
    return server


# ---------------------------------------------------------------- model
def download_model() -> Path:
    from huggingface_hub import hf_hub_download

    model_dir = Path(CFG.get("kaggle.model_dir"))
    model_dir.mkdir(parents=True, exist_ok=True)
    path = hf_hub_download(
        repo_id=CFG.get("kaggle.model_repo"),
        filename=CFG.get("kaggle.model_file"),
        local_dir=model_dir,
        token=os.environ.get("HF_TOKEN") or None,
    )
    print(f"model at {path}")
    return Path(path)


# ---------------------------------------------------------------- processes
def start_llama(server: Path, model: Path) -> subprocess.Popen:
    ls = CFG.get("kaggle.llama_server", {})
    cmd = [
        str(server),
        "--model", str(model),
        "--host", str(ls.get("host")),
        "--port", str(ls.get("port")),
        "--ctx-size", str(ls.get("ctx_size")),
        "--n-gpu-layers", str(ls.get("n_gpu_layers")),
        "--split-mode", str(ls.get("split_mode")),
        "--tensor-split", str(ls.get("tensor_split")),
        "--parallel", str(ls.get("parallel_slots")),
        "--alias", str(CFG.get("llm.model")),
    ]
    if ls.get("flash_attn"):
        cmd.append("--flash-attn")
    log = open(LOG_DIR / "llama-server.log", "w")
    env = dict(os.environ, LD_LIBRARY_PATH=f"{server.parent}:{os.environ.get('LD_LIBRARY_PATH', '')}")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
    _wait_http(f"http://{ls.get('host')}:{ls.get('port')}/health", 600, "llama-server")
    return proc


def start_backend() -> subprocess.Popen:
    log = open(LOG_DIR / "backend.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.main"],
        cwd=APP_DIR, stdout=log, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONPATH=str(APP_DIR)),
    )
    port = CFG.get("server.port")
    _wait_http(f"http://127.0.0.1:{port}/api/health", 60, "backend")
    return proc


def start_tunnel() -> str:
    """Cloudflared quick tunnel (no account needed). Returns the public URL."""
    port = int(CFG.get("kaggle.tunnel.local_port", CFG.get("server.port")))
    cf = WORK / "cloudflared"
    if not cf.exists():
        urllib.request.urlretrieve(
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
            cf,
        )
        cf.chmod(0o755)
    logfile = LOG_DIR / "cloudflared.log"
    log = open(logfile, "w")
    subprocess.Popen(
        [str(cf), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    for _ in range(60):
        time.sleep(2)
        m = url_re.search(logfile.read_text(errors="ignore"))
        if m:
            print(f"\nPUBLIC URL: {m.group(0)}\n")
            return m.group(0)
    raise RuntimeError(f"tunnel URL not found; check {logfile}")


def _wait_http(url: str, timeout_s: int, name: str) -> None:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status < 500:
                    print(f"{name} is up ({url})")
                    return
        except Exception:  # noqa: BLE001
            time.sleep(3)
    raise RuntimeError(f"{name} did not come up within {timeout_s}s — see {LOG_DIR}")


def smoke_test() -> None:
    """End-to-end request against the running stack."""
    import httpx

    port = CFG.get("server.port")
    sample = (APP_DIR / "tests" / "sample_bad.py").read_text()
    r = httpx.post(
        f"http://127.0.0.1:{port}/api/review",
        json={"code": sample, "filename": "sample_bad.py"},
        timeout=30,
    )
    job_id = r.json()["job_id"]
    print(f"job {job_id} submitted; polling…")
    while True:
        time.sleep(5)
        job = httpx.get(f"http://127.0.0.1:{port}/api/job/{job_id}", timeout=30).json()
        states = {l["name"]: l["state"] for l in job["layers"]}
        print(f"  state={job['state']} layers={states}")
        if job["state"] in ("done", "error"):
            break
    print(json.dumps(job.get("stats", {}), indent=2))
    for v in job.get("violations", []):
        fx = "fix✓" if v.get("fix") and v["fix"].get("validated") else "no-fix"
        print(f"  L{v['line']:>4} [{v['layer']}/{v['rule']}] {v['severity']}: {v['message'][:80]} ({fx})")
