# P0 评论区优先 · 实现与验收报告

会话：comment-core（`fa555edf-8208-4db3-9f34-e54ad52c93ca`）
日期：2026-11-14
范围：`app/bili.py`、`app/pipeline.py`、`app/store.py`、`app/config.py`、`tests/*`、`docs/02`；产品代码未接 LLM、未装依赖、未 commit。

## 1. 改动清单

| 文件 | 改动 |
|---|---|
| `app/bili.py` | `comments()` 切 `/x/v2/reply/main`（`mode=3|2`、`next` 游标、`ps=20`）；`parse_comments` 解析 `replies` + 可选 `top_replies`（按 `rpid` 去重），保留 `ctime/like/rpid`；`parse_comment_page` 返回 `next`（`is_end` 时归 0）；`comment_page` 供多页采集；失败记录 `client.errors` 并返回空；离线兼容旧 `reply.json` 结构。 |
| `app/store.py` | `comment_cache` 主键改 `(aid, mode, page)`，启动时幂等迁移（旧行补 `mode=3`）；`get/put_comment_cache` 带 mode，缓存内容强制含 `ctime`；`insert_text_samples` 的 `raw_time` 改取 `ctime`（评论），弹幕回退 `at`。 |
| `app/pipeline.py` | 评论双 mode 采集 + `rpid`/文本去重 + 30 天窗口过滤 + 每视频 ≤40 条（热度按赞、时间按 ctime 各取一半再补齐）；评论 `ctime/mode/rpid` 以动态属性挂到 `TextDoc`（不改 `extract.TextDoc`）；新增 `comment_evidence()` / `comment_gate()`；`stats` 新增评论指标；弹幕降配。 |
| `app/config.py` | 新增 `COMMENT_MODES=(3,2)`、`COMMENT_PAGES=1`、`COMMENT_PAGE_SIZE=20`、`COMMENT_WINDOW_DAYS=30`、`COMMENT_RECENT_DAYS=7`、`COMMENT_MAX_PER_VIDEO=40`、`COMMENT_GATE_MIN_DOCS=200`、`COMMENT_LIKE_HALF_LIFE_DAYS=14`；`DANMAKU_VIDEOS 50→10`、`MAX_DANMAKU_PER_VIDEO 400→200`、`SEARCH_DANMAKU_VIDEOS 25→2`。 |
| `app/http.py` | 仅 1 行：`_endpoint_label` 把 `/x/v2/reply/main` 归入 `comments`（该文件不在任务所有权清单内，属观测所需的显式小改，已在此披露）。 |
| `tests/*` | 新增 `tests/test_p0_comment.py`（13 个用例）、fixture `reply_main.json` / `reply_main_mode2.json`；`test_wave2b.py` 的 FakeClient 适配 `mode`。 |
| `docs/02` | 接口表、§2.1 采集配额与窗口、§4.1 评论证据与门控、§7.1 缓存/统计口径。 |

## 2. 关键发现：`buvid3` 让评论接口只回 3 条

同一 `oid/mode/next/ps` 实测（2026-11-14，匿名）：

| 请求条件 | `reply/main` 返回 |
|---|---|
| 带 `buvid3`（现有 `bootstrap()` 默认会设） | **3 条** |
| 只带 `buvid4` | 20 条 |
| 清空 cookie | 20 条 |
| 清掉后重新加 `buvid3` | 又变回 3 条 |

结论：旧的「一轮 90 条评论」不只是 `/x/v2/reply` 的问题，`buvid3` 本身就会把匿名评论页压到 3 条。修复：`BiliClient._without_buvid3()` 只在评论请求期间摘掉 `buvid3`（`buvid4` 保留），请求后原样放回；其他接口行为不变。修复前后同一口径：87 条 → 237 条（+172%）。

## 3. 采集口径与配额

