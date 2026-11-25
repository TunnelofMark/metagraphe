#!/usr/bin/env bash
# Whole-book PP-DocLayoutV3 pass (full-book version of run_layout.sh; same model and run_layout.py).
#   run_layout_full.sh <source.pdf (WSL path, read-only)> <pages.json> <layout_dir>
# Output: <layout_dir>/pNNNN_res.json + timing.json, <layout_dir>/run.{time.json,gpu.csv,stdout.log}
set -eu
pdf=$1; pages=$2; out=$3
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ocr/paddle/bin/activate
export PADDLE_PDX_CACHE_HOME=/opt/ocr/cache/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
mkdir -p "$out"
n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$pages")
bash $P/scripts/run_with_gpu.sh "$out/run" "$n" -- \
  python $P/scripts/run_layout.py "$pdf" "$pages" "$out"
cat "$out/timing.json"
