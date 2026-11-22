#!/usr/bin/env bash
# Check which package / model sources are reachable from WSL (direct, no proxy).
for u in https://pypi.org/simple/mineru/ https://huggingface.co https://hf-mirror.com https://www.modelscope.cn \
         https://www.paddlepaddle.org.cn/packages/stable/cu126/ https://github.com https://astral.sh/uv/install.sh \
         https://mirrors.aliyun.com/pypi/simple/ https://download.pytorch.org/whl/cu128/ \
         https://paddle-model-ecology.bj.bcebos.com; do
  printf "%-60s " "$u"
  curl -s -o /dev/null -m 15 -w "%{http_code} %{time_total}s\n" "$u" || echo FAIL
done
