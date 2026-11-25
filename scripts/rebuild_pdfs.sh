#!/usr/bin/env bash
# Rebuild only the two-layer PDFs from existing final/book.json (after build_pdf.py changes).
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S=$P/scripts
cd $S
for tool in mineru_advanced paddleocr_vl ppstructurev3; do
  for book in xigang rizhao; do
    src=$(python3 -c "import json;print(json.load(open('$S/books.json'))['$book']['wsl_pdf'])")
    pages=$(python3 -c "import json;print(json.load(open('$S/books.json'))['$book']['test_pages'])")
    /opt/ocr/post/bin/python build_pdf.py "$src" "$P/ocr_test/$tool/$book/final/book.json" \
      "$P/ocr_test/$tool/$book/final/${book}_双层_测试样品.pdf" --pages "$pages"
  done
done
