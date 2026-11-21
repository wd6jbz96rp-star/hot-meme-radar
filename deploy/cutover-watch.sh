#!/usr/bin/env bash
# Phase G 自动切流看守（cutover watcher）：每 5 分钟检查一次影子 v2 报告，
# 满足切流条件才自动执行 deploy/cutover.sh。**安全优先**：
#
#   1) flock 防 timer 重叠（拿不到锁直接 exit 0，等下一轮）；
#   2) 生产报告已是 v2/ok → 认为已切流完成，退出并停用本 watcher timer；
#   3) 先跑 `deploy/cutover.sh --check-only`：失败 → 记一行 wait 日志后 exit 0（不切流）；
#   4) --check-only 通过才执行 `deploy/cutover.sh`；
#   5) 切流成功 → 记成功时间/报告/回滚点，停用 watcher timer；
#   6) 切流失败（或成功退出但报告不是 v2/ok）→ **自动回滚**：
#        a. 停用 state-worker timer 并停掉正在跑的轮次（先切断新写入）；
#        b. 恢复生产报告 var/meme_info_latest.json.before-phase-g（tmp + mv，原子替换）；
#        c. `systemctl enable --now hotmeme-worker`（旧 worker 立即恢复写 v1）；
#        d. 停用 watcher timer（避免对陈旧影子报告反复重试），写 failure marker；
#        e. 把 cutover 输出尾部写日志，exit 1。
#
# 日志：`var/cutover_watch.log`（不含 token/密码；prod/shadow 报告内容不落日志）。
#
# 环境变量（都有默认值；便于 /tmp 沙盒测试，生产 unit 不需要设置）：
#   HOTMEME_REPO_DIR / HOTMEME_SHADOW_REPORT / HOTMEME_PROD_REPORT / HOTMEME_BACKUP_REPORT
#   HOTMEME_WATCH_LOG / HOTMEME_WATCH_LOCK
#   HOTMEME_WORKER_UNIT / HOTMEME_STATE_TIMER_UNIT / HOTMEME_STATE_SERVICE_UNIT
#   HOTMEME_WATCH_TIMER_UNIT / HOTMEME_CUTOVER_CMD / HOTMEME_SYSTEMCTL / HOTMEME_PYTHON
#
# 手工介入（回滚后想恢复等待）：
#   1) 重新装影子 drop-in：sudo cp deploy/shadow-dropin.conf \
#        /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf && sudo systemctl daemon-reload
#   2) sudo systemctl enable --now hotmeme-state-worker.timer hotmeme-cutover-watch.timer
#   3) 确认旧 worker 已停（sudo systemctl disable --now hotmeme-worker），删除 failure marker：
#        rm -f var/cutover_watch.failed
#
# 退出码：0 = 等待/完成/重叠跳过；1 = cutover 失败并已自动回滚（需要人工介入）。
set -uo pipefail

REPO_DIR="${HOTMEME_REPO_DIR:-/home/ubuntu/hotmeme}"
VAR_DIR="$REPO_DIR/var"
SHADOW_REPORT="${HOTMEME_SHADOW_REPORT:-$VAR_DIR/meme_info_v2_shadow.json}"
PROD_REPORT="${HOTMEME_PROD_REPORT:-$VAR_DIR/meme_info_latest.json}"
BACKUP_REPORT="${HOTMEME_BACKUP_REPORT:-$VAR_DIR/meme_info_latest.json.before-phase-g}"
WATCH_LOG="${HOTMEME_WATCH_LOG:-$VAR_DIR/cutover_watch.log}"
WATCH_LOCK="${HOTMEME_WATCH_LOCK:-$VAR_DIR/cutover_watch.lock}"
FAIL_MARKER="${HOTMEME_FAIL_MARKER:-$VAR_DIR/cutover_watch.failed}"
WORKER_UNIT="${HOTMEME_WORKER_UNIT:-hotmeme-worker}"
STATE_TIMER_UNIT="${HOTMEME_STATE_TIMER_UNIT:-hotmeme-state-worker.timer}"
STATE_SERVICE_UNIT="${HOTMEME_STATE_SERVICE_UNIT:-hotmeme-state-worker.service}"
WATCH_TIMER_UNIT="${HOTMEME_WATCH_TIMER_UNIT:-hotmeme-cutover-watch.timer}"
CUTOVER_CMD="${HOTMEME_CUTOVER_CMD:-$REPO_DIR/deploy/cutover.sh}"
SYSTEMCTL="${HOTMEME_SYSTEMCTL:-systemctl}"
PY="${HOTMEME_PY:-${HOTMEME_PYTHON:-$REPO_DIR/.venv/bin/python}}"

