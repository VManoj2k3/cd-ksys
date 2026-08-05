# Cd koosys — on-prem image (GPU code review service)
# Builds llama.cpp with CUDA for the target GPU and bundles the analysis
# toolchains. Default CUDA arch targets Blackwell (RTX 50-series = sm_120).
#
#   docker build -t koosys --build-arg CUDA_ARCH=120 -f deploy/Dockerfile .
#
# RTX 5070 (Blackwell) needs CUDA 12.8+ and a recent driver (>= 570).
ARG CUDA_IMAGE=nvidia/cuda:12.8.0-devel-ubuntu22.04
FROM ${CUDA_IMAGE}

ARG CUDA_ARCH=120
ARG LLAMA_CPP_TAG=master
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

# ---- system + analysis toolchains ----
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip git cmake build-essential curl ca-certificates \
      cppcheck clang-tidy flawfinder \
      nodejs npm openjdk-17-jre-headless unzip \
    && rm -rf /var/lib/apt/lists/*

# ---- build llama.cpp (CUDA) ----
RUN git clone --depth 1 --branch ${LLAMA_CPP_TAG} \
      https://github.com/ggml-org/llama.cpp /opt/llama.cpp && \
    cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
      -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} && \
    cmake --build /opt/llama.cpp/build --target llama-server -j && \
    cp /opt/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server && \
    cp /opt/llama.cpp/build/bin/*.so* /usr/local/lib/ 2>/dev/null || true && \
    ldconfig && rm -rf /opt/llama.cpp

# ---- app + python deps ----
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ---- Java PMD (optional layer for Java review) ----
ARG PMD_VERSION=7.7.0
RUN mkdir -p /opt/tools && cd /opt/tools && \
    curl -fsSL -o pmd.zip \
      https://github.com/pmd/pmd/releases/download/pmd_releases%2F${PMD_VERSION}/pmd-dist-${PMD_VERSION}-bin.zip && \
    unzip -q pmd.zip && rm pmd.zip

COPY . /app

# ---- eslint toolchain (JS/TS) warmed at build ----
RUN python3 -c "import sys; sys.path.insert(0,'/app'); \
    from backend.languages.typescript import ensure_eslint; print('eslint:', bool(ensure_eslint()))" || true

EXPOSE 8000 8080
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