| 项 | 取值 |
|---|---|
| 评论视频 | 30（分区榜按 reply + 10 个梗搜索视频） |
| 每视频评论 | `mode=3` + `mode=2` 各 1 页（60 请求/轮），合并去重后 ≤40 条 |
| 窗口 | `ctime` ≤30 天（增速另算 ≤7 天） |
| 去重 | 先 `rpid` 后归一化文本 |
| 弹幕 | 10 视频，每视频 ≤200 条（1704 条/轮），仅旁证 |
| 缓存 | 30 分钟，键 `(aid,mode,page)`；命中计 `comment_cached` 且不请求 |

## 4. P0 门控（选择与说明）

选择 **`rejected`**（比 `observing` 更能真实影响候选池），但加健康闸门：

- 仅当本轮 `comments_recent ≥ COMMENT_GATE_MIN_DOCS=200` 时启用；评论采集不健康时整体跳过，避免单源失败清空候选池；
- 非种子候选且 `comment_count_recent == 0` → `rejected`，原因 `no_recent_comment`，写入 `stats.comment_gate_rejected`；
- 种子仍走原有 `passes_meme_gate` / `status_for` 规则。

## 5. pytest

```
50 passed → 63 passed（新增 13；1 warning 为既有 starlette 弃用告警）
```

覆盖：main 解析（`top_replies` 去重 / 旧 fixture）、`buvid3` 摘除与复原、请求 URL `mode/next/ps`、错误记录与 `-404` 业务空、`ctime` 落库、缓存 mode 隔离 + 旧库迁移幂等、窗口过滤/去重/≤40、7 天增速与年龄归一赞、`danmaku_only`、门控四分支、配额（弹幕 10 / 评论 30）、离线端到端（冻结时钟 + 真实时钟两套）。

## 6. 离线跑通

`HOTMEME_OFFLINE=1` + 冻结时钟 `2025-04-10`（fixture 评论落在窗口内），库 `/tmp/p0_offline.db`：

| 指标 | 值 |
|---|---|
| comments / comments_recent / comments_7d | 432 / 432 / 351 |
| comment_modes | `{"3": 243, "2": 189}` |
| comment_dupes / comment_filtered_old | 27 / 27 |
| comment_requests / comment_cached | 54 / 0 |
| danmaku / danmaku_docs | 2000 / 2000 |
| comment_evidence_ready | true |
| text_sample(comment) raw_time>0 | 432 / 432 |
| candidates / families / elapsed | 222 / 216 / 0.2s |

真实时钟下旧 fixture 评论全部 >30 天 → `comments_recent=0`、`comment_evidence_ready=false`、门控自动跳过（有专门用例守护）。

## 7. 真机两轮（`/tmp/p0.db`，`MAX_RPS=2.0`，不起服务、不碰 8000、不写 `data/hotmeme.db`）

| 指标 | 第 1 轮 | 第 2 轮 |
|---|---|---|
| cycle / status | #1 done | #2 done |
| 耗时 | 51.4s | 56.4s |
| HTTP 请求 | 103 | 103 |
| endpoint_counts | spi 1 / ranking 8 / popular 1 / nav 1 / search 20 / pagelist 2 / danmaku 10 / comments 60 | 同左 |
| 评论保留 / ≤30 天 / ≤7 天 | 237 / 237 / 147 | 222 / 222 / 154 |
| comment_modes | `{"3": 34, "2": 203}` | `{"3": 29, "2": 193}` |
| 去重 / 窗口外丢弃 | 6 / 700 | 10 / 726 |
| 评论页请求 / 缓存命中 | 60 / 0 | 60 / 0 |
| 弹幕文档 | 1704 | 1704 |
| videos / candidates / families | 377 / 630 / 474 | 379 / 655 / 487 |
| 错误 | 0 | 0 |
| comment_evidence_ready / gate_checked / gate_rejected | true / — / 0 | true / 5 / 0 |
| meme_gate_rejected | — | 482 |

`text_sample.raw_time` 抽样（第 2 轮，222/222 非 0；min 1792053945 = 2026-10-14，max 1794644452 = 2026-11-14T16:20+08:00）：

