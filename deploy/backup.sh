#!/usr/bin/env bash
# Hot Meme Postgres 每日备份（pg_dump -Fc，保留 7 天）。
#
# 用法：
#   deploy/backup.sh                 # 手工备份一次（systemd hotmeme-backup.service 也走这条）
#   HOTMEME_BACKUP_DIR=/tmp/b deploy/backup.sh
#
# 环境：
#   BACKUP_DATABASE_URL     备份连接串（优先；缺省回退 MIGRATE_DATABASE_URL、DATABASE_URL）
#   HOTMEME_BACKUP_DIR      输出目录（默认 /home/ubuntu/backups）
#   HOTMEME_BACKUP_KEEP_DAYS 保留天数（默认 7）
#   HOTMEME_BACKUP_LOG      日志文件（默认 $HOTMEME_BACKUP_DIR/backup.log）
#
# 输出：<BACKUP_DIR>/hotmeme-YYYYMMDD.dump（先写 .tmp 再 mv，失败不留半成品）
# 失败：退出码非 0（systemd 标记 failed；旧 dump 不受影响）；不打印连接串。
set -euo pipefail

BACKUP_DIR="${HOTMEME_BACKUP_DIR:-/home/ubuntu/backups}"
KEEP_DAYS="${HOTMEME_BACKUP_KEEP_DAYS:-7}"
LOG="${HOTMEME_BACKUP_LOG:-$BACKUP_DIR/backup.log}"
DSN="${BACKUP_DATABASE_URL:-${MIGRATE_DATABASE_URL:-${DATABASE_URL:-}}}"

mkdir -p "$BACKUP_DIR"
touch "$LOG" 2>/dev/null || true
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$LOG" >&2; }

if [ -z "$DSN" ]; then
  log "ERROR: 未配置 BACKUP_DATABASE_URL / MIGRATE_DATABASE_URL / DATABASE_URL"
  exit 2
fi

STAMP="$(date +%Y%m%d)"
OUT="$BACKUP_DIR/hotmeme-${STAMP}.dump"
TMP="$OUT.tmp-$$"
PG_DUMP="${PG_DUMP:-/usr/bin/pg_dump}"

cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

log "backup begin target=$OUT keep_days=$KEEP_DAYS"
started=$(date +%s)
if ! "$PG_DUMP" -Fc --no-owner --no-privileges --dbname="$DSN" -f "$TMP" >>"$LOG" 2>&1; then
  log "ERROR: pg_dump 失败（目标 $OUT 未生成）"
  exit 1
fi
mv -f "$TMP" "$OUT"
chmod 600 "$OUT"
log "backup ok $OUT size=$(du -h "$OUT" | cut -f1) elapsed=$(( $(date +%s) - started ))s"

# 保留 7 天：删除 mtime 超过 KEEP_DAYS 天的 hotmeme-*.dump
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'hotmeme-*.dump' -mtime +"$KEEP_DAYS" -print -delete \
  >>"$LOG" 2>&1 || true
count=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'hotmeme-*.dump' | wc -l)
log "backup retention done keep_days=$KEEP_DAYS dumps=$count"
