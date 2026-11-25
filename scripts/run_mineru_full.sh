#!/usr/bin/env bash
# Whole-book MinerU run (full-book version of run_mineru.sh; same command, tier and env).
#   run_mineru_full.sh <source.pdf (WSL path, read-only)> <work_dir> <n_pages>
# Writes <work_dir>/result.zip, <work_dir>/raw/ (unzipped), <work_dir>/mineru.{time.json,gpu.csv,stdout.log}
set -eu
pdf=$1; work=$2; npages=$3
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ocr/mineru/bin/activate
export HF_HOME=/opt/ocr/cache/hf MODELSCOPE_CACHE=/opt/ocr/cache/modelscope
export MINERU_MODEL_SMALL_BACKEND=torch MINERU_MODEL_VLM_ENGINE=vllm
# no nvcc in WSL: use the PyTorch top-k/top-p sampler instead of the flashinfer JIT kernel
export VLLM_USE_FLASHINFER_SAMPLER=0
mkdir -p "$work"
rm -f "$work/result.zip"
# --ocr-mode ocr: never trust the PDF's old 超星 text layer
bash $P/scripts/run_with_gpu.sh "$work/mineru" "$npages" -- \
  mineru-kit parse "$pdf" -o "$work/result.zip" -f zip --tier advanced --ocr-mode ocr -p all -v
cd "$work" && rm -rf raw && mkdir raw && cd raw && unzip -q ../result.zip
test -f "$work/raw/middle_json.json"