| bvid | raw_time | 时间 | likes | 文本 |
|---|---|---|---|---|
| BV1kMWz6CESY | 1794644452 | 2026-11-14T16:20 | 0 | 星爷还是太权威了 |
| BV1kMWz6CESY | 1794644415 | 2026-11-14T16:20 | 0 | 黄：钟哥，我也可以谈，我也可以爱国[吃瓜] |
| BV1kMWz6CESY | 1794644379 | 2026-11-14T16:19 | 0 | 经典冰冷的上千亿[doge] |

弹幕未写 `text_sample`（沿用旧行为，只在内存里参与抽取），因此报告中的 `raw_time` 抽样针对评论；`danmaku_docs` 单独统计。

## 8. 新 Top10 vs 生产 cycle#7 Top10

生产基线（只读 `data/hotmeme.db` cycle#7）：

| # | canonical | score | seed_hit | origin | 生产评论证据（text_sample 命中，raw_time 恒 0） |
|---|---|---|---|---|---|
| 1 | 抽象 | 89.3 | 是 | 2023 离谱好笑 | 1 |
| 2 | 咕咕嘎嘎 | 81.1 | 是 | 2025 空耳 | 0 |
| 3 | 蚌埠住了 | 77.3 | 是 | 2021 绷不住 | 0 |
| 4 | 哈基米 | 72.6 | 是 | 2024 猫咪空耳 | 0 |
| 5 | 嘉豪 | 68.2 | 是 | 2025 校园梗 | 0 |
| 6 | 今夕是何年 | 65.3 | 是 | 2023 怀旧 | 0 |
| 7 | 你礼貌吗 | 64.6 | 是 | 2022 反问 | 0 |
| 8 | 麻了 | 59.7 | 是 | 2021 麻木 | 0 |
| 9 | 整活 | 55.5 | 是 | 2021 整蛊表演 | 0 |
| 10 | 吃瓜 | 53.3 | 是 | 2020 围观 | 0 |

P0 第 1 轮（237 评论）：

| # | canonical | score | status | seed | origin | c30 | c7 | likeW | df | growth | age | danmaku_only |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 抽象 | 92.8 | hot | 是 | 2023 离谱好笑 | 3 | 3 | 0.0 | 3 | 4.0 | 1.6 | False |
| 2 | 哈基米 | 81.3 | observing | 是 | 2024 猫咪空耳 | 0 | 0 | 0.0 | 0 | 1.0 | — | True |
| 3 | 整活 | 76.5 | observing | 是 | 2021 整蛊表演 | 1 | 0 | 0.0 | 1 | 0.5 | 12.1 | False |
| 4 | 咕咕嘎嘎 | 67.5 | observing | 是 | 2025 空耳 | 0 | 0 | 0.0 | 0 | 1.0 | — | True |
| 5 | 吃瓜 | 59.7 | observing | 是 | 2020 围观 | 4 | 3 | 14.94 | 3 | 2.0 | 9.2 | False |
| 6 | 离谱 | 59.3 | observing | 是 | 2021 离谱 | 2 | 0 | 0.0 | 2 | 0.333 | 22.7 | False |
| 7 | 嘉豪 | 49.8 | observing | 是 | 2025 校园梗 | 0 | 0 | 0.0 | 0 | 1.0 | — | False |
| 8 | 那咋了 | 19.1 | observing | 是 | 2024 反问怼人 | 1 | 1 | 0.0 | 1 | 2.0 | 1.3 | False |

P0 第 2 轮（222 评论，含门控计数）：

| # | canonical | score | status | seed | origin | c30 | c7 | likeW | df | growth | age | danmaku_only |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 抽象 | 91.4 | hot | 是 | 2023 离谱好笑 | 2 | 2 | 0.0 | 2 | 3.0 | 1.6 | False |
| 2 | 整活 | 80.9 | observing | 是 | 2021 整蛊表演 | 1 | 0 | 0.0 | 1 | 0.5 | 12.1 | False |
| 3 | 哈基米 | 80.8 | observing | 是 | 2024 猫咪空耳 | 0 | 0 | 0.0 | 0 | 1.0 | — | True |
| 4 | 离谱 | 64.1 | observing | 是 | 2021 离谱 | 2 | 0 | 0.0 | 2 | 0.333 | 22.7 | False |
| 5 | 吃瓜 | 54.0 | observing | 是 | 2020 围观 | 4 | 4 | 14.53 | 2 | 5.0 | 1.7 | False |

