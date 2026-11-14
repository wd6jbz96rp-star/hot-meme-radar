# Shadow 同轮评估：app 旧 pipeline vs meme_info 模块（2026-11-15 00:15–00:23）

- 会话：shadow-eval
- 基线：HEAD `86dd9c7`（工作区只有既有未跟踪 `.archify/`，本会话未触碰）
- 目标：在**同一时段**各跑一轮「app 旧 pipeline」与「meme_info 模块」，做公平对比，判断是否值得切 `list=new`
- 网络约束：单进程 2 rps；两轮**串行**执行（app 平均 1.41 rps，模块 1.10 rps），无重试风暴
- 只写 `/tmp` 与本文件；**未**改代码/配置/数据、**未**写 `data/hotmeme.db`、**未**起/重启 8000、**未**跑 scheduler、**未** commit

> 结论先行：**继续 shadow，暂不切 `list=new`。** 两轮 Top10：app `0/10` 命中用户 16 锚点（10/10 是种子表老梗），模块 `7 ✅ / 0 ❌ / 3 待标`；两榜 overlap **0/10**。模块明显优于 app 当前可用的 new 榜，但单轮、3 条 Top10 未标、Top20 仍有 2 条用户 ❌，且 app 的 `list=new` 本身有结构性噪声问题，切换前必须先修+补标+多轮复验。

---

## 0. 时间与「同一时段」证据

| | app 旧 pipeline | meme_info 模块 |
|---|---|---|
| 运行窗口 | **00:15:46 – 00:19:01**（195.0s） | **00:19:17 – 00:22:45**（207.0s，采集始于 00:19:17） |
| 报告时间戳 | cycle 1 `started_at=00:15:46 / finished_at=00:19:01` | `generated_at=00:19:18`（`as_of` 为采集起点；实际采集/审核到 00:22:45） |
| 缓存 | 全新临时库：`comment_cached=0`、`danmaku_cached=0` | 全新缓存目录（启动前不存在）：`cache_hits=0`，LLM `calls=20`（全部真调用） |

- 两轮**首尾相接**：app 结束后 **16 秒**模块开始；总跨度 **00:15:46–00:22:45（7 分 00 秒）**。
- **不是同一秒并发**：app 快照冻结在 00:19:01，模块采集在 00:19:17–00:22:45。对分钟级热梗趋势公平，但不宣称「零时差快照」。
- 选择串行而非并发：并发会让两个进程合计 ~4 rps 超合规线；串行保证全程 ≤2 rps。
- **不受缓存影响**：两侧缓存都是冷的（app 临时库无历史缓存；模块新缓存目录 `cache_hits=0`）。00:01 的旧模块报告用的是 `/tmp/mi_cache`，与本轮 `/tmp/shadow-eval-cache` 完全隔离，不污染结果。
- B站限流/失败：app `errors=[]`、`comment_page_fail=0`；模块 `errors=[]`、`search_fail=0`、`budget_exhausted=false`，有 **30 条 comment 页 `http:None` 失败**（`endpoint_counts.comments=165`），仍采到 826 条评论，未重试风暴、未整轮降级。

### 0.1 复现命令（本会话实际执行）

```bash
# ① app 旧 pipeline → 临时库（不碰生产库、不启 8000）
cd /Users/wanjunqiang/Desktop/hot_meme_search
HOTMEME_DB=/tmp/shadow-app.db HOTMEME_MAX_RPS=2 HOTMEME_LLM_MODE=off HOTMEME_LLM_EXTRACT=0 \
  .venv/bin/python - <<'PY'
from app import pipeline, store, config
snap = pipeline.run_cycle()
...  # 落盘 /tmp/shadow-app-cycle.json：status/stats/top/list_new/list_classic
PY

# ② 模块 → /tmp/shadow-module.json（key 从 ~/.hotmeme_llm_key 读取，不打印；冷缓存）
HOTMEME_LLM_API_KEY_FILE=$HOME/.hotmeme_llm_key \
  .venv/bin/python -m tools.meme_info_shadow \
    --out /tmp/shadow-module.json --cache-dir /tmp/shadow-eval-cache \
    --limit 20 --verify --verify-gate
```

