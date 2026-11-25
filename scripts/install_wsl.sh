#!/usr/bin/env bash
# Reproducible environment setup inside WSL2 Ubuntu 24.04 (run as root).
#   wsl -d Ubuntu-24.04 -u root -- bash /mnt/e/project/metagraphe/scripts/install_wsl.sh [step...]
# Steps: base mineru paddle paddle_vllm post   (default: all)
# Every env lives under /opt/ocr/<name>; model caches under /opt/ocr/cache.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ROOT=/opt/ocr
export UV_CACHE_DIR=$ROOT/cache/uv
export HF_HOME=$ROOT/cache/hf
export MODELSCOPE_CACHE=$ROOT/cache/modelscope
export PADDLE_PDX_CACHE_HOME=$ROOT/cache/paddlex
export PATH=/root/.local/bin:$PATH
mkdir -p $ROOT/cache

step_base() {
  apt-get update -q
  apt-get install -y -q build-essential git curl ca-certificates unzip \
      fonts-noto-cjk default-jre-headless libgl1 libglib2.0-0 libjpeg-turbo-progs python3-dev
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  uv --version
}

step_mineru() {
  # Tool A: MinerU 4.x, NVIDIA/Linux "full" extra = torch small models + vLLM VLM engine
  uv venv --clear --python 3.12 $ROOT/mineru
  VIRTUAL_ENV=$ROOT/mineru uv pip install -U "mineru[full]>=4.0,<5"
  $ROOT/mineru/bin/python -c "import importlib.metadata as m; print('mineru', m.version('mineru')); import torch; print('torch', torch.__version__, torch.version.cuda, torch.cuda.is_available())"
}

step_paddle() {
  # Tool B: PaddleOCR 3.x (PaddleOCR-VL 1.6 pipeline + PP-StructureV3), Paddle GPU runtime
  uv venv --clear --python 3.12 $ROOT/paddle
  VIRTUAL_ENV=$ROOT/paddle uv pip install "paddlepaddle-gpu==${PADDLE_VER:-3.2.1}" \
      --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
      --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match
  # pymupdf: run_layout.py renders pages itself
  VIRTUAL_ENV=$ROOT/paddle uv pip install -U "paddleocr[doc-parser]>=3.6.0" pymupdf
  $ROOT/paddle/bin/python -c "import paddle, importlib.metadata as m; print('paddle', paddle.__version__, paddle.device.is_compiled_with_cuda()); print('paddleocr', m.version('paddleocr'))"
}

step_paddle_vllm() {
  # Separate env for the PaddleOCR-VL vLLM inference server (per official docs)
  [ -x $ROOT/paddle_vllm/bin/python ] || uv venv --python 3.12 $ROOT/paddle_vllm
  VIRTUAL_ENV=$ROOT/paddle_vllm uv pip install -U "paddleocr[doc-parser]>=3.6.0" pip
  # The official `paddleocr install_genai_server_deps vllm` also builds flash-attn from source (needs
  # nvcc) and aborts the whole batch when that fails. Install exactly what the plugin check requires
  # (paddlex EXTRAS["genai-vllm-server"]); vLLM ships its own flash-attention kernels.
  VIRTUAL_ENV=$ROOT/paddle_vllm uv pip install "vllm==0.10.2" "torch==2.8.0" "transformers<5.0.0" einops uvloop
  # On CUDA the plugin check also requires flash-attn: use the upstream (Dao-AILab) prebuilt wheel
  # matching torch 2.8 / cp312 / the torch C++ ABI instead of compiling it.
  ABI=$($ROOT/paddle_vllm/bin/python -c "import torch; print('TRUE' if torch._C._GLIBCXX_USE_CXX11_ABI else 'FALSE')")
  VIRTUAL_ENV=$ROOT/paddle_vllm uv pip install \
    "https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3.post2/flash_attn-2.8.3.post2+cu12torch2.8cxx11abi${ABI}-cp312-cp312-linux_x86_64.whl"
  $ROOT/paddle_vllm/bin/python -c "import importlib.metadata as m; print('vllm', m.version('vllm'))
from paddlex.utils.deps import is_genai_engine_plugin_available as f; print('vllm plugin ok:', f())"
}

step_post() {
  # Post-processing: PDF text layer, image re-crop, EPUB
  uv venv --clear --python 3.12 $ROOT/post
  VIRTUAL_ENV=$ROOT/post uv pip install pymupdf pikepdf pillow numpy lxml rapidfuzz
  PANDOC_VER=3.6.4
  if ! command -v pandoc >/dev/null; then
    curl -L -o /tmp/pandoc.deb https://github.com/jgm/pandoc/releases/download/$PANDOC_VER/pandoc-$PANDOC_VER-1-amd64.deb
    apt-get install -y -q /tmp/pandoc.deb
  fi
  EPUBCHECK_VER=5.2.1
  if [ ! -f $ROOT/epubcheck/epubcheck.jar ]; then
    curl -L -o /tmp/epubcheck.zip https://github.com/w3c/epubcheck/releases/download/v$EPUBCHECK_VER/epubcheck-$EPUBCHECK_VER.zip
    unzip -q -o /tmp/epubcheck.zip -d /tmp && rm -rf $ROOT/epubcheck && mv /tmp/epubcheck-$EPUBCHECK_VER $ROOT/epubcheck
  fi
  pandoc --version | head -1; java -jar $ROOT/epubcheck/epubcheck.jar --version | head -1
}

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(base mineru paddle paddle_vllm post)
for s in "${steps[@]}"; do echo "=== step $s ==="; "step_$s"; done
