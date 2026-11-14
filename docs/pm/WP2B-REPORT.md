# WP2B 工程修复报告（限速/curl、cid、评论缓存、采集单飞、health）

- 会话：app-core-eng（`6994f43d-59df-4b55-a725-4339dab3d316`）
- 日期：2026-11-14；基线：`f4629e5` / `0bb3d3a`（PM 检查点）+ 本轮改动
- 所有权内改动：`app/http.py`、`app/pipeline.py`、`app/store.py`、`app/api.py`、`app/config.py`、`tests/*`、`README.md`、`docs/02-capture-and-scoring.md`
- **未改**：`app/static/index.html`、`app/judge.py`、`app/score.py`、`app/extract.py`、`app/bili.py`（URL 分类在 `http.py` 内完成）、`docs/01`、`data/*`；未启动/重启任何服务，未写 `data/hotmeme.db`，未安装依赖，未 commit。

## 0. 结论

| 验收项 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q` | **50 passed**（改动前 34 passed，新增 16 条覆盖任务 1–5） |
| 离线流水线两轮 | `done/done`，第二轮 `comment_requests=0 / comment_cached=27 / pagelist_requests=0` |
| 真机第二轮（2 rps，无并发） | **31 请求 / 18.0s / 0 errors**，≤40 请求、≤30s，**达标** |
| 真机 warm 轮 pagelist / comments | **25 → 0 / 30 → 0**（评论缓存命中 30 页） |
| 残留 running | 启动清理 + `BaseException` 收尾均有测试；真机库 `running` 行 = 0 |

## 1. 改了什么

### 1.1 限速口径与 curl 兜底（任务 1）

- `config.MAX_RPS` 默认 `3 → 2.0`，与 `docs/02 §7` 合规线一致；README 环境变量表同步。
- `RateLimiter`（`app/http.py:55`）保持**单线程 start-to-start 语义**，改为锁内槽位预约 + 锁外 sleep（并发不突发，也不退化为串行）：
  - 请求耗时 < 间隔：下一次等到「上次开始 + 1/rps」；
  - 请求耗时 > 间隔：网络往返已超间隔，**不额外 sleep（正常，不是 bug）**。
- 新增 `_count_request`（`http.py:116`）用 `threading.Lock` 保护 `requests += 1`，同时按 URL 归类到 `endpoint_counts`（`http.py:78`）。
- `_curl_get`（`http.py:195`）改为**先 `wait()`、再计数、再 `subprocess.run`**；curl 兜底不再是「风控时越限连发且不计请求」。
- 新增测试：`test_rate_limiter_start_to_start_interval`、`test_curl_get_waits_before_request_and_counts`、`test_request_counter_is_thread_safe`、`test_default_max_rps_is_two`。
- **未按 ASYNC-DESIGN 「3 rps 从未生效」去改坏间距语义**：该结论只说明往返 452ms > 333ms，修复价值在 curl 与计数，不在重新定义限速。

### 1.2 cid 持久化（任务 2）

- `store.upsert_videos` 的 `ON CONFLICT`：`cid=excluded.cid` → **`cid=COALESCE(excluded.cid, cid)`**（`store.py:229`），搜索视频的 `cid=None` 不再反向覆盖。
- 新增 `store.cid_map`（`:236`）批量读取已持久化 cid、`store.update_video_cid`（`:249`）在 `collect_docs` 补到 cid 后回写（`pipeline.py:100-107`）。
- 验收：真机 warm 轮 `pagelist_requests` **25 → 0**；`SELECT zone,COUNT(*),COUNT(cid) ...` 中「梗搜索」`with_cid` 从 0 → 25，且第二轮不再补。
- 测试：`test_upsert_videos_preserves_cid_on_null`、`test_cid_reuse_skips_pagelist_after_backfill`、`test_update_video_cid_is_idempotent`。

### 1.3 评论缓存（任务 3）

- 新表 `comment_cache(aid, page, fetched_at, payload, PRIMARY KEY(aid,page))`（`store.py:75`，`CREATE TABLE IF NOT EXISTS` 幂等，兼容旧库）。
- `store.get_comment_cache/put_comment_cache`（`:318/:336`）按 `COMMENT_CACHE_SECONDS=1800` TTL；`HOTMEME_COMMENT_CACHE_SECONDS=0` 可关闭。
- `collect_docs` 评论循环先查缓存；**命中不请求但仍按当前 cycle 写 `text_sample`**，`stats.comments` 口径不变；`stats.comment_cached` 记录命中页数（`pipeline.py:136-149`）。
- 验收：真机 warm 轮 `comment_requests` **30 → 0**、`comment_cached=30`、`comments` 仍为 90。
- 测试：`test_comment_cache_hit_skips_requests`、`test_comment_cache_ttl_expired_refetches`、`test_comment_cache_disabled_refetches`。

### 1.4 采集单飞（任务 4）

- `api.py` 新增 `RUN_LOCK`（`asyncio.Lock`）+ 唯一入口 `_run_collection`（`:34`）；`_start_collection`（`:49`）**同步占位 `STATE["running"]=True` 后**再 `create_task`，检查与置位之间无 `await`，双跑窗口消除。
- scheduler（`:59`）与 `/api/refresh` 共用入口；手动刷新在跑时 scheduler 本轮 tick 跳过；并发/排队 refresh 返回 **409**。
- `lifespan` 启动时调用 `store.abort_running_cycles`（`store.py:175`）把残留 `status='running'` 标记为 `aborted`，并写 `source_error(source='lifecycle')`。
- `pipeline.run_cycle` 的 `except Exception` → **`except BaseException`**（`pipeline.py:436`），`KeyboardInterrupt` 也先 `finish_cycle('failed')` 再抛出；`start_cycle` 移入 `try` 并判空。
- 测试：`test_refresh_single_flight_second_call_409`（两次刷新只跑 1 次、第二次 409）、`test_scheduler_and_manual_refresh_mutually_exclusive`、`test_run_cycle_keyboard_interrupt_marks_failed`、`test_abort_running_cycles_marks_aborted`、`test_lifespan_aborts_stale_running_cycle`。

### 1.5 `/api/health` 的 scheduler（任务 5）

- 改为返回 `_SCHEDULER_TASK is not None and not _SCHEDULER_TASK.done()`（`api.py:138`），不再用 `not DISABLE_SCHEDULER` 谎报。`OFFLINE=1` 或 `DISABLE_SCHEDULER=1` 时返回 `scheduler=false`。
- 测试：`test_health_scheduler_false_when_offline`。

### 1.6 文档（任务 6）

- README：`HOTMEME_MAX_RPS` 改 `2.0`；补齐 `HOTMEME_DATA_DIR`、`HOTMEME_FIXTURES`、`HOTMEME_TIMEOUT`、`HOTMEME_UA`、`HOTMEME_COMMENT_CACHE_SECONDS`。
- docs/02：新增 §7.1「采集缓存与请求指标」，说明 `comment_cached`、`pagelist_requests`、`endpoint_counts` 与评论缓存口径；限速仍为 2 rps（本次代码已对齐，未改规范口径）。

## 2. 测试与离线验收

测试环境（`tests/conftest.py` 强制 + 单测 `tmp_path` 覆盖）：

```bash
HOTMEME_OFFLINE=1 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/hotmeme_pytest.db \
  .venv/bin/python -m pytest -q