> **⚠️ 复现 bug（本轮发现）**：任务书里的 `.venv/bin/python tools/meme_info_shadow.py` **跑不起来**：
> `ModuleNotFoundError: No module named 'meme_info.cli'; 'meme_info' is not a package`。
> 原因：直接执行脚本时 `sys.path[0]=tools/`，`tools/meme_info.py` 遮蔽了 `meme_info/` 包。
> 必须用 **`-m tools.meme_info_shadow`**（与 `SHADOW-ADAPTER-REPORT.md §12` 一致）。建议后续把文档/调度命令统一为 `-m`（或重命名 `tools/meme_info.py`）。

---

## 1. app 旧 pipeline 结果（cycle 1，/tmp/shadow-app.db）

- status **`done`**（非 degraded）；耗时 **195.0s**；请求 **275**；`errors=[]`。
- 语料：视频 377 / 评论 592 / 弹幕 1704 / docs≈2296；families 603 → usable **105**。
- 冷启动代价：无评论/弹幕缓存，评论请求 180 次（生产库稳态 cycle 24 仅 32 请求/19.2s）。
- **注意**：临时库无历史，`is_new` 全部为 true、`hype_streak` 为空，只有 `抽象` 达到 hot（其余 observing）。这是冷库的显示/状态口径差异，排序主因仍是种子表 ∩ 热度。

### 1.1 Top10

| # | canonical | score | list | is_new | status | seed_hit/理由 |
|---:|---|---:|---|---|---|---|
| 1 | 抽象 | 88.5 | classic | true | hot | seed✓（2023 离谱好笑）；seed_hit .35 + quoted .10 + breadth .15 + meme_source .12 |
| 2 | 上头 | 78.4 | classic | true | observing | seed✓（2021 上瘾）；seed_hit .35 + breadth .15 |
| 3 | 整活 | 74.9 | classic | true | observing | seed✓（2021 整蛊表演）；seed_hit .35 + breadth .15 + meme_source .12 |
| 4 | 咕咕嘎嘎 | 63.6 | new | true | observing | seed✓（2025 空耳）；seed_hit .35 + breadth_mid .10 + danmaku_repeat .10 + meme_source .12 |
| 5 | 离谱 | 62.2 | classic | true | observing | seed✓（2021）；seed_hit .35 + quoted .10 + meme_source .12 |
| 6 | 吃瓜 | 53.9 | classic | true | observing | seed✓（2020 围观）；seed_hit .35 + quoted .10 + meme_source .12 + evergreen −.05 |
| 7 | 嘉豪 | 44.6 | new | true | observing | seed✓（2025 校园梗）；seed_hit .35 + quoted .10 + meme_source .12 |
| 8 | yyds | 43.3 | classic | true | observing | seed✓（2021 缩写）；seed_hit .35 + quoted .10 + meme_source .12 |
| 9 | 绝绝子 | 31.4 | classic | true | observing | seed✓（2021）；seed_hit .35 + quoted .10 + meme_source .12 + evergreen −.05 |
| 10 | 那咋了 | 16.9 | new | true | observing | seed✓（2024 反问）；seed_hit .35 + quoted .10 |

- **10/10 全部命中 `data/seed_memes.json`**（148 条种子，origin 2020–2025）；`list` 分布 classic 7 / new 3（new 仅因种子 origin ≥2024）。
- 用户 16 锚点命中：app Top10 **0/16**；app 全部 105 条 signals 里 **0/16**（16 条当前梗完全没有进入旧 pipeline 的可用池）。

### 1.2 ⚠️ app `list=new` 实测：被 rank=0 噪声词占满

