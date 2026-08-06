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
sys.path.insert(0, str(APP))


def gpu_count() -> int:
    p = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
    return (p.stdout or "").count("GPU ")


def main() -> None:
    os.chdir(APP)
    n = gpu_count()
    tensor_split = "1,1" if n >= 2 else "1"
    print(f"GPUs visible: {n} -> tensor_split={tensor_split}")

    overlay = OUT / "acceptance_overlay.yaml"
    overlay.write_text(
        "server:\n"
        "  host: 127.0.0.1\n"
        "logging:\n"
        "  level: info\n"
        "kaggle:\n"
        "  model_dir: /kaggle/tmp/models\n"
        "  llama_server:\n"
        f"    tensor_split: \"{tensor_split}\"\n",
        encoding="utf-8",
    )
    os.environ["KOOSYS_CONFIG_OVERLAY"] = str(overlay)

    from deploy import bootstrap as bs  # reads config AFTER the overlay is set

    bs.install_deps()
    server = bs.get_llama_server()
    model = bs.download_model()
    bs.start_llama(server, model)
    bs.start_backend()

    env = dict(
        os.environ,
        KOOSYS_URL="http://127.0.0.1:8000",
        EVAL_TIMEOUT="1800",
        EVAL_REPORT=str(OUT / "last_report.json"),
        PYTHONPATH=str(APP),
    )
    rc = subprocess.run([sys.executable, "-m", "tests.accuracy_eval"],
                        env=env, cwd=APP).returncode

    for f in bs.LOG_DIR.glob("*.log"):
        shutil.copy2(f, OUT / f.name)
    print(f"\nacceptance eval exit code: {rc}")
    print(f"outputs in {OUT}: last_report.json + service logs")
    sys.exit(rc)


if __name__ == "__main__":
    main()
