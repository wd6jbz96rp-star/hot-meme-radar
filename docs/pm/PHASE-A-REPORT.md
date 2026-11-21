# Phase A 交付报告：Postgres 状态层

> 日期：2026-11-21  
> 状态：**完成**（未接入 worker/app，未切流量）

## 1. 交付内容

| 文件 | 作用 |
|---|---|
| `migrations/0001_state.sql` | 初始 schema：8 张业务表 + `schema_version` |
| `migrations/0002_pool_flags.sql` | 池字段修正：meta_only / explainer_only / audit_adjustment / status 语义 / pool2.explainer_only |
| `meme_info/state.py` | 连接、事务、迁移发现/执行、健康检查、保留期 helper 占位 |
| `tools/migrate_state.py` | CLI：`migrate` / `check` / `version` |
| `tests/test_state.py` | 单元 + Postgres 集成测试（无连接串时 skip） |
| `requirements.txt` | 新增 `psycopg[binary]>=3.2` |

## 2. 云上状态

- 服务器：`124.220.189.74`，Ubuntu 24.04
- PostgreSQL：**16.15**
- 监听：`127.0.0.1:5432`，无公网 5432
- 数据库：`hotmeme`，UTF8，owner `hotmeme_migrator`
- 角色（均非 superuser）：`hotmeme_migrator` / `hotmeme_worker` / `hotmeme_admin`
- 连接串：`/etc/hotmeme/env`（600 root），含 worker/admin/migrator 三个 DSN
- schema_version：**2**
- 表：`round`、`video_metric`、`pool1`、`pool2`、`family_round_stat`、`audit_event`、`family_alias`、`pool_transition`、`schema_version`

## 3. 权限与验证

- worker：8 张表 DML + 序列；`CREATE TABLE` 被拒
- admin：审核/池 DML；`CREATE TABLE` 被拒
- 迁移：空库首次迁移成功；重复迁移幂等；0002 重复执行幂等
- 测试：
  - 本地：**104 passed, 15 skipped**（skip 为无本地 Postgres 的集成测试）
  - 云上真实 Postgres：`tests/test_state.py` **32 passed**
- 测试行已清理，8 表残留为 0

## 4. 未做

- 未接 `meme_info` 业务逻辑 / worker / adapter
- 未改 systemd `EnvironmentFile`
- 未建保留期定时清理（Phase G）
- 未建每日 pg_dump 定时备份（Phase G；Phase A 数据库为空，风险低）

## 5. 环境备注

- 现有 Nginx 80、hotmeme-app 8090、hotmeme-worker、8000 FastAPI 均正常。
- Caddy :8080 自 **2026-11-17 19:24** 起 inactive/disabled，早于本次 Phase A，非本次改动；如需恢复另行处理。
- DB timezone 为 UTC；`TIMESTAMPTZ` 存储 UTC，展示时区由客户端决定。
