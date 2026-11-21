#!/usr/bin/env bash
# Phase G 切流脚本（影子 v2 → 生产）：**默认不自动执行**，先手工 --check-only 验证。
#
# 背景：冷启动期（夜间 Phase C 采样量低于 degraded 阈值）新 worker 以影子模式运行：
#   - systemd drop-in: /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
#     把 HOTMEME_MEME_INFO_REPORT 指向 /home/ubuntu/hotmeme/var/meme_info_v2_shadow.json
#   - 旧 hotmeme-worker 继续写生产 v1 报告 var/meme_info_latest.json
# 当影子报告出现 `status=ok` 且 pool1/pool2 非空时，本脚本执行切换：
#   1) 前置校验（v2 schema / status=ok / pool1 非空 / pool2 非空 / 公开 app 200）
#   2) 停旧 worker（disable --now，unit 保留可回滚）
#   3) 删除影子 drop-in + daemon-reload（新 worker 恢复写生产报告路径）
#   4) 原子替换生产报告：shadow → var/meme_info_latest.json（先同目录 tmp 再 mv）
#   5) 确保 hotmeme-state-worker.timer 启用，手工触发一轮新 worker（写生产 v2）
#   6) 复核：报告 status/schema/pool 数、API 200、公网 200
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
set -euo pipefail

REPO_DIR="${HOTMEME_REPO_DIR:-/home/ubuntu/hotmeme}"
VAR_DIR="$REPO_DIR/var"
# 路径/URL 均可用环境变量覆盖，便于在服务器上用 /tmp 的假报告做 --check-only 正路径验证。
SHADOW_REPORT="${HOTMEME_SHADOW_REPORT:-$VAR_DIR/meme_info_v2_shadow.json}"
PROD_REPORT="${HOTMEME_PROD_REPORT:-$VAR_DIR/meme_info_latest.json}"
BACKUP_REPORT="$VAR_DIR/meme_info_latest.json.before-phase-g"
DROPIN_DIR="${HOTMEME_DROPIN_DIR:-/etc/systemd/system/hotmeme-state-worker.service.d}"
DROPIN_FILE="$DROPIN_DIR/10-shadow.conf"
APP_LOCAL="${HOTMEME_APP_LOCAL:-http://127.0.0.1:8090/api/meme-info?only=publishable}"
PUBLIC_URL="${HOTMEME_PUBLIC_URL:-http://124.220.189.74/hotmeme/}"
PY="$REPO_DIR/.venv/bin/python"

CHECK_ONLY=0
case "${1:-}" in
  --check-only) CHECK_ONLY=1 ;;
  -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
  "") ;;
  *) echo "未知参数：$1（支持 --check-only）" >&2; exit 2 ;;
esac

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

# ---------------------------------------------------------------- 1) 前置校验
log "== 1/6 前置校验（影子报告 + 公开 app）=="
[ -f "$SHADOW_REPORT" ] || fail "影子报告不存在：$SHADOW_REPORT（等待至少一轮影子运行）"
"$PY" - "$SHADOW_REPORT" <<'PY' || fail "影子报告校验未通过（schema/status/pool 非空）"
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
log "== 2/6 停旧 worker（unit 保留，可回滚）=="
sudo systemctl disable --now hotmeme-worker
systemctl is-active hotmeme-worker >/dev/null 2>&1 && fail "旧 worker 未停止" || true
log "旧 worker 已停（disabled，unit/脚本保留）"

# ---------------------------------------------------------------- 3) 删除影子 drop-in
log "== 3/6 移除影子 drop-in（新 worker 恢复写生产报告路径）=="
sudo rm -f "$DROPIN_FILE"
sudo systemctl daemon-reload
log "drop-in 已移除：$DROPIN_FILE"

# ---------------------------------------------------------------- 4) 原子替换生产报告
log "== 4/6 影子报告原子替换生产报告 =="
TMP="$PROD_REPORT.tmp-cutover-$$"
cp -a "$SHADOW_REPORT" "$TMP"
[ -f "$VAR_DIR/meme_info_v2_shadow.last_good.json" ] && \
  cp -a "$VAR_DIR/meme_info_v2_shadow.last_good.json" \
        "$VAR_DIR/meme_info_latest.last_good.json" || true
mv -f "$TMP" "$PROD_REPORT"
"$PY" - "$PROD_REPORT" <<'PY' || fail "生产报告替换后校验失败"
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"prod schema={report.get('schema_version')} status={report.get('status')} "
      f"pool1={len(report.get('pool1') or [])} pool2={len(report.get('pool2') or [])}")
assert report.get("schema_version") == "meme-info/v2", "生产报告 schema 不是 v2"
PY

# ---------------------------------------------------------------- 5) 启用 timer + 触发一轮
log "== 5/6 启用新 timer 并手工触发一轮（写生产 v2）=="
sudo systemctl enable --now hotmeme-state-worker.timer
sudo systemctl start hotmeme-state-worker.service || log "WARN: 手工触发轮失败，见 journalctl -u hotmeme-state-worker"
log "new timer: $(systemctl list-timers hotmeme-state-worker.timer --no-pager | sed -n '2p' | sed 's/  */ /g')"

# ---------------------------------------------------------------- 6) 复核
log "== 6/6 复核 =="
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
