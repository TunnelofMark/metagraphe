#!/usr/bin/env bash
# Whole-book post-processing (full-book version of post_all.sh, MinerU branch; same steps and scripts):
#   adapt_mineru.py -> hybrid_images.py -> assemble.py -> epubcheck -> build_pdf.py (no --pages)
#   post_full.sh <source.pdf (WSL, read-only)> <book_out_dir> <book_name> <title>
# Reads <book_out_dir>/work/{raw,pages.json,layout}; writes the deliverables into <book_out_dir>.
set -eu
src=$1; out=$2; name=$3; title=$4
S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W=$out/work
PY=/opt/ocr/post/bin/python
$PY $S/adapt_mineru.py "$W" "$W/pages.json"
test -d "$W/layout"
$PY $S/hybrid_images.py "$W/doc.json" "$W/layout" "$W/doc_hybrid.json" --log "$W/hybrid_log.json" > "$out/logs/hybrid_images.log"
cp "$W/hybrid_log.json" "$out/hybrid_log.json"
cd $S
# assemble writes book.md, book.json, images/, epub.css, book.epub straight into the book folder
$PY assemble.py "$W/doc_hybrid.json" "$src" "$out" --title "$title"
mv -f "$out/book.epub" "$out/$name.epub"
java -jar /opt/ocr/epubcheck/epubcheck.jar "$out/$name.epub" > "$out/logs/epubcheck.txt" 2>&1 || true
tail -3 "$out/logs/epubcheck.txt"
$PY build_pdf.py "$src" "$out/book.json" "$out/${name}_双层.pdf" | tee "$out/logs/build_pdf.json"
