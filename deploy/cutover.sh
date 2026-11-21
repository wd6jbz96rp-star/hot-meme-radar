#!/usr/bin/env bash
# Phase G 切流脚本（影子 v2 → 生产）：**默认不自动执行**，先手工 --check-only 验证。
#
# 背景：冷启动期新 worker 以影子模式运行：
#   - systemd drop-in: /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
#     把 HOTMEME_MEME_INFO_REPORT 指向 /home/ubuntu/hotmeme/var/meme_info_v2_shadow.json
#   - 旧 hotmeme-worker 继续写生产 v1 报告 var/meme_info_latest.json
# 当影子报告出现 `status=ok` 且 pool1/pool2 非空时，本脚本执行切换：
#   1) 前置校验（v2 schema / status=ok / pool1 非空 / pool2 非空 / 公开 app 200）
#   2) 停旧 worker（disable --now，unit 保留可回滚）
#   3) **停新 state worker**：disable timer + stop service 并等待退出（防切流竞态：
#      timer 每 15 分钟可能正好在写影子报告，必须先冻结写入）
#   3.5) 停完后**再次校验冻结的影子报告**；若已不可用 → 安全中止（恢复旧 worker +
#        shadow timer，生产报告不动，退出码 3，watcher 会继续等待）
#   4) 删除影子 drop-in + daemon-reload（新 worker 恢复写生产报告路径）
#   5) 原子替换生产报告：shadow → var/meme_info_latest.json（同目录 tmp + mv）
#   6) 启用 state-worker timer 并手工触发一轮（写生产 v2）
#   7) 复核：报告 schema/status/pool、本机 API 200、公网 200
#
# 退出码：0 = 切流成功；3 = 安全中止（生产未改动，已恢复等待状态）；其它 = 失败
#        （由 cutover-watch.sh 负责自动回滚；手工执行时按部署 README 回滚）。
#
# 用法：
#   deploy/cutover.sh --check-only    # 只做校验，不改变任何状态（先跑这个）
#   deploy/cutover.sh                 # 全部校验通过后执行切流
#
# 回滚（切流后出问题，手工执行）：
#   sudo systemctl disable --now hotmeme-state-worker.timer
#   sudo systemctl stop hotmeme-state-worker.service
#   sudo mkdir -p /etc/systemd/system/hotmeme-state-worker.service.d
#   sudo cp /home/ubuntu/hotmeme/deploy/shadow-dropin.conf \
#           /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
#   sudo systemctl daemon-reload
#   sudo cp -a /home/ubuntu/hotmeme/var/meme_info_latest.json.before-phase-g \
#              /home/ubuntu/hotmeme/var/meme_info_latest.json
#   sudo systemctl enable --now hotmeme-worker
#   sudo systemctl restart hotmeme-app     # 可选；app 每次请求重读报告
#
# 不修改 Nginx / 其他服务；不打印任何密码或 token。
#
# 测试用环境变量（生产 unit 不需要设置）：HOTMEME_REPO_DIR / HOTMEME_PY /
#   HOTMEME_SHADOW_REPORT / HOTMEME_PROD_REPORT / HOTMEME_BACKUP_REPORT /
#   HOTMEME_DROPIN_DIR / HOTMEME_DROPIN_FILE / HOTMEME_APP_LOCAL / HOTMEME_PUBLIC_URL /
#   HOTMEME_SYSTEMCTL（默认 `sudo systemctl`）
set -euo pipefail