`/tmp/shadow-app.db` cycle 1：`new` 98 条 / `classic` 7 条。但 `list=new` 按 score 排序的 Top10 是：

| # | canonical | score | rank | 说明 |
|---:|---|---:|---:|---|
| 1 | ai | 87.5 | 0 | 非种子、评论证据通道放行，`status=observing`、不占主榜名次 |
| 2 | 看完 | 86.3 | 0 | 同上 |
| 3 | b站 | 85.8 | 0 | 同上 |
| 4 | ge | 85.5 | 0 | 同上（英文碎片） |
| 5 | 这句 | 85.5 | 0 | 同上 |
| 6 | on | 85.5 | 0 | 同上 |
| 7 | 11 | 85.2 | 0 | 同上 |
| 8 | 20 | 85.1 | 0 | 同上 |
| 9 | 到了 | 85.1 | 0 | 同上 |
| 10 | in | 84.8 | 0 | 同上 |

- 机制：`comment_backed_candidate` 放行的非种子高频碎片绕过 `MEME_MIN` 进 observing（`rank=0`），但 score 含 novelty/exposure/engagement/breadth，可达 85+；而 `list_items(list=new)` 只按 `score DESC`、不过滤 rank=0 → 真正的 3 条新榜梗（咕咕嘎嘎 63.6 / 嘉豪 44.6 / 那咋了 16.9）被挤出 new Top10。
- **口径 caveat**：这是冷库单轮结果，`is_new` 全 true、分数受无历史影响；但「rank=0 comment_admitted 项按 score 混入 new 榜」是代码结构行为，与历史无关，需在 warm-history 库上复验（见 §7 动作 2）。

---

## 2. meme_info 模块结果（/tmp/shadow-module.json）

- `schema_version=meme-info/v1`；`generated_at=2026-11-15T00:19:18+08:00`；exit 0。
- 耗时 **207.0s**（采集 123.4s + 打分/报告 28.76s + LLM 审核 ≈55s）；请求 **228**（预算 330，未触顶，`cache_hits=0`）。
- 语料：视频 **943**（来源 ranking 752 / popular 50 / 搜索 199，去重后 943）/ 评论 **826** / 弹幕 **2000** / docs 4504；families 1841 → `current_families=47`、`current_2x2=21`、`observing=16`、`memes=20`。
- 失败：`errors=[]`、`search_fail=0`、`source_errors=30`（评论页个别失败，未整轮降级）。

### 2.1 LLM 审核 / gate（本轮冷缓存，全部真实调用）

