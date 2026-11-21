-- 0002_pool_flags.sql — pool1/pool2 展示与审核计分字段（Phase A schema 修正）
--
-- 背景（PM 复核）：已确认决策需要以下字段，0001 建表时遗漏：
--   * pool1.meta_only / explainer_only：元话语-only 与 explainer-only 例外标记（§3.4）；
--   * pool1.audit_adjustment：最新决定性审核结论的分数变化（替换而非累加，§3.8）；
--   * pool1.status 语义改为 normal / provisional / out，默认 normal（原默认 active）；
--   * pool2.explainer_only：展示用，与 pool1.explainer_only 同步（应用层负责同步）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS / ALTER ... SET DEFAULT / UPDATE ... WHERE 可重复执行；
-- 迁移器同一事务内执行，schema_version 记录 version=2。

ALTER TABLE pool1 ADD COLUMN IF NOT EXISTS meta_only BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE pool1 ADD COLUMN IF NOT EXISTS explainer_only BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE pool1 ADD COLUMN IF NOT EXISTS audit_adjustment REAL NOT NULL DEFAULT 0;

-- status 语义：normal / provisional / out；默认 normal（历史值 active → normal）
ALTER TABLE pool1 ALTER COLUMN status SET DEFAULT 'normal';
UPDATE pool1 SET status = 'normal' WHERE status = 'active';

ALTER TABLE pool2 ADD COLUMN IF NOT EXISTS explainer_only BOOLEAN NOT NULL DEFAULT false;

COMMENT ON TABLE pool1 IS '候选/可信池（单位=family），长期保留（无清理）；status: normal / provisional / out';
COMMENT ON COLUMN pool1.status IS 'normal / provisional / out（默认 normal）；provisional 为元话语-only 初始态（§3.4）';
COMMENT ON COLUMN pool1.meta_only IS '元话语-only 标记：true 时以 provisional 进池1；前 2 轮零计数不重罚（§3.4）';
COMMENT ON COLUMN pool1.explainer_only IS 'explainer-only 例外标记：强元话语+种子/人工确认可无使用进池2，每轮≤2 条（§3.4）';
COMMENT ON COLUMN pool1.audit_adjustment IS '最新决定性审核结论的分数变化（替换而非累加；人工覆盖 LLM，§3.8）';
COMMENT ON COLUMN pool2.explainer_only IS '展示用：与 pool1.explainer_only 同步（应用层写入）';