REPO_DIR="${HOTMEME_REPO_DIR:-/home/ubuntu/hotmeme}"
VAR_DIR="$REPO_DIR/var"
PY="${HOTMEME_PY:-${HOTMEME_PYTHON:-$REPO_DIR/.venv/bin/python}}"
SHADOW_REPORT="${HOTMEME_SHADOW_REPORT:-$VAR_DIR/meme_info_v2_shadow.json}"
PROD_REPORT="${HOTMEME_PROD_REPORT:-$VAR_DIR/meme_info_latest.json}"
BACKUP_REPORT="${HOTMEME_BACKUP_REPORT:-$VAR_DIR/meme_info_latest.json.before-phase-g}"
DROPIN_DIR="${HOTMEME_DROPIN_DIR:-/etc/systemd/system/hotmeme-state-worker.service.d}"
DROPIN_FILE="${HOTMEME_DROPIN_FILE:-$DROPIN_DIR/10-shadow.conf}"
APP_LOCAL="${HOTMEME_APP_LOCAL:-http://127.0.0.1:8090/api/meme-info?only=publishable}"
PUBLIC_URL="${HOTMEME_PUBLIC_URL:-http://124.220.189.74/hotmeme/}"
SYSTEMCTL="${HOTMEME_SYSTEMCTL:-sudo systemctl}"
WORKER_UNIT="${HOTMEME_WORKER_UNIT:-hotmeme-worker}"
STATE_TIMER_UNIT="${HOTMEME_STATE_TIMER_UNIT:-hotmeme-state-worker.timer}"
STATE_SERVICE_UNIT="${HOTMEME_STATE_SERVICE_UNIT:-hotmeme-state-worker.service}"

CHECK_ONLY=0
case "${1:-}" in
  --check-only) CHECK_ONLY=1 ;;
  -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
  "") ;;
  *) echo "未知参数：$1（支持 --check-only）" >&2; exit 2 ;;
esac

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

# 影子报告校验（v2 + ok + pool1/pool2 非空）；成功打印一行摘要，失败 stderr 原因
validate_shadow_report() {
  "$PY" - "$SHADOW_REPORT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
schema = str(report.get("schema_version") or "")
status = str(report.get("status") or "")
pool1 = report.get("pool1") or []
pool2 = report.get("pool2") or []
print(f"schema={schema} status={status} pool1={len(pool1)} pool2={len(pool2)}")
problems = []
if schema != "meme-info/v2":
    problems.append(f"schema_version={schema!r} 不是 meme-info/v2")
if status != "ok":
    problems.append(f"status={status!r} 不是 ok（degraded 轮不切流）")
if not pool1:
    problems.append("pool1 为空（Phase D 冻结/无 family）")
if not pool2:
    problems.append("pool2 为空（新梗榜将为空，不切流）")
if problems:
    print("FAIL: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
PY
}

# 停 state worker：先 disable timer，再 stop service 并等待退出；超时 SIGKILL。
# 返回 0 = 已确认停止；1 = 停不下来。
stop_state_worker() {
  "$SYSTEMCTL" disable --now "$STATE_TIMER_UNIT" >/dev/null 2>&1 || return 1
  if "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT"; then
    "$SYSTEMCTL" stop "$STATE_SERVICE_UNIT" >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do
      "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT" || return 0
      sleep 1
    done
    "$SYSTEMCTL" kill --signal=SIGKILL "$STATE_SERVICE_UNIT" >/dev/null 2>&1 || true
    sleep 1
    "$SYSTEMCTL" is-active --quiet "$STATE_SERVICE_UNIT" && return 1
  fi
  return 0
}

# 安全中止：生产报告未改动；恢复旧 worker + shadow timer（drop-in 仍在）后 exit 3。
safe_abort() {
  log "ABORT: $*（生产报告未改动；恢复旧 worker + shadow timer，继续等待下一轮）"
  "$SYSTEMCTL" enable --now "$WORKER_UNIT" >/dev/null 2>&1 \
    || log "WARN: 恢复 $WORKER_UNIT 失败（请手工 systemctl enable --now $WORKER_UNIT）"
  "$SYSTEMCTL" enable --now "$STATE_TIMER_UNIT" >/dev/null 2>&1 \
    || log "WARN: 恢复 $STATE_TIMER_UNIT 失败（shadow 停止，需人工检查）"
  exit 3
}

# ---------------------------------------------------------------- 1) 前置校验
log "== 1/7 前置校验（影子报告 + 公开 app）=="
[ -f "$SHADOW_REPORT" ] || fail "影子报告不存在：$SHADOW_REPORT（等待至少一轮影子运行）"
validate_shadow_report || fail "影子报告校验未通过（schema/status/pool 非空）"
local_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$APP_LOCAL" || true)
public_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PUBLIC_URL" || true)
[ "$local_code" = "200" ] || fail "本机 app API HTTP $local_code（期望 200）"
[ "$public_code" = "200" ] || fail "公网 app HTTP $public_code（期望 200）"
log "前置校验通过（app_local=$local_code public=$public_code）"

if [ "$CHECK_ONLY" = "1" ]; then
  log "--check-only：校验通过；未改变任何状态（收工）"
  exit 0
fi

# ---------------------------------------------------------------- 2) 停旧 worker
log "== 2/7 停旧 worker（unit 保留，可回滚）=="
"$SYSTEMCTL" disable --now "$WORKER_UNIT" || fail "停旧 worker 失败"
"$SYSTEMCTL" is-active --quiet "$WORKER_UNIT" && fail "旧 worker 未停止" || true
log "旧 worker 已停（disabled，unit/脚本保留）"

# ---------------------------------------------------------------- 3) 停新 state worker（防竞态）
log "== 3/7 停新 state worker（disable timer + stop service 并等待退出，防切流竞态）=="
if ! stop_state_worker; then
  safe_abort "无法停止 $STATE_SERVICE_UNIT / $STATE_TIMER_UNIT（拒绝在写入未冻结时切流）"
fi
log "state worker 已停（timer disabled，service inactive）"

# ---------------------------------------------------------------- 3.5) 冻结后复核影子报告
log "== 3.5/7 复核冻结后的影子报告 =="
if ! validate_shadow_report; then
  safe_abort "停 worker 后影子报告已不再是 ok+非空（可能刚被新一轮覆盖）"
fi

# ---------------------------------------------------------------- 4) 删除影子 drop-in
log "== 4/7 移除影子 drop-in（新 worker 恢复写生产报告路径）=="
rm -f "$DROPIN_FILE" 2>/dev/null || sudo rm -f "$DROPIN_FILE"
"$SYSTEMCTL" daemon-reload || fail "daemon-reload 失败"
log "drop-in 已移除：$DROPIN_FILE"

# ---------------------------------------------------------------- 5) 原子替换生产报告
log "== 5/7 影子报告原子替换生产报告 =="
TMP="$PROD_REPORT.tmp-cutover-$$"
cp -a "$SHADOW_REPORT" "$TMP" || fail "复制影子报告失败（生产报告未改动）"
if [ -f "$VAR_DIR/meme_info_v2_shadow.last_good.json" ]; then
  cp -a "$VAR_DIR/meme_info_v2_shadow.last_good.json" \
        "$VAR_DIR/meme_info_latest.last_good.json" || true
fi
mv -f "$TMP" "$PROD_REPORT" || fail "替换生产报告失败（旧报告仍在）"
chown ubuntu:ubuntu "$PROD_REPORT" 2>/dev/null || true
chmod 644 "$PROD_REPORT" 2>/dev/null || true
"$PY" - "$PROD_REPORT" <<'PY' || fail "生产报告替换后校验失败"
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"prod schema={report.get('schema_version')} status={report.get('status')} "
      f"pool1={len(report.get('pool1') or [])} pool2={len(report.get('pool2') or [])}")
