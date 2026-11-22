#!/usr/bin/env bash
# Start or resume the batch (completed stages are skipped):
#   $env:WSL_UTF8=1; wsl -d Ubuntu-24.04 -u root -- bash /mnt/e/project/provenance/scripts/run_all.sh [--only NAME ...]
mkdir -p /mnt/e/project/provenance/ocr_full/_batch
exec python3 /mnt/e/project/provenance/scripts/run_all.py "$@" >> /mnt/e/project/provenance/ocr_full/_batch/run_all.console.log 2>&1
