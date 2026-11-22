#!/usr/bin/env bash
# Dump the Python stack of a running process matched by pattern: wsl_pyspy.sh "<pgrep -f pattern>"
export PATH=/root/.local/bin:$PATH
[ -x /opt/ocr/paddle/bin/py-spy ] || VIRTUAL_ENV=/opt/ocr/paddle uv pip install -q py-spy
pid=$(pgrep -f "$1" | head -1)
echo "pid=$pid"
/opt/ocr/paddle/bin/py-spy dump --pid "$pid" 2>&1 | head -60
