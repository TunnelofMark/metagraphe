#!/usr/bin/env bash
# Post-process one tool/book: adapter -> doc.json, assemble (recrop, md, epub), epubcheck, two-layer PDF.
#   post_all.sh <tool_dir_name> <book>      e.g. post_all.sh mineru_advanced xigang
set -eu
tool=$1; book=$2
P=/mnt/e/project/provenance
S=$P/scripts
D=$P/ocr_test/$tool/$book
PY=/opt/ocr/post/bin/python
src=$(python3 -c "import json;print(json.load(open('$S/books.json'))['$book']['wsl_pdf'])")
title=$(python3 -c "import json;print(json.load(open('$S/books.json'))['$book']['title'])")
pages=$(python3 -c "import json;print(json.load(open('$S/books.json'))['$book']['test_pages'])")
DOC=$D/doc.json
case $tool in
  mineru*)
    $PY $S/adapt_mineru.py "$D" "$P/ocr_test/source/$book/pages.json"
    # figure regions from PP-DocLayoutV3 (layout jsons of the PaddleOCR-VL / layout-only run)
    LAY=${LAYOUT_DIR:-$P/ocr_test/paddleocr_vl/$book/raw}
    if [ -d "$LAY" ]; then
      $PY $S/hybrid_images.py "$D/doc.json" "$LAY" "$D/doc_hybrid.json" --log "$D/hybrid_log.json"
      DOC=$D/doc_hybrid.json
    fi ;;
  paddleocr_vl|ppstructurev3) $PY $S/adapt_paddle.py "$D" "$P/ocr_test/source/$book/pages.json" ;;
esac
cd $S
$PY assemble.py "$DOC" "$src" "$D/final" --title "$title（测试样品）"
java -jar /opt/ocr/epubcheck/epubcheck.jar "$D/final/book.epub" > "$D/final/epubcheck.txt" 2>&1 || true
tail -3 "$D/final/epubcheck.txt"
$PY build_pdf.py "$src" "$D/final/book.json" "$D/final/${book}_双层_测试样品.pdf" --pages "$pages"
