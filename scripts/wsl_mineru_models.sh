#!/usr/bin/env bash
# Download MinerU 4 models for the standard/advanced tier (torch small models + vLLM VLM).
set -e
source /opt/ocr/mineru/bin/activate
export HF_HOME=/opt/ocr/cache/hf MODELSCOPE_CACHE=/opt/ocr/cache/modelscope
mineru-kit models show
mineru-kit models download --tier basic --small-backend torch --source ${MINERU_SRC:-huggingface} -v
mineru-kit models download --tier standard --small-backend torch --vlm-engine vllm --source ${MINERU_SRC:-huggingface} -v
mineru-kit models verify --tier standard
mineru-kit models show
