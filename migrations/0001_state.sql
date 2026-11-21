-- 0001_state.sql — hot_meme_search 状态层初始 schema（Phase A）
-- 迁移器：hotmeme_migrator；应用运行角色：hotmeme_worker / hotmeme_admin（都不是超级用户）。
-- 约定：
--   * 所有时间用 TIMESTAMPTZ（存储 UTC，显示时区由数据库/会话决定）。
--   * 复杂结构用 JSONB：round.stats / pool1.aliases_json / pool1.evidence_json。
--   * 本文件可重复执行（IF NOT EXISTS / ON CONFLICT），版本记录见 schema_version。
-- 保留期（见 §3.7；只做约定与清理 helper 占位，Phase A 不建定时任务）：
--   round              90 天
--   family_round_stat  30 天
--   audit_event        90 天
--   pool_transition    90 天
--   video_metric        7 天  ← 每轮新增最多 300 行，先在 pg_cron / systemd timer 之前手工清理

-- ---------------------------------------------------------------- 版本表
CREATE TABLE IF NOT EXISTS schema_version (
  version     INT PRIMARY KEY,
  description TEXT,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 4.1 round
CREATE TABLE IF NOT EXISTS round (
  id               BIGSERIAL PRIMARY KEY,
  started_at       TIMESTAMPTZ NOT NULL,
  finished_at      TIMESTAMPTZ,
  window_start     TIMESTAMPTZ NOT NULL,
  window_end       TIMESTAMPTZ NOT NULL,
  status           TEXT NOT NULL,              -- running / ok / degraded / failed
  videos_sampled   INT DEFAULT 0,
  comments_sampled INT DEFAULT 0,
  danmaku_sampled  INT DEFAULT 0,
  degraded_reason  TEXT,
  stats            JSONB DEFAULT '{}',
  created_at       TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE round IS '每轮采集/计算记录；保留 90 天';
COMMENT ON COLUMN round.status IS 'running / ok / degraded / failed';
COMMENT ON COLUMN round.stats IS '本轮统计快照 JSONB（候选数、配额、耗时等扩展字段）';

-- ---------------------------------------------------------------- 4.2 video_metric
CREATE TABLE IF NOT EXISTS video_metric (
  round_id        BIGINT NOT NULL REFERENCES round(id),
  bvid            TEXT NOT NULL,
  cid             BIGINT,
  title           TEXT,
  zone            TEXT,
  pubdate         BIGINT,
  source          TEXT,
  view            BIGINT,
  reply           BIGINT,
  danmaku         BIGINT,
  online          INT,
  new_view        BIGINT,
  new_reply       BIGINT,
  new_danmaku     BIGINT,
  play_proxy      REAL,
  hot_score       REAL,
  selected_comment BOOLEAN DEFAULT false,
  selected_danmaku BOOLEAN DEFAULT false,
  error           TEXT,
  PRIMARY KEY (round_id, bvid)
);

CREATE INDEX IF NOT EXISTS idx_video_metric_bvid_round
  ON video_metric (bvid, round_id DESC);
CREATE INDEX IF NOT EXISTS idx_video_metric_round_hot
  ON video_metric (round_id, hot_score DESC);

COMMENT ON TABLE video_metric IS '每轮候选视频指标快照；保留 7 天（本轮最多约 300 行/轮，约 28.8k 行/天）';

-- ---------------------------------------------------------------- 4.3 pool1
CREATE TABLE IF NOT EXISTS pool1 (
  family_key      TEXT PRIMARY KEY,
  canonical       TEXT NOT NULL,
  aliases_json    JSONB DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'active',   -- active / out
  score           REAL NOT NULL DEFAULT 0,
  count_prev      INT DEFAULT 0,
  count_now       INT DEFAULT 0,
  zero_streak     INT DEFAULT 0,
  audit_state     TEXT DEFAULT 'none',
  audit_hash      TEXT,
  first_seen_round BIGINT,
  last_seen_round  BIGINT,
  evidence_json   JSONB DEFAULT '[]',
  updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pool1_status_score ON pool1 (status, score DESC);
CREATE INDEX IF NOT EXISTS idx_pool1_last_seen_round ON pool1 (last_seen_round);

COMMENT ON TABLE pool1 IS '候选/可信池（单位=family），长期保留（无清理）；status: active / out';
COMMENT ON COLUMN pool1.aliases_json IS '归并别名列表 JSONB；人工别名另见 family_alias';
COMMENT ON COLUMN pool1.evidence_json IS '最新证据快照 JSONB（展示/审核用，不进比例计数）';

-- ---------------------------------------------------------------- 4.4 pool2
CREATE TABLE IF NOT EXISTS pool2 (
  family_key  TEXT PRIMARY KEY REFERENCES pool1(family_key) ON DELETE CASCADE,
  round_id    BIGINT REFERENCES round(id),
  heat_score  REAL,
  rank        INT,
  promoted_at TIMESTAMPTZ DEFAULT now(),
  reason      TEXT
);

COMMENT ON TABLE pool2 IS '发布/热度池；pool2 ⊆ pool1，pool1 撤出时同步删除 pool2';

-- ---------------------------------------------------------------- 4.5 family_round_stat
CREATE TABLE IF NOT EXISTS family_round_stat (
  id            BIGSERIAL PRIMARY KEY,
  round_id      BIGINT NOT NULL REFERENCES round(id),
  family_key    TEXT NOT NULL REFERENCES pool1(family_key),
  comment_count INT DEFAULT 0,
  danmaku_count INT DEFAULT 0,
  video_count   INT DEFAULT 0,
  meta_count    INT DEFAULT 0,
  count_total   INT DEFAULT 0,
  ratio         REAL,
  score_before  REAL,
  score_after   REAL,
  delta         REAL,
  reason        TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (round_id, family_key)
);

COMMENT ON TABLE family_round_stat IS '每轮 family 计数/比例/分数变化；保留 30 天';

-- ---------------------------------------------------------------- 4.6 audit_event
CREATE TABLE IF NOT EXISTS audit_event (
  id            BIGSERIAL PRIMARY KEY,
  family_key    TEXT REFERENCES pool1(family_key),
  round_id      BIGINT REFERENCES round(id),
  kind          TEXT NOT NULL,            -- llm / human
  verdict       TEXT NOT NULL,            -- pass / fail / unclear / merge
  delta         REAL DEFAULT 0,
  confidence    REAL,
  reason        TEXT,
  operator      TEXT,
  model         TEXT,
  prompt_version TEXT,
  evidence_hash TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (family_key, evidence_hash, kind)
);

COMMENT ON TABLE audit_event IS '审核事件（llm/human）；加减分按「同一 family+evidence_hash 最新结论替换」读取；保留 90 天';

-- ---------------------------------------------------------------- 4.7 family_alias
CREATE TABLE IF NOT EXISTS family_alias (
  id         BIGSERIAL PRIMARY KEY,
  family_key TEXT REFERENCES pool1(family_key) ON DELETE CASCADE,
  alias      TEXT NOT NULL,
  norm_alias TEXT NOT NULL,
  source     TEXT,                       -- seed / manual / auto
  confidence REAL,
  UNIQUE (norm_alias)
);

COMMENT ON TABLE family_alias IS 'family 别名（种子/人工/自动归并结果）；长期保留';

-- ---------------------------------------------------------------- 4.8 pool_transition
CREATE TABLE IF NOT EXISTS pool_transition (
  id         BIGSERIAL PRIMARY KEY,
  family_key TEXT REFERENCES pool1(family_key),
  round_id   BIGINT REFERENCES round(id),
  from_pool  TEXT,
  to_pool    TEXT,
  reason     TEXT,
  score      REAL,
  created_at TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE pool_transition IS '池迁移审计日志（pool1/pool2 进出）；保留 90 天';

-- ---------------------------------------------------------------- 清理 helper 占位（Phase A 不建定时任务）
-- 7 天 video_metric 清理（每日一次，由后续 systemd timer / pg_cron 调用）：
--   DELETE FROM video_metric WHERE round_id IN (
--     SELECT id FROM round WHERE started_at < now() - interval '7 days'
--   );
-- 其余表按上文 COMMENT 中的保留期清理。清理前先做 pg_dump -Fc（见 §3.7.1）。

-- ---------------------------------------------------------------- 权限
-- 角色可能尚未创建（例如本地测试库），因此逐个检查后再 GRANT；迁移器保留所有者权限。
DO $grants$
DECLARE
  r TEXT;
BEGIN
  FOREACH r IN ARRAY ARRAY['hotmeme_worker', 'hotmeme_admin'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r);
      EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
        'round, video_metric, pool1, pool2, family_round_stat, '
        'audit_event, family_alias, pool_transition TO %I', r);
      EXECUTE format('GRANT SELECT ON TABLE schema_version TO %I', r);
      EXECUTE format(
        'GRANT USAGE, SELECT ON SEQUENCE '
        'round_id_seq, family_round_stat_id_seq, audit_event_id_seq, '
        'family_alias_id_seq, pool_transition_id_seq TO %I', r);
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hotmeme_migrator') THEN
    EXECUTE 'GRANT USAGE, CREATE ON SCHEMA public TO hotmeme_migrator';
  END IF;
END
$grants$;