### 结论（如实）

- **Top10 仍是种子/老梗，没有出现评论驱动的新梗**：两轮 Top10 100% 是种子（origin 2020–2025），排名靠前的仍是「抽象 / 整活 / 哈基米 / 离谱 / 吃瓜」。
- **根因（按影响排序）**：
  1. 候选池在更早的**梗性门控**就被截断：487 个梗族里 482 个未过 `MEME_MIN=0.45`，只有 5 个进入评论门控，且 5 个全是种子 → P0 评论门控 `gate_rejected=0`，本轮没有可拒绝的非种子输入。这不是评论门控失效，是上游没有非种子候选。
  2. 评论证据目前只进 `components.raw`（PM 明确 P0 不改 judge/score），因此**不参与排序**，只做门控；在「全是种子」的输入上自然不改变名次。
  3. 评论语料规模仍偏小：222 条 vs 弹幕 1704 条（13.0%）；60 页拉回 958 条，726 条因 >30 天被窗口丢弃（76%），近 30 天证据主要来自 `mode=2`（193 条，点赞几乎为 0 → 大多数 `comment_like_weighted=0`）。这与 PM 记录的「热度评论集中在 30–90 天前、时间序评论均赞 0.1–0.5」一致。
  4. `/tmp/p0.db` 是全新库：无历史分数与 `first_seen`，全部 `is_new=true`，没有 EMA 平滑与「回锅」路径，种子凭曝光/互动直接占榜。
- **未达 §13.4 的「评论 ≥800 条/轮」**：实测 237/222。原因是匿名接口单页实际上限（`mode=3` 20 条、`mode=2` 仅 3–4 条且常 `is_end`）+ 30 天窗口丢弃 76%，不是配额没跑满（评论请求 60/轮已按 30 视频 ×2 mode ×1 页打满，总请求 103，仍 ≤110 预算）。要冲 800 需要放开请求预算并实现 `mode=2` 的 `pagination_str.next_offset` 多页（见 §9），本轮未做。
- **`danmaku_only` 已生效**：哈基米、咕咕嘎嘎在 P0 两轮都被标为 `danmaku_only=True`（弹幕有命中、评论 ≤7 天与视频覆盖都为 0），可作为「只进经典榜」的候选，但 P0 未据此改榜（避免硬删弹幕/硬调阈值）。

## 9. 建议的下一步（需 PM 决策，本轮未做）

1. **评论翻页**：`mode=2` 用 `cursor.pagination_reply.next_offset` 走 `pagination_str` 多页；请求预算 110 → ~150（每视频 2 页时间序），预计评论 222 → 500+。
2. **评论证据参与打分**（P1）：把 `comment_7d_count` / `comment_growth_7d` / `comment_like_weighted` 作为 `velocity`/`engagement` 的组成或单独 5–10% 权重；否则评论只能门控、不能改榜。
3. **非种子候选召回**：现在 482/487 被梗性门控拒绝，评论证据再准也没有输入；需要评估 `MEME_MIN` 或给「评论近 7 天高频」的候选一条旁路（P1 再动阈值）。
4. 老梗识别：把 `danmaku_only` + `age_days>90` 落到「经典榜」而不是主榜；主榜只留 ≤30 天评论证据的族。

## 10. 产物

- `/tmp/p0.db`（两轮真机数据，第 2 轮保留）、`/tmp/p0_round1.json`、`/tmp/p0_round2.json`、`/tmp/p0_offline.json`、`/tmp/p0_run.py`
- 报告：`docs/pm/P0-COMMENT-REPORT.md`；口径：`docs/02-capture-and-scoring.md`
