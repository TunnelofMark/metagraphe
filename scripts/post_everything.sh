#!/usr/bin/env bash
# Re-run adapters + assemble + epubcheck + two-layer PDF for every tool/book, then MinerU's own EPUB.
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in mineru_advanced paddleocr_vl ppstructurev3; do
  for book in xigang rizhao; do
    echo "##### $tool $book"
    bash $P/scripts/post_all.sh $tool $book 2>&1 | grep -v -E "WARNING|translations|Abstract|^\s*$"
  done
done
for book in xigang rizhao; do
  title=$(python3 -c "import json;print(json.load(open('$P/scripts/books.json'))['$book']['title'])")
  /opt/ocr/mineru/bin/python $P/scripts/mineru_native_epub.py $P/ocr_test/mineru_advanced/$book/raw \
      $P/ocr_test/mineru_advanced/$book/final/${book}_mineru_native.epub "$title（MinerU 原生 EPUB）"
  java -jar /opt/ocr/epubcheck/epubcheck.jar $P/ocr_test/mineru_advanced/$book/final/${book}_mineru_native.epub \
      > $P/ocr_test/mineru_advanced/$book/final/epubcheck_native.txt 2>&1
  tail -2 $P/ocr_test/mineru_advanced/$book/final/epubcheck_native.txt
done
