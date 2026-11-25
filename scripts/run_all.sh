#!/usr/bin/env bash
# Start or resume the batch (completed stages are skipped):
#   $env:WSL_UTF8=1; wsl -d Ubuntu-24.04 -u root -- bash /mnt/e/project/metagraphe/scripts/run_all.sh [--only NAME ...]
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$P/ocr_full/_batch"
exec python3 "$P/scripts/run_all.py" "$@" >> "$P/ocr_full/_batch/run_all.console.log" 2>&1
