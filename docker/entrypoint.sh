#!/usr/bin/env bash
set -e
echo "== ragkernel =="
if [ ! -d "${HF_HOME:-/models}/hub" ]; then
  echo ">> 首次启动：下载本地模型（bge-m3 + reranker，约 2GB，仅此一次）…"
fi
# 预载模型（下载/校验），失败不阻断（首个请求会重试）
ragkernel models || true
echo ">> 启动 Web 服务 :8360"
exec ragkernel serve