assert report.get("schema_version") == "meme-info/v2", "生产报告 schema 不是 v2"
PY

# ---------------------------------------------------------------- 6) 启用 timer + 触发一轮
log "== 6/7 启用新 timer 并手工触发一轮（写生产 v2）=="
"$SYSTEMCTL" enable --now "$STATE_TIMER_UNIT" || fail "启用 $STATE_TIMER_UNIT 失败"
"$SYSTEMCTL" start "$STATE_SERVICE_UNIT" || log "WARN: 手工触发轮失败，见 journalctl -u $STATE_SERVICE_UNIT"
log "new timer: $("$SYSTEMCTL" list-timers "$STATE_TIMER_UNIT" --no-pager 2>/dev/null | sed -n '2p' | sed 's/  */ /g')"

# ---------------------------------------------------------------- 7) 复核
log "== 7/7 复核 =="
"$PY" - "$PROD_REPORT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"report schema={report.get('schema_version')} status={report.get('status')} "
      f"generated_at={report.get('generated_at')} counts={report.get('counts')}")
PY
local_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$APP_LOCAL" || true)
public_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PUBLIC_URL" || true)
log "复核完成：app_local=$local_code public=$public_code"
log "切流完成。回滚步骤见本脚本头部注释与 deploy/README.md §5。"
