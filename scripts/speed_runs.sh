#!/usr/bin/env bash
# Steady-state throughput on 100 consecutive pages per book (startup measured separately).
P=/mnt/e/project/provenance
S=$P/scripts
# A: MinerU advanced (model load is inside the run; subtract the 2-page smoke run's fixed cost)
for b in xigangspeed rizhaospeed; do bash $S/run_mineru.sh $b advanced > /dev/null 2>&1; done
# B: PaddleOCR-VL via vLLM server (server start is a one-off per batch, logged separately)
GPU_UTIL=0.5 nohup bash $S/paddle_vllm_server.sh > $P/ocr_test/logs/paddle_vllm_server_speed.log 2>&1 &
t0=$(date +%s)
until grep -q "Application startup complete" $P/ocr_test/logs/paddle_vllm_server_speed.log; do sleep 3; done
echo "vllm_server_startup_s=$(( $(date +%s) - t0 ))" > $P/ocr_test/logs/vllm_startup.txt
for b in xigangspeed rizhaospeed; do bash $S/run_paddle.sh vl $b > /dev/null 2>&1; done
pkill -f paddlex_genai_server
# PP-StructureV3
for b in xigangspeed rizhaospeed; do bash $S/run_paddle.sh ppsv3 $b > /dev/null 2>&1; done
cat $P/ocr_test/*/*speed/run.time.json $P/ocr_test/logs/vllm_startup.txt
