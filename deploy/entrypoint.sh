#!/usr/bin/env bash
# Start llama-server (across the GPUs) + the FastAPI backend. No public tunnel.
# Supervises both: if either dies the container exits (restart policy restarts
# it); SIGTERM shuts both down cleanly so in-flight reviews are marked.
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/models}"
MODEL_REPO="${MODEL_REPO:-bartowski/Qwen2.5-Coder-14B-Instruct-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen2.5-Coder-14B-Instruct-Q6_K.gguf}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
CTX_SIZE="${CTX_SIZE:-16384}"
NGL="${NGL:-999}"
TENSOR_SPLIT="${TENSOR_SPLIT:-1,1}"     # split across 2 GPUs; set "" for single
PARALLEL="${PARALLEL:-2}"
export PYTHONPATH=/app

mkdir -p "$MODEL_DIR"
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"

if [ ! -f "$MODEL_PATH" ]; then
  echo "[entrypoint] downloading model $MODEL_FILE ..."
  python3 - <<PY
from huggingface_hub import hf_hub_download
import os
hf_hub_download(repo_id="$MODEL_REPO", filename="$MODEL_FILE",
                local_dir="$MODEL_DIR", token=os.environ.get("HF_TOKEN") or None)
PY
fi

echo "[entrypoint] starting llama-server on $LLAMA_HOST:$LLAMA_PORT ..."
SPLIT_ARGS=()
if [ -n "$TENSOR_SPLIT" ]; then SPLIT_ARGS=(--split-mode layer --tensor-split "$TENSOR_SPLIT"); fi
llama-server \
  --model "$MODEL_PATH" \
  --host "$LLAMA_HOST" --port "$LLAMA_PORT" \
  --ctx-size "$CTX_SIZE" --n-gpu-layers "$NGL" \
  --parallel "$PARALLEL" --alias "${LLM_MODEL:-qwen2.5-coder-14b-instruct}" \
  "${SPLIT_ARGS[@]}" &
LLAMA_PID=$!

# wait for llama-server to be ready
for i in $(seq 1 120); do
  if curl -sf "http://$LLAMA_HOST:$LLAMA_PORT/v1/models" >/dev/null 2>&1; then
    echo "[entrypoint] llama-server ready"; break
  fi
  if ! kill -0 $LLAMA_PID 2>/dev/null; then echo "[entrypoint] llama-server died"; exit 1; fi
  sleep 3
done

echo "[entrypoint] starting backend ..."
python3 -m backend.main &
BACKEND_PID=$!

shutdown() {
  echo "[entrypoint] stopping ..."
  kill -TERM "$BACKEND_PID" "$LLAMA_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# exit when EITHER process dies; propagate its exit code
set +e
wait -n "$LLAMA_PID" "$BACKEND_PID"
CODE=$?
shutdown
wait
exit $CODE
