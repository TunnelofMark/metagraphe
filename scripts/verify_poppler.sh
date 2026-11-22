#!/usr/bin/env bash
# poppler check: font embedding/ToUnicode and term counts via pdftotext.  verify_poppler.sh <pdf> <term>...
f=$1; shift
pdffonts "$f" | sed -n 3,6p
txt=$(pdftotext -enc UTF-8 "$f" -)
for t in "$@"; do printf '%s: %s\n' "$t" "$(grep -o -- "$t" <<<"$txt" | wc -l)"; done
