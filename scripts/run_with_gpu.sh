#!/usr/bin/env bash
# Run a command while sampling GPU memory every 0.5 s; write <log_prefix>.gpu.csv and <log_prefix>.time.json
#   run_with_gpu.sh <log_prefix> <n_pages> -- <command...>
set -u
prefix=$1; npages=$2; shift 3
mkdir -p "$(dirname "$prefix")"
nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv,noheader,nounits -lms 500 > "$prefix.gpu.csv" &
mon=$!
base=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
t0=$(date +%s.%N)
"$@" > "$prefix.stdout.log" 2>&1
rc=$?
t1=$(date +%s.%N)
kill $mon 2>/dev/null; wait $mon 2>/dev/null
peak=$(awk -F', ' 'BEGIN{m=0}{if($2+0>m)m=$2+0}END{print m}' "$prefix.gpu.csv")
python3 - "$prefix" "$npages" "$t0" "$t1" "$base" "$peak" "$rc" "$*" <<'EOF'
import sys, json
p, n, t0, t1, base, peak, rc, cmd = sys.argv[1:]
el = float(t1) - float(t0)
d = {"cmd": cmd, "rc": int(rc), "pages": int(n), "seconds": round(el, 1), "sec_per_page": round(el / int(n), 2),
     "gpu_mem_baseline_mib": int(base), "gpu_mem_peak_mib": int(peak), "gpu_mem_delta_mib": int(peak) - int(base)}
open(p + ".time.json", "w").write(json.dumps(d, indent=1))
print(json.dumps(d))
EOF
exit $rc
