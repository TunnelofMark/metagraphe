#!/usr/bin/env bash
# Tool A: MinerU 4.x on a book's test subset.
#   run_mineru.sh <book> <tier> [pages]
# Outputs: <repo>/ocr_test/mineru_<tier>/<book>/
set -eu
book=$1; tier=$2; pages=${3:-all}
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ocr/mineru/bin/activate
export HF_HOME=/opt/ocr/cache/hf MODELSCOPE_CACHE=/opt/ocr/cache/modelscope
export MINERU_MODEL_SMALL_BACKEND=torch MINERU_MODEL_VLM_ENGINE=vllm
# no nvcc in WSL: use the PyTorch top-k/top-p sampler instead of the flashinfer JIT kernel
export VLLM_USE_FLASHINFER_SAMPLER=0
out=$P/ocr_test/mineru_$tier/$book${OUT_SUFFIX:-}
mkdir -p "$out"
# --ocr-mode ocr: never trust the PDF's old 超星 text layer
bash $P/scripts/run_with_gpu.sh "$out/run" "${NPAGES:-$(python3 -c "import json;print(len(json.load(open('$P/ocr_test/source/$book/pages.json'))))")}" -- \
  mineru-kit parse "$P/ocr_test/source/$book/test.pdf" -o "$out/result.zip" -f zip --tier "$tier" --ocr-mode ocr -p "$pages" -v
cd "$out" && rm -rf raw && mkdir raw && cd raw && unzip -q ../result.zip && ls -R | head -30