mkdir -p "$(dirname "$WATCH_LOG")" "$(dirname "$WATCH_LOCK")" 2>/dev/null || true
touch "$WATCH_LOG" 2>/dev/null && chmod 644 "$WATCH_LOG" 2>/dev/null || true

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$WATCH_LOG" >&2; }

# ---------------------------------------------------------------- flock 防重叠
exec 9>"$WATCH_LOCK" || { echo "cutover-watch: 无法打开锁文件 $WATCH_LOCK" >&2; exit 0; }
if ! flock -n 9; then
  log "skip: overlap（另一个 watcher 正在运行）"
  exit 0
fi

# ---------------------------------------------------------------- 工具函数
# 报告是否已是 v2/ok（只读 schema_version+status，不打印内容）
report_is_v2_ok() {
  "$PY" - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except Exception:
    sys.exit(0)
if report.get("schema_version") == "meme-info/v2" and report.get("status") == "ok":
    print("v2-ok")
PY
}

# 停 state worker：disable timer + stop service 并等待退出（超时 SIGKILL）。
# 失败回滚时必须先确认它真的停了，否则它可能继续往生产报告路径写（切流后 drop-in 已移除）。
stop_state_worker() {
  "$SYSTEMCTL" disable --now "$STATE_TIMER_UNIT" >>"$WATCH_LOG" 2>&1 \
    || log "WARN: 停用 $STATE_TIMER_UNIT 失败"
  if "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT"; then
    "$SYSTEMCTL" stop "$STATE_SERVICE_UNIT" >>"$WATCH_LOG" 2>&1 || true
    for _ in $(seq 1 60); do
      "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT" && { sleep 1; continue; }
      log "rollback: $STATE_SERVICE_UNIT 已停止"
      return 0
    done
    "$SYSTEMCTL" kill --signal=SIGKILL "$STATE_SERVICE_UNIT" >>"$WATCH_LOG" 2>&1 || true
    sleep 1
    if "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT"; then
      log "ERROR: $STATE_SERVICE_UNIT 无法停止（可能继续写报告，请人工检查）"
      return 1
    fi
    log "rollback: $STATE_SERVICE_UNIT 已停止（SIGKILL）"
  fi
  return 0
}

# 原子恢复生产报告（tmp + mv；失败保留原文件不删）
restore_production_report() {
  if [ ! -f "$BACKUP_REPORT" ]; then
    log "ERROR: 回滚报告不存在：$BACKUP_REPORT"
    return 1
  fi
  local tmp="$PROD_REPORT.restore-$$"
  if cp -a "$BACKUP_REPORT" "$tmp" && mv -f "$tmp" "$PROD_REPORT"; then
    chown ubuntu:ubuntu "$PROD_REPORT" 2>/dev/null || true
    chmod 644 "$PROD_REPORT" 2>/dev/null || true
    log "rollback: 生产报告已从 $BACKUP_REPORT 恢复"
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
  log "ERROR: 恢复生产报告失败（原文件未删除）：$BACKUP_REPORT"
  return 1
}

# ---------------------------------------------------------------- 1) 已切流则收工
if [ "$(report_is_v2_ok "$PROD_REPORT")" = "v2-ok" ]; then
  log "done: 生产报告已是 v2/ok（${PROD_REPORT}），停用 watcher timer"
  "$SYSTEMCTL" disable --now "$WATCH_TIMER_UNIT" >>"$WATCH_LOG" 2>&1 \
    || log "WARN: 停用 $WATCH_TIMER_UNIT 失败（可手工 systemctl disable --now）"
  exit 0
fi

# ---------------------------------------------------------------- 2) 影子报告存在性
if [ ! -f "$SHADOW_REPORT" ]; then
  log "wait: 影子报告尚未生成（$SHADOW_REPORT）"
  exit 0
fi

# ---------------------------------------------------------------- 3) 预检（不改变状态）
CHECK_OUT="$(mktemp /tmp/cutover-check.XXXXXX)" || exit 0
if ! "$CUTOVER_CMD" --check-only >"$CHECK_OUT" 2>&1; then
  reason="$(grep -E 'schema=|FAIL' "$CHECK_OUT" 2>/dev/null | tail -2 | tr '\n' ' ' | tr -d '\r' | cut -c1-300)"
  log "wait: cutover --check-only 未通过（${reason:-unknown}）；继续等待"
  rm -f "$CHECK_OUT" 2>/dev/null || true
  exit 0
