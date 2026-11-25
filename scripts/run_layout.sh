#!/usr/bin/env bash
# Layout-only pass (PP-DocLayoutV3) for hybrid_images.py: run_layout.sh <book>
# Output: ocr_test/layout_only/<book>/pNNNN_res.json  (pass as LAYOUT_DIR to post_all.sh)
set -eu
book=$1
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ocr/paddle/bin/activate
export PADDLE_PDX_CACHE_HOME=/opt/ocr/cache/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
out=$P/ocr_test/layout_only/$book
mkdir -p "$out"
n=$(python3 -c "import json;print(len(json.load(open('$P/ocr_test/source/$book/pages.json'))))")
bash $P/scripts/run_with_gpu.sh "$out/run" "$n" -- \
  python $P/scripts/run_layout.py "$P/ocr_test/source/$book/test.pdf" "$P/ocr_test/source/$book/pages.json" "$out"
cat "$out/timing.json"