| 项 | 值 |
|---|---|
| model / configured | `deepseek-chat` / **是**（key 从 `~/.hotmeme_llm_key` 读取，未打印） |
| LLM 调用 | **20 次**（prompt 23,022 / completion 3,263 / total 26,285 tokens；估算 ¥0.03–0.05） |
| verification | attempted **20** / ok **17** / rejected **3** / errors **0** |
| gate 剔除（raw_score） | `冷静点`(57.32)、`非凡之爱`(53.93，用户 ❌)、`牛肉面`(53.25) → 本会排在 ~#12–14 被剔掉 |
| 仍漏的用户 ❌ | `最怕BLG夺冠之人`(#12, rising, 已审 true/true)、`腋毛攻击`(#17, observing, 未审) |
| publishable | Top10 **7/10**、Top20 **8/20**（`hot/rising` 且 `evidence_rate_2x2=true`） |
| 2×2 证据 | Top10 **7/10**、Top20 **8/20** |

### 2.2 Top10（= `memes[:10]`，已按发布门排序）

| # | canonical | score | status | 2×2 | publishable | verified（is_meme/is_current, heat, conf） | meta/usage | 用户盲标 |
|---:|---|---:|---|---|---|---|---|---|
| 1 | 吃了不烧心 | 77.97 | hot | ✓ | ✓ | true/true, 72, 0.82 | 5 / 64 | ✅ |
| 2 | 吴家村 | 76.00 | hot | ✓ | ✓ | true/true, 72, 0.78 | 8 / 21 | ✅ |
| 3 | MJ宇宙 | 75.25 | hot | ✓ | ✓ | true/true, 72, 0.78 | 5 / 23 | ✅ |
| 4 | 司凤音乐 | 69.88 | hot | ✓ | ✓ | true/true, 78, 0.82 | 4 / 7 | ✅ |
| 5 | 中国人能飞 | 66.02 | hot | ✓ | ✓ | true/true, 78, 0.82 | 7 / 43 | ✅ |
| 6 | 蓝色大肥鱼 | 65.78 | hot | — | ✗ | true/true, 72, 0.78 | 4 / 7 | **未标** |
| 7 | 灭建国 | 65.37 | hot | ✓ | ✓ | true/true, 72, 0.78 | 6 / 5 | ✅ |
| 8 | 扎啤 | 60.15 | hot | — | ✗ | true/true, 42, 0.72 | 8 / 3 | ✅ |
| 9 | doge | 57.52 | hot | ✓ | ✓ | true/true, 72, 0.78 | 1 / 49 | **未标** |
| 10 | 心生爱慕 | 54.11 | hot | — | ✗ | true/true, 42, 0.62 | 8 / 3 | **未标** |

### 2.3 Top11–20（裁切）

| # | canonical | score | status | 2×2 | 用户盲标 |
|---:|---|---:|---|---|---|
| 11 | 不小心惹了上帝 | 52.09 | hot | — | — |
| 12 | 最怕BLG夺冠之人 | 45.52 | rising | — | ❌ |
| 13 | 借过一下 | 45.35 | rising | — | — |
| 14 | 老牧师 | 45.23 | rising | ✓ | — |
| 15 | PiuPiuPiu | 42.50 | observing | — | ✅ |
| 16 | 狗尾草表情包 | 42.48 | rising | — | ✅ |
| 17 | 腋毛攻击 | 41.29 | observing | — | ❌ |
| 18 | 雪豹眉眉 | 41.26 | rising | — | ❓ |
| 19 | 没希望，佩不上 | 41.02 | rising | — | ✅ |
| 20 | 我画地为牢啊 | 39.65 | rising | — | — |

---

## 3. Overlap / 新面孔

| 对比 | 结果 |
|---|---|
| app Top10 ∩ 模块 Top10 | **0 / 10** |
| app Top10 ∩ 模块 Top20 | **0 / 20** |
| app 全部 105 条 signals ∩ 模块 Top20 | **0 / 20** |
| 模块 Top10 中「app 完全没有的新面孔」 | **10 / 10**（既不在 app 105 条 signals，也不在 148 条种子表） |
| app Top10 种子表命中 | **10 / 10**（旧梗） vs 模块 Top10 **0 / 10** |
| app Top10 ∩ app 自己的 list=new Top10 | 0（两榜内容完全错位） |

- 两个榜单是**内容级完全不同**的两套结果：app 旧 pipeline 在「种子表 ∩ 当前热度」里循环，模块给出的是当前正在用的新梗。
- 这不是时段差异（两轮相隔 16 秒），而是**召回通道差异**：app 377 视频/592 评论，模块 943 视频/826 评论 + 搜索扩量 + 弹幕通道。

## 4. 用户 16 条盲标（`meme_info/labels/blind_20261114_user.json`）

### 4.1 指标

| 口径 | 模块 Top10 | 模块 Top20 | app Top10 | app 全量 |
|---|---|---|---|---|
| ✅ 命中 | **7** | 10 | 0 | 0 |
| ❌ 命中 | **0** | 2 | 0 | 0 |
| ❓ 命中 | 0 | 1 | 0 | 0 |
| 未标（不在 16 锚点） | 3 | 7 | 10 | 105 |
| raw P（✅/N） | **0.70** | **0.50** | 0/16* | 0/16* |
| labeled P（✅/(✅+❌)） | **1.00** | **0.833** | — | — |

\* app 侧是「16 锚点在 app 结果中的召回率」= 0/16，不是同样口径的 precision。

### 4.2 16 锚点位置矩阵

| 用户标签 | 梗 | app Top10 | app 全量最好名次 | 模块名次 |
|---|---|---:|---:|---:|
| ✅ | 吃了不烧心 | — | — | 1 |
| ✅ | 司凤音乐 | — | — | 4 |
| ✅ | 扎啤 | — | — | 8 |
| ✅ | 老实赚钱很枯燥吧 | — | — | **未进 Top20**（候选 raw #11，observing） |
| ✅ | 灭建国 | — | — | 7 |
| ✅ | 中国人能飞 | — | — | 5 |
| ✅ | 吴家村 | — | — | 2 |
| ✅ | MJ宇宙 | — | — | 3 |
| ❌ | 最怕BLG夺冠之人 | — | — | **12（漏）** |
| ❌ | 非凡之爱 | — | — | 未进（gate 正确剔除，raw 53.93） |
| ✅ | PiuPiuPiu | — | — | 15 |
| ✅ | 狗尾草表情包 | — | — | 16 |
| ❓ | 雪豹眉眉 | — | — | 18 |
| ❌ | 腋毛攻击 | — | — | **17（漏）** |
| ✅ | 没希望，佩不上 | — | — | 19 |
| ❌ | 明码标价 | — | — | 未进（规则层剔除，正确） |

- 模块 Top10：**7 ✅ / 0 ❌ / 3 未标**；3 条未标为 `蓝色大肥鱼`、`doge`、`心生爱慕`，需人工复核（`蓝色大肥鱼` 上轮 #18，`doge`/`心生爱慕` 上轮 Top20 外；`心生爱慕` 此前只有 AI 标签，`MEME-INFO-REVIEW §8.2` 已注明）。
- 模块 Top20：13/16 锚点召回；漏掉的 3 条 = `老实赚钱很枯燥吧`（#11 候选但未进报告 20 条）、`非凡之爱`（gate 剔除正确）、`明码标价`（规则剔除正确）。
- gate 本期正确剔除了用户 ❌ `非凡之爱`，但漏掉 `最怕BLG夺冠之人`、`腋毛攻击` 两个已知假阳性 → **gate 仍不完整**。

## 5. 单轮稳定性 / 缓存影响

- 与 00:01 的上一轮模块报告（同默认参数，不同缓存目录）相比：**Top10 overlap 7/10、Top20 15/20**。
  - 新进 Top10：`蓝色大肥鱼`（上轮 #18）、`doge`、`心生爱慕`；掉出 Top10：`闪身步`（上轮 #3）、`不小心惹了上帝`（#7）、`老牧师`（#10）。
- 结论：20 分钟内 Top10 有 3 席轮换，**单轮结果不足以判定切榜**；需 2–3 轮复验。
- 本轮两侧都是冷缓存，结果来自实时采集而非旧缓存；00:01 报告使用 `/tmp/mi_cache`，与本轮 `/tmp/shadow-eval-cache` 无交集。

---

## 6. 结论：是否值得切 `list=new`？

**不值得现在切；继续 shadow。** 依据：

| 维度 | app 旧 pipeline（同轮） | meme_info 模块（同轮） |
|---|---|---|
| Top10 用户 ✅ | 0/16 锚点召回 | 7 ✅ / 0 ❌ / 3 待标 |
| Top10 内容 | 10/10 种子表老梗（2020–2025） | 10/10 当前新梗，0 种子表 |
| publishable | 无该概念 | 7/10 带发布门标记 |
| list=new 可用性 | **不可用**：Top10 被 rank=0 噪声碎片（ai/看完/b站…）占满 | 有发布门 + 2×2 + LLM 审核，Top10 无 ❌ |

**还缺什么（切榜前必须满足）：**

1. **3 条 Top10 未标**（蓝色大肥鱼/doge/心生爱慕）需要用户确认（doge 大概率真梗，其它需人工看）；
2. **2 条已知 ❌ 仍在 Top20**（最怕BLG夺冠之人 #12、腋毛攻击 #17）——gate 回归没修完；
3. **单轮不稳定**：Top10 相对 00:01 有 3 席轮换，需同参数连跑 ≥3 轮看稳定性；
4. **app `list=new` 自身有结构缺陷**（rank=0 comment_backed 观察项按 score 挤占 new 榜前排）——即使不切模块，这个也要修，否则「app 旧 new 榜」本身不可用，无法作为对照基线；
5. **产品口径**：两榜 overlap 0/10 = 切过去是 feed 内容整体换血，需要产品确认「只发 publishable（7–8 条/轮）」是否够用。

## 7. 下一轮（继续 shadow）的 3 个动作

1. **人工补标 + gate 回归**：复核 `蓝色大肥鱼 / doge / 心生爱慕`（Top10 未标）+ `雪豹眉眉`（❓），确认 `最怕BLG夺冠之人 / 腋毛攻击` 假阳性原因；把结果补进 `meme_info/labels/blind_20261114_user.json` 并加 gate 回归用例（验收：Top10 无 ❌、raw P@10 ≥0.8）。
2. **warm-history 双跑复验**：`cp data/hotmeme.db /tmp/shadow-app2.db`，在副本上连跑 app 2–3 轮（第 2 轮起 comment/danmaku 缓存命中、`is_new`/`hype_streak`/`previous_rank` 有历史），同一时段再跑模块 2–3 轮（建议固定 `--now` 或紧邻窗口）；验收：app `list=new` 是否仍被 rank=0 噪声占满、模块 Top10 ≥2/3 轮一致、publishable 数稳定。
3. **修 3 处**：(a) app `list=new` 排序/过滤——rank=0 的 comment_admitted 观察项不得按 score 排在 new 榜前（或 new 榜只取 rank>0 + 发布门）；(b) `tools/meme_info_shadow.py` 直接脚本执行 bug（统一用 `-m tools.meme_info_shadow`，或重命名 `tools/meme_info.py` 避免遮蔽包）；(c) LLM gate 对 2 条漏网用户 ❌ 加二次审/回归。全部通过后再按 `MEME-INFO-REVIEW §6` 顺序决定是否把 `list=new` 数据源切到模块。

## 8. 约束遵守与证据

| 约束 | 证据 |
|---|---|
| 不碰生产库 | `data/hotmeme.db` sha256 `00b3e839…f295`，mtime `1794649432`/size `5640192` 全程未变；所有写入在 `/tmp/shadow-app.db` |
| 不启/重启 8000 | 全程 `lsof -iTCP:8000 -sTCP:LISTEN` 为空；未起服务 |
| 不跑 scheduler | 只显式调用 `pipeline.run_cycle()` 一次；未运行 `run.py` |
| 不 commit / 不改代码配置数据 | `git status` 仅既有未跟踪 `.archify/`；`docs/pm/SHADOW-EVAL.md` 为唯一新增文件 |
| 2 rps | 串行两轮：app 275 req/195s≈1.41 rps，模块 228 req/207s≈1.10 rps；无并发叠加 |
| 只写 /tmp 与本报告 | `/tmp/shadow-app.db`、`/tmp/shadow-app-cycle.json`、`/tmp/shadow-module.json`、`/tmp/shadow-eval-cache/`、`/tmp/shadow-eval-analysis.json` |

**产物**：`/tmp/shadow-app-cycle.json`（app 全量快照）、`/tmp/shadow-module.json`（模块报告 2.45MB）、`/tmp/shadow-eval-analysis.json`（对比矩阵）、本报告。