fi
rm -f "$CHECK_OUT" 2>/dev/null || true

# ---------------------------------------------------------------- 4) 执行切流
log "ready: 预检通过，开始执行 cutover（$CUTOVER_CMD）"
CUT_OUT="$(mktemp /tmp/cutover-run.XXXXXX)" || exit 0
CUT_RC=0
if "$CUTOVER_CMD" >"$CUT_OUT" 2>&1; then
  CUT_RC=0
  rm -f "$CUT_OUT" 2>/dev/null || true
  if [ "$(report_is_v2_ok "$PROD_REPORT")" = "v2-ok" ]; then
    log "success: 切流完成 at $(date '+%Y-%m-%dT%H:%M:%S%z')；prod=$PROD_REPORT"
    log "success: 回滚点 report=$BACKUP_REPORT / last_good=$VAR_DIR/meme_info_latest.last_good.json；回滚命令：systemctl enable --now $WORKER_UNIT"
    rm -f "$FAIL_MARKER" 2>/dev/null || true
    "$SYSTEMCTL" disable --now "$WATCH_TIMER_UNIT" >>"$WATCH_LOG" 2>&1 \
      || log "WARN: 停用 $WATCH_TIMER_UNIT 失败（可手工 systemctl disable --now）"
    exit 0
  fi
  log "ERROR: cutover 退出码 0，但生产报告仍不是 v2/ok；按失败回滚"
else
  CUT_RC=$?
  if [ "$CUT_RC" = "3" ]; then
    # cutover.sh 安全中止：生产报告未改动，已自行恢复旧 worker + shadow timer，继续等待
    if [ "$(report_is_v2_ok "$PROD_REPORT")" = "v2-ok" ]; then
      log "success: cutover 返回 3 但生产报告已是 v2/ok（按成功处理）"
      rm -f "$CUT_OUT" 2>/dev/null || true
      "$SYSTEMCTL" disable --now "$WATCH_TIMER_UNIT" >>"$WATCH_LOG" 2>&1 || true
      exit 0
    fi
    log "abort: cutover 安全中止（rc=3，生产未切换）；继续等待下一轮"
    tail -5 "$CUT_OUT" 2>/dev/null | sed 's/^/  | /' >>"$WATCH_LOG"
    rm -f "$CUT_OUT" 2>/dev/null || true
    exit 0
  fi
  log "ERROR: cutover 执行失败（rc=$CUT_RC）；开始自动回滚"
fi

# ---------------------------------------------------------------- 5) 自动回滚
# a) 先切断并确认新链路写入已停（否则它在恢复旧报告后可能继续写生产路径）
stop_state_worker || log "ERROR: state worker 未确认停止，回滚继续但请人工检查"
# b) 恢复旧报告
restore_production_report || true
# c) 旧 worker 立即恢复写 v1
if "$SYSTEMCTL" enable --now "$WORKER_UNIT" >>"$WATCH_LOG" 2>&1; then
  log "rollback: $WORKER_UNIT 已恢复（enable --now）"
else
  log "ERROR: 恢复 $WORKER_UNIT 失败，生产可能无 worker！请人工执行：$SYSTEMCTL enable --now $WORKER_UNIT"
fi
# d) 停 watcher，避免对陈旧影子报告反复重试；写 failure marker
"$SYSTEMCTL" disable --now "$WATCH_TIMER_UNIT" >>"$WATCH_LOG" 2>&1 \
  || log "WARN: 停用 $WATCH_TIMER_UNIT 失败（可手工 systemctl disable --now）"
{
  printf 'failed_at=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
  printf 'cutover_rc=%s\n' "${CUT_RC:-0}"
  printf 'prod_report=%s\n' "$PROD_REPORT"
  printf 'restore_report=%s\n' "$BACKUP_REPORT"
  printf 'note=自动回滚已完成；恢复等待需人工重装 shadow drop-in 并 enable timer\n'
} >"$FAIL_MARKER" 2>/dev/null || true
# e) 失败细节（脱敏：cutover.sh 本身不打印连接串/token）
log "FAIL details（cutover 输出尾部）："
tail -20 "$CUT_OUT" 2>/dev/null | sed 's/^/  | /' >>"$WATCH_LOG"
rm -f "$CUT_OUT" 2>/dev/null || true
log "rollback: 完成；旧 worker + 旧报告已恢复，watcher timer 已停用（需人工介入）"
exit 1