# 50 passed, 1 warning in 2.29s
```

TestClient 用例（`test_health_scheduler_false_when_offline`、`test_lifespan_aborts_stale_running_cycle`）均在 `HOTMEME_DISABLE_SCHEDULER=1`、`HOTMEME_OFFLINE=1` 下运行，并把 `config.DB_PATH` 指向 pytest `tmp_path`（不触碰 `data/hotmeme.db`）。

离线两轮（`HOTMEME_DB=/tmp/wave2b-offline.db`）：

| 轮次 | requests | comment_requests | comment_cached | pagelist_requests | danmaku_cached |
|---|---|---|---|---|---|
| 1 | 0 | 27 | 0 | 4 | 3 |
| 2 | 0 | **0** | **27** | **0** | 35 |

## 3. 真机两轮（`/tmp/wave2b.db`，唯一临时库）

启动 env（未启动服务，进程内 `pipeline.run_cycle`）：

```bash
HOTMEME_OFFLINE=0 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/wave2b.db \
HOTMEME_MAX_RPS=2.0 .venv/bin/python   # pipeline.run_cycle() ×2
```

### 3.1 总览

| 轮次 | 状态 | wall / stats.elapsed | requests | errors | comments | danmaku | families | usable |
|---|---|---|---|---|---|---|---|---|
| cycle#1 冷 | done | 95.1s | 165 | 0 | 90 | 16446 | 1047 | 12 |
| cycle#2 热 | done | **18.0s** | **31** | 0 | 90 | 16446 | 1047 | 12 |

### 3.2 请求分解（`cycle.stats_json.endpoint_counts`）

| 端点 | cycle#1（冷） | cycle#2（热） | 说明 |
|---|---|---|---|
| ranking | 8 | 8 | 8 个分区榜 |
| popular | 1 | 1 | 全站热门 |
| search | 20 | 20 | 20 组关键词 |
| spi | 1 | 1 | 每轮新 buvid |
| nav | 1 | 1 | wbi 密钥 |
| pagelist | **25** | **0** | cid 复用生效 |
| comments | **59** | **0** | 见 §3.3 |
| danmaku | 50 | **0** | 弹幕缓存 24h |
| **合计** | **165** | **31** | 与 `stats.requests` 一致 |

`comment_cached`：cycle#1 = 0，cycle#2 = **30**（30 个目标各 1 页全部命中）；`comment_requests`：30 → **0**；`comments` 两轮均 90，`text_sample` 口径未变。

### 3.3 冷轮 comments=59 的根因（推断，有算术支撑）

`comment_requests=30` 是**评论页逻辑拉取数**；HTTP 层 `endpoint_counts.comments=59`，差额 29 恰等于「urllib 首次 412 + curl 兜底成功」的额外请求数（29×2 + 1×1 = 59）。本轮 `errors=0`，说明 29 次 curl 兜底全部成功。修复前这 29 个 curl 请求**既不限速也不计数**（旧口径冷轮 136 请求）；现在是「计数变真 + curl 被 2 rps 约束」，所以冷轮 136 → 165、65.6s → 95.1s 不是请求变多，而是把此前逃逸的请求纳入合规统计。冷轮不在「≤40 请求 / ≤30s」的验收范围内；热轮已达标。

## 4. 前后对比

| 指标 | 改动前（生产 cycle#2 / 基线） | 改动后（/tmp/wave2b.db cycle#2） |
|---|---|---|
| 默认 `MAX_RPS` | 3（且实际未生效） | **2.0**（start-to-start 生效） |
| warm 轮请求 | 86 | **31**（−64%） |
| warm 轮耗时 | 43.0s | **18.0s** |
| pagelist 请求 | 25 | **0** |
| 评论请求 | 30 | **0**（缓存命中 30 页） |
| 弹幕网络请求 | 0（已缓存） | 0（缓存 50） |
| curl 兜底 | 不限速、不计数 | **先限速 + 计数** |
| 并发 refresh | 可双跑 | 第二次 **409**，只跑 1 次 |
| 中断残留 running | 永久残留 | 启动标 `aborted`；`KeyboardInterrupt` 落 `failed` |
| health.scheduler | OFFLINE 下谎报 true | 反映实际任务状态 |
| 测试 | 34 passed | **50 passed** |

验收线：第二轮 **31 ≤ 40 请求、18.0s ≤ 30s、0 errors**；未通过调高 `MAX_RPS` 或关闭信号凑数。

## 5. 风险与遗留

1. **冷轮仍慢（95.1s）**：合规 2 rps 下，cold 轮 165 个 HTTP 请求（含 29 次评论 curl 兜底）是结构性成本；弹幕/评论缓存只能改善热轮。若要压缩冷轮，需要降请求数（例如限制评论目标）或另立并发方案，不在本轮范围。
2. **comments 端点的 412 面较大**（29/30 首次 urllib 被 412）：说明 curl 兜底是热路径而非异常路径。现在兜底被限速/计数，合规性正确，但冷轮时延受此放大。建议后续单独评估评论接口的 TLS/UA 策略。
3. **`comment_requests` 与 `endpoint_counts.comments` 不再相等**：前者是逻辑页数、后者是 HTTP 请求数（含 curl）。文档已写明；看板若用 `requests` 展示总数，口径为真实 HTTP 请求。
4. **单飞只在单进程内成立**：多 uvicorn worker 下 `_COLLECT_TASK`/`RUN_LOCK` 各自为政，需进程级锁或单 worker 部署；本轮未改部署方式（未启动服务）。
5. **health.scheduler 反映进程内 task**：若 scheduler 卡在 `to_thread` 的 `run_cycle` 中，任务仍 `not done()`，会显示 true——语义是「调度器任务存活」，不是「本轮完成」。
6. **旧库兼容**：仅新增 `comment_cache` 表（`IF NOT EXISTS`）与读写函数；无列变更，`init_db` 幂等迁移测试仍通过。
7. 真机临时库 `/tmp/wave2b.db` 保留供复核；`data/hotmeme.db` 未被本轮读写。

## 6. 证据命令

```bash
# 测试
.venv/bin/python -m pytest -q

# 真机两轮（已执行，最多 2 轮）
HOTMEME_OFFLINE=0 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/wave2b.db \
HOTMEME_MAX_RPS=2.0 .venv/bin/python - <<'PY'
from app import pipeline, store
conn = store.connect(); store.init_db(conn)
for _ in range(2): pipeline.run_cycle(conn=conn)
PY

# 复核 stats / cid / 残留
sqlite3 /tmp/wave2b.db "SELECT id,status,json_extract(stats_json,'$.requests'),json_extract(stats_json,'$.elapsed'),json_extract(stats_json,'$.comment_cached'),json_extract(stats_json,'$.endpoint_counts') FROM cycle ORDER BY id;"
sqlite3 /tmp/wave2b.db "SELECT zone,COUNT(*),COUNT(cid) FROM video GROUP BY zone ORDER BY COUNT(*) DESC LIMIT 3;"
sqlite3 /tmp/wave2b.db "SELECT COUNT(*) FROM cycle WHERE status='running';"  # 0
```
