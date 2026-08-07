"""Headless Kaggle acceptance run — no notebook, no tunnel.

Boots the full GPU stack (llama-server + Qwen GGUF + backend) inside a Kaggle
script kernel, runs tests/accuracy_eval.py against localhost, and copies the
JSON report + service logs into /kaggle/working so `kaggle kernels output`
can retrieve them after the run.

Push via the Kaggle API with a thin kernel.py wrapper:

    git clone --depth 1 -b <branch> https://github.com/VManoj2k3/cd-ksys.git
    python cd-ksys/deploy/kaggle_acceptance.py

Heavy artifacts (model, llama build) go to /kaggle/tmp so they are NOT
persisted as kernel output; only the report and logs are.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
OUT = Path("/kaggle/working")
BIN_CACHE_DS = Path("/kaggle/input/koosys-llama-bin")  # optional dataset cache
TMP_BIN = Path("/kaggle/tmp/llama-bin")                # bootstrap's BIN_DIR
sys.path.insert(0, str(APP))


def gpu_count() -> int:
    p = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
    return (p.stdout or "").count("GPU ")


def seed_llama_from_dataset() -> None:
    """If the koosys-llama-bin dataset is attached, pre-place the binary so
    bootstrap's cache check hits instantly — no GitHub download, no source
    build. The dataset is created once from a previous run's output."""
    if not BIN_CACHE_DS.exists():
        return
    src = BIN_CACHE_DS / "llama-bin" if (BIN_CACHE_DS / "llama-bin").exists() \
        else BIN_CACHE_DS
    server = src / "llama-server"
    if not server.exists():
        print(f"cache dataset attached but no llama-server under {src}")
        return
    TMP_BIN.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, TMP_BIN / f.name)
    (TMP_BIN / "llama-server").chmod(0o755)
    print(f"seeded llama-server from dataset cache ({src})")


def export_llama_for_cache() -> None:
    """Copy the working llama binary (+ shared libs, minus release zips) into
    the kernel output so it can be published once as the cache dataset."""
    if BIN_CACHE_DS.exists():
        return  # already running from the cache — nothing new to export
    if (OUT / "llama-bin" / "llama-server").exists():
        return  # already exported this run
    if not (TMP_BIN / "llama-server").exists():
        print("no standalone llama-server binary to export (python-module mode?)")
        return
    dst = OUT / "llama-bin"
    dst.mkdir(parents=True, exist_ok=True)
    for f in TMP_BIN.iterdir():
        if f.is_file() and not f.name.endswith(".zip"):
            shutil.copy2(f, dst / f.name)
    size = sum(f.stat().st_size for f in dst.iterdir()) // (1024 * 1024)
    print(f"exported llama-bin to kernel output for caching ({size} MB)")


def main() -> None:
    os.chdir(APP)
    n = gpu_count()
    tensor_split = "1,1" if n >= 2 else "1"
    # single-GPU sessions (P100 16 GB): smaller ctx leaves VRAM headroom for
    # the 12 GB Q6 model + KV cache; dual T4 keeps the full window
    ctx = 16384 if n >= 2 else 12288
    print(f"GPUs visible: {n} -> tensor_split={tensor_split} ctx={ctx}")

    overlay = OUT / "acceptance_overlay.yaml"
    overlay.write_text(
        "server:\n"
        "  host: 127.0.0.1\n"
        "logging:\n"
        "  level: info\n"
        "kaggle:\n"
        "  model_dir: /kaggle/tmp/models\n"
        "  llama_server:\n"
        f"    tensor_split: \"{tensor_split}\"\n"
        f"    ctx_size: {ctx}\n",
        encoding="utf-8",
    )
    os.environ["KOOSYS_CONFIG_OVERLAY"] = str(overlay)

    from deploy import bootstrap as bs  # reads config AFTER the overlay is set

    rc = 1
    try:
        seed_llama_from_dataset()
        bs.install_deps()
        server = bs.get_llama_server()
        model = bs.download_model()
        bs.start_llama(server, model)
        bs.start_backend()
        export_llama_for_cache()

        env = dict(
            os.environ,
            KOOSYS_URL="http://127.0.0.1:8000",
            EVAL_TIMEOUT="1800",
            EVAL_REPORT=str(OUT / "last_report.json"),
            PYTHONPATH=str(APP),
        )
        # if the real-world public-code sample dataset is attached, review it
        # through the full pipeline (report-only) to measure LLM FP on code
        # neither the tool nor the tests were written against
        wild = Path("/kaggle/input/koosys-wild-sample")
        if wild.exists():
            env["EVAL_EXTRA_DIR"] = str(wild)
            print(f"wild-sample dataset mounted -> EVAL_EXTRA_DIR={wild}")
        # run by file path — immune to `tests` package shadowing from any
        # site-packages on the host image (accuracy_eval has no repo imports)
        rc = subprocess.run(
            [sys.executable, str(APP / "tests" / "accuracy_eval.py")],
            env=env, cwd=APP).returncode
        # Phase 2: validate the guideline (RAG) layer against the real model
        print("\n===== RAG guideline smoke (real LLM) =====", flush=True)
        subprocess.run([sys.executable, str(APP / "tests" / "rag_smoke.py")],
                       env=env, cwd=APP)
    finally:
        # ALWAYS ship the service logs — a boot failure without
        # llama-server.log is undiagnosable from the kernel log alone
        if bs.LOG_DIR.exists():
            for f in bs.LOG_DIR.glob("*.log"):
                shutil.copy2(f, OUT / f.name)
        # a failed run must still export the built binary for caching
        export_llama_for_cache()
    print(f"\nacceptance eval exit code: {rc}")
    print(f"outputs in {OUT}: last_report.json + service logs")
    sys.exit(rc)


if __name__ == "__main__":
    main()
