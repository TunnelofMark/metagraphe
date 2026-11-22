#!/usr/bin/env bash
# Tool B runner: run_paddle.sh vl|ppsv3 <book>
# vl needs the vLLM server from paddle_vllm_server.sh on 127.0.0.1:8118
set -eu
tool=$1; book=$2
P=/mnt/e/project/provenance
source /opt/ocr/paddle/bin/activate
export PADDLE_PDX_CACHE_HOME=/opt/ocr/cache/paddlex HF_HOME=/opt/ocr/cache/hf
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
dir=$([ "$tool" = vl ] && echo paddleocr_vl || echo ppstructurev3)
out=$P/ocr_test/$dir/$book; mkdir -p "$out"
n=$(python3 -c "import json;print(len(json.load(open('$P/ocr_test/source/$book/pages.json'))))")
extra=$([ "$tool" = vl ] && echo "--server http://127.0.0.1:8118/v1" || true)
bash $P/scripts/run_with_gpu.sh "$out/run" "$n" -- python $P/scripts/run_paddle.py "$tool" "$book" $extra
