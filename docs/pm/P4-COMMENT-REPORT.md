# P4b · 45 评论视频 + 评论接口健壮性 · 实现与验收报告

> 会话：comment-robust（`5e23416e-fe35-46a4-a463-d8f027421b85`），委派方 lead。
> 项目根：`/Users/wanjunqiang/Desktop/hot_meme_search`，基线 HEAD `0a2b258`。
> 范围：`app/bili.py`、`app/pipeline.py`、`app/config.py`、`app/store.py`（未改）、`tests/*`、`docs/02-capture-and-scoring.md`、本文件。
> **未修改** `app/llm.py`、`app/review.py`、`app/llm_extract.py`、`app/judge.py`、`app/score.py`、`app/extract.py`、`static/*`、`data/*`、`docs/01`、`README`；未装依赖、未 commit、未起服务、未碰 8000、未写生产库。

## 0. 结论（TL;DR）

| 项 | 结果 |
|---|---|
| 评论视频 | 30 → **45**（分区配额 + 10 个梗搜索视频，不足按 `reply/view` 补齐；弹幕 **10** 视频 / 每视频 ≤200 条） |
| 冷轮（真机） | **111.4s / 223 请求 / status=done / 0 errors**；评论 **631**（≥600 ✅），≤7 天 **293** |
| warm 轮（真机） | **17.6s / 31 请求 / 0 评论请求**（180 页全部缓存命中，`comment_cached=180`） |
| 评论页失败 | 本次真机 **失败 0 / 重试 0**；失败分类、0.5/1/2s 退避、重试预算、只缓存有效页、失败率 >20% 降级均由新增测试覆盖 |
| 测试 | 全仓 **192 passed**（新增 `tests/test_p4_comment_robust.py` 13 例；P0 配额断言同步 45/10，P3 离线断言随 45 视频更新） |
| 是否达 600 | **达到**：631（30 天窗口）/ 293（7 天窗口），未改 30 天窗口 |

## 1. 实现

### 1.1 配额（`app/config.py`、`app/pipeline.py`）

| 配置 | 旧 | 新 |
|---|---|---|
| `COMMENT_VIDEOS` | 30 | **45** |
| `DANMAKU_VIDEOS` | 7 | **10** |
| `COMMENT_PAGES` | `{3:1, 2:3}` | 不变（每视频 4 页，45×4=180 评论请求） |
| `MAX_DANMAKU_PER_VIDEO` | 200 | 不变 |
| `COMMENT_WINDOW_DAYS` / `COMMENT_RECENT_DAYS` | 30 / 7 | **不变**（未放宽窗口硬凑） |

`select_targets` 保持「梗搜索视频优先 + 分区按 reply 取」的原逻辑，新增加固：分区配额只作下限，不足 45/10 时按 `(reply, view)` 从剩余视频补齐并去重。测试 `test_select_45_comment_videos_and_10_danmaku` 断言恰好 45 个唯一评论视频、10 个唯一弹幕视频。

### 1.2 评论页健壮性（`app/bili.py`）

`BiliClient.comment_page_ex(aid, page, mode, cursor, ps) -> (items, next_cursor, ok)`：

- 直接走 `http.get` 取原始字节（不改公共 `app/http.py`），因此能区分 **HTML 风控页 / 空响应 / JSON decode 失败**；`comment_page` 保持原 2 元组签名兼容旧调用方与测试 fake。
- 失败分类与重试：

| 类别 | 判定 | 重试 | bootstrap |
|---|---|---|---|
| `http_412` / `http_403` | `HttpError.status` | 是 | 首次重试前强制重取 buvid（每页 1 次） |
| `http_5xx` | HTTP 5xx | 是 | 否 |
| `html` / `empty` | 2xx 但 `body` 以 `<` 开头 / 空 | 是 | 是（每页 1 次） |
| `non_json` | 2xx 但 JSON decode 失败 | 是 | 否 |
| `code:-352` | 合法 JSON 风控 code | 是 | 是（每页 1 次） |
| `code=-404/12002/12061/12071` | 无评论/不可见 | **否** | 否；视为有效空页（可缓存、不记 error） |
| 其他 `code` / 网络错误 | 业务错误 / urllib 失败 | 否 | 否 |

- 退避：`0.5s → 1s → 2s`（指数），`COMMENT_MAX_ATTEMPTS=4`（首次 + 3 次重试）；单轮重试总预算 `COMMENT_RETRY_BUDGET=40`，风控风暴时超预算直接记失败，避免冷轮被退避拖爆。
- 失败页只记一次 error（含最终类别与尝试次数），统计 `comment_page_fail` / `comment_page_retry` / `comment_page_fail_kinds`；`ok=True` 的页才写 `comment_cache`，失败页不缓存、下一轮重试。

