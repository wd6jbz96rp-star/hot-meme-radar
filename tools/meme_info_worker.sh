#!/usr/bin/env bash
# meme_info 报告刷新 worker（10 分钟一轮，串行）。
#
# 用法：
#   tools/meme_info_worker.sh                 # 前台循环
#   MEME_INFO_WORKER_ONCE=1 tools/meme_info_worker.sh  # 只跑一轮（测试）
#
# 环境：
#   MEME_INFO_WORKER_INTERVAL=600     轮间隔（秒）
#   MEME_INFO_WORKER_ONCE=1           只跑一轮后退出
#   MEME_INFO_VERIFY=0|1              是否启用 LLM 语义审核；默认 0（2026-11-16 暂停）
#   MEME_INFO_REPORT=/tmp/meme_info_latest.json
#   MEME_INFO_CACHE_DIR=/tmp/meme_info_cache
#   HOTMEME_LLM_API_KEY_FILE=~/.hotmeme_llm_key
#
# 约束：只写报告 JSON 与 /tmp 日志；工具自身拒绝写 .db/data/，不碰 app/生产库。
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

INTERVAL="${MEME_INFO_WORKER_INTERVAL:-600}"
ONCE="${MEME_INFO_WORKER_ONCE:-0}"
VERIFY="${MEME_INFO_VERIFY:-0}"
REPORT="${MEME_INFO_REPORT:-/tmp/meme_info_latest.json}"
CACHE="${MEME_INFO_CACHE_DIR:-/tmp/meme_info_cache}"
KEY_FILE="${HOTMEME_LLM_API_KEY_FILE:-$HOME/.hotmeme_llm_key}"
LOG="${MEME_INFO_WORKER_LOG:-/tmp/meme_info_worker.log}"
VERIFY_FLAGS=""
if [ "$VERIFY" = "1" ]; then
  VERIFY_FLAGS="--verify --verify-gate"
fi

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG"; }

if [ -f "$KEY_FILE" ]; then
  export HOTMEME_LLM_API_KEY_FILE="$KEY_FILE"
fi

log "worker start interval=${INTERVAL}s verify=${VERIFY} report=${REPORT} cache=${CACHE} key=$( [ -f "$KEY_FILE" ] && echo configured || echo missing )"
trap 'log "worker stopped"; exit 0' TERM INT

while true; do
  started=$(date +%s)
  log "run begin"
  if .venv/bin/python -m tools.meme_info_shadow \
      --out "$REPORT" --cache-dir "$CACHE" --limit 20 \
      $VERIFY_FLAGS >> "$LOG" 2>&1; then
    log "run ok $(( $(date +%s) - started ))s"
  else
    log "run failed rc=$? $(( $(date +%s) - started ))s"
  fi
  if [ "$ONCE" = "1" ]; then
    log "worker once done"
    exit 0
  fi
  sleep "$INTERVAL"
done
