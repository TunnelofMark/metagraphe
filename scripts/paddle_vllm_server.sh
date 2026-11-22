#!/usr/bin/env bash
# Start the PaddleOCR-VL 1.6 vLLM inference server (port 8118). GPU share capped because the
# Windows desktop already holds ~3.4 GB of the 10 GB and the layout model runs beside it.
source /opt/ocr/paddle_vllm/bin/activate
export HF_HOME=/opt/ocr/cache/hf PADDLE_PDX_CACHE_HOME=/opt/ocr/cache/paddlex
cat > /opt/ocr/paddle_vllm/backend.yaml <<EOF
gpu-memory-utilization: ${GPU_UTIL:-0.5}
max-num-seqs: ${MAX_SEQS:-32}
# PaddleX's vLLM default is 131072 (sized for big GPUs); 16384 keeps the profiling peak inside 10 GB
max-num-batched-tokens: ${MAX_BATCHED:-16384}
EOF
exec paddlex_genai_server --model_name PaddleOCR-VL-1.6-0.9B --host 127.0.0.1 --port 8118 \
     --backend vllm --backend_config /opt/ocr/paddle_vllm/backend.yaml