### 1.3 降级口径（`app/pipeline.py`）

- `collect_docs` 统计 `comment_requests`、`comment_page_fail`、`comment_page_retry`、`comment_page_fail_kinds`、`comment_page_fail_rate`、`comment_degraded`。
- 失败率 = `comment_page_fail / comment_requests`；**严格 >20%（`COMMENT_FAIL_RATE_MAX=0.20`）才 `comment_degraded=True`**。
- `run_cycle` 状态：评论 error 不再触发「有任何 error 就 degraded」；只有非评论 error、分区空、或 `comment_degraded` 才降级。单页失败 cycle 仍为 `done`。
- 评论全失败时 `comments=0 → comment_evidence_ready=false`（评论门控跳过），标题/弹幕/种子候选照常出榜；由 `test_comment_collection_total_failure_still_produces_board` 覆盖（status=degraded 且 `top` 非空）。

## 2. 真机验收

环境：仅 `/tmp/p4.db`（新建）、`HOTMEME_OFFLINE=0`、`HOTMEME_MAX_RPS=2.0`、`HOTMEME_DISABLE_SCHEDULER=1`、`HOTMEME_LLM_MODE=off`、不写生产库、不起服务。脚本 `/tmp/p4_run.py`，原始输出 `/tmp/p4_run.json`、`/tmp/p4_run.log`。

### 2.1 冷轮（cycle #1）

| 项 | 值 |
|---|---|
| 耗时 / 状态 | **111.4s** / `done` |
| HTTP 请求 | **223**（2 rps 下符合预算） |
| 请求分解 | comments **180**、ranking 8、popular 1、search 20、spi 1、nav 1、pagelist 2、danmaku 10 |
| 评论 / 30 天 / ≤7 天 | **631** / 631 / **293** |
| 评论 mode 分布 | mode3（热度）**54** / mode2（时间序）**577** |
| 窗口外丢弃 / 去重 | `comment_filtered_old=1930` / `comment_dupes=69` |
| 评论页成功/失败/重试 | 180 / **0** / **0**（失败类型 `{}`） |
| 弹幕 | 10 视频 / **1704** 条文档（每视频 ≤200） |
| 产出 | 377 视频 / 579 梗族 / Top 有榜、0 errors |
| 证据就绪 | `comment_evidence_ready=true` |

### 2.2 warm 轮（cycle #2，10 分钟 TTL 内）

| 项 | 值 |
|---|---|
| 耗时 / 状态 | **17.6s** / `done` |
| HTTP 请求 | **31**（spi 1、ranking 8、popular 1、nav 1、search 20；pagelist 0、danmaku 0） |
| 评论请求 / 缓存 | **0** / `comment_cached=180`（45×4 页全部有效并命中） |
| 评论 / ≤7 天 | 631 / 293（口径不变） |
| 评论页失败/重试 | 0 / 0 |
| 产出 | 与冷轮相同 Top；0 errors |

**warm 是否 0 评论请求：是。** 前提是冷轮缓存了 180 个有效页；失败页按设计不缓存，若有失败，warm 会只剩对应失败页的重试请求。

### 2.3 与 P1/P3 的请求/耗时对比（同一 2 rps 口径）

| 轮次 | 评论视频 | 请求 | 耗时 | 评论 | 失败页 |
|---|---|---|---|---|---|
| P1 验收（30 视频） | 30 | 160 | 87.9s | 408 | 0 |
| P3 冷轮（30 视频，风控） | 30 | 168 | 84.5s | 276 | 28 页非 JSON |
| **P4b 冷轮（45 视频）** | **45** | **223** | **111.4s** | **631** | **0** |
| **P4b warm（45 视频）** | 45 | **31** | **17.6s** | 631 | 0 |

冷轮相比 P1 增加 3 个弹幕视频 + 60 个评论页 = +63 请求 / +23.5s，换来 +223 条评论（408→631）；warm 与 P1 同为 31 请求（45 视频但 0 评论请求，耗时 17.6s vs 15.9s，差异是网络与抽取抖动）。

## 3. 600 达成情况与真实瓶颈（如实）

- **已达 631（30 天窗口）**，超过 600 目标 5%；45 视频下每视频平均 14.0 条（631/45），有评论的视频 41 个（4 个无窗口内评论）。
- 单视频分布：**max 40（上限）、中位数 12、最少 1**；说明不是「单视频拿不到」，而是 30 天窗口 + 每视频 ≤40 上限在起作用。
- 本轮拉回候选评论约 **2630 条**（631 + 1930 窗口外 + 69 去重），**1930 条（73%）因 >30 天被丢**；mode3 热度评论仅 54 条（5 天窗口内热评大多是旧评论），近 30 天证据主要仍靠 mode2 时间序（577 条）。
- 若未来需要更高量，优先按 `reply` 扩容到 50–60 视频（每 5 视频约 +70 条、+20 请求、+10s），**不要放宽 30 天窗口**；本次未调整窗口。

## 4. 失败/重试的验证方式

真机本轮 0 失败（未复现 P3 的 28 页风控），因此接口健壮性由 13 个新增用例覆盖：

| 用例 | 覆盖点 |
|---|---|
| `test_html_page_retries_then_succeeds` | HTML → 第 2 次成功；`retry=1`、退避 0.5s |
| `test_failure_kinds_are_distinguished_and_backoff_exponential` | `empty/html/non_json` 分类；4 次尝试、0.5/1/2s |
| `test_412_backoff_rebootstraps_once_before_retrying` | 412 退避；每页仅强制 bootstrap 一次；`http_412` 计数 |
| `test_code_minus_352_rebootstraps_and_retries` | `-352` 判为风控、重取 buvid 后重试成功 |
| `test_5xx_retries_but_business_errors_do_not` | 5xx 重试；`-500` 不重试；`-404` 视为有效空页、不记 error |
| `test_only_valid_pages_cached_and_failure_stats` | 失败页不写缓存、`fail_rate=0.25` 降级、评论文档仍产出 |
| `test_failure_rate_threshold_is_strictly_above_20_percent` | 20% 不降级、30% 降级（严格 >20%） |
| `test_warm_round_skips_cached_pages_but_retries_failed_ones` | warm 有效页 0 请求 |
| `test_single_comment_page_failure_does_not_degrade_cycle` | 单页失败 cycle 仍 `done`、榜单/评论正常 |
| `test_comment_collection_total_failure_still_produces_board` | 评论全失败 `degraded` 但 `top` 非空、`comment_evidence_ready=false` |
| `test_select_45_comment_videos_and_10_danmaku` | 45/10 目标选择与去重 |

全仓 `pytest -q`：**192 passed**（1 条 Starlette/httpx 弃用 warning，与本改动无关）。

## 5. 变更文件

| 文件 | 改动 |
|---|---|
| `app/config.py` | `COMMENT_VIDEOS=45`、`DANMAKU_VIDEOS=10`；新增 `COMMENT_RETRY_DELAYS=(0.5,1,2)`、`COMMENT_MAX_ATTEMPTS=4`、`COMMENT_RETRY_BUDGET=40`、`COMMENT_FAIL_RATE_MAX=0.20` |
| `app/bili.py` | `comment_page_ex`（失败分类/退避/重试预算/buvid 重取/计数）；`comment_page` 保持 2 元组兼容；`RETRYABLE_COMMENT_KINDS`、`REBOOTSTRAP_COMMENT_KINDS` |
| `app/pipeline.py` | `select_targets` 补齐 45/10；`_fetch_comment_page` 兼容旧 fake；只缓存 `ok` 页；失败率/重试/失败类型 stats；status 只按非评论 error 与 >20% 失败率降级 |
| `app/store.py` | 无需改动（缓存写入策略移到 pipeline；表结构/迁移沿用） |
| `tests/test_p0_comment.py` | 配额断言 30/7 → 45/10 |
| `tests/test_p3_llm_gate.py` | 45 视频离线语料更新后的 `all` 成员断言（行为隔离逻辑不变） |
| `tests/test_p4_comment_robust.py` | 新增 13 例（见 §4） |
| `docs/02-capture-and-scoring.md` | §2.1 配额/请求预算、§2.2 失败分类与重试/缓存/降级口径、§7 风险、§7.1 指标 |
| `docs/pm/P4-COMMENT-REPORT.md` | 本文件 |

## 6. 已知风险 / 后续

1. **风控风暴下的冷轮耗时**：单轮重试预算 40 次；若 180 页中有 >40 页持续 412，退避 + 重试会显著拉长冷轮（理论上限约 +60–90s）。本轮未复现，建议生产保持 10 分钟轮间隔。
2. **warm 非 0 评论请求**：失败页不缓存是刻意设计（避免把风控页当成功缓存）；有失败时 warm 会重试失败页，属预期。
3. **每视频 40 条上限**仍是达到更高评论量的主要限制；窗口 30 天保持不变。
4. 本次真机 0 失败，不能作为「风控已解决」的证据；失败路径由测试覆盖，真实风控下的恢复能力待后续压测（需冷却，不能靠降页数掩盖）。
