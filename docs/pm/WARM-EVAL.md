# Warm-history 3 轮评估：app 旧 pipeline vs meme_info 模块（2026-11-15 00:47–00:56）

- 会话：warm-eval（HEAD `0c55268`；工作区既有未跟踪 `.archify/`，本会话未触碰）
- 目标：app 旧 pipeline + `meme_info` 模块**同段串行 3 轮**（第 1 轮冷缓存，第 2/3 轮 warm），回答两问：
  1. 模块 Top10/publishable 是否稳定、是否出现用户 ❌；
  2. 是否达到 `meme_info/REPLACE-NEW-LIST.md` §2.2 的切榜门槛。
- 网络约束：单进程 2 rps，6 个采集段**串行**；无重试风暴、无 B站限流降级。
- 只写 `/tmp` 与本文件。**未**起/重启 8000、**未**跑 scheduler、**未** commit、**未**改代码/配置/数据（唯一例外见 §8 ⚠️）。

> **结论先行：技术稳定性已达标，但切榜门槛 4 项只满足 1 项 → 本轮不建议切 `list=new`，继续 shadow。**
> 3 轮模块 Top10 **用户 ❌ = 0/0/0**（Top20 也是 0），publishable 7/8/8 条（Top20 10/11/11），Top20 Jaccard **0.905**；app 旧 Top10 仍 10/10 种子表且与模块 overlap **0/10**；app `list=new` 的 rank=0 噪声在 warm 库上**依旧存在**。
> 未满足：① Top10 仍有 1 条连续 3 轮未标（`闪身步`）+ Top20 publishable 有未标项（`牛肉面`/`老牧师`/`给`），需用户复核；② 产品需接受「只发 7–8 条/轮」的内容换血；③ 旧 `list=new` 尚未下线（且 warm 库实测不可用）。另需补 1 轮 **LLM 缓存冷** gate 复验（warm 轮 gate 结论大部分来自缓存）。

---

## 0. 固定 env / 复现命令 / 时间线

```bash
# ① app 旧 pipeline（复用同一库：r1 冷、r2/r3 warm；不启 8000、不碰生产库）
cd /Users/wanjunqiang/Desktop/hot_meme_search
HOTMEME_DB=/tmp/warm-app.db HOTMEME_MAX_RPS=2 HOTMEME_LLM_MODE=off HOTMEME_LLM_EXTRACT=0 \
  .venv/bin/python - <<'PY'
from app import pipeline, store
pipeline.run_cycle()                 # 每轮一次；stats/cycle 落 /tmp/warm-app.db
PY

# ② meme_info 模块（复用同一缓存目录：r1 冷、r2/r3 warm）
HOTMEME_LLM_API_KEY_FILE=$HOME/.hotmeme_llm_key \
  .venv/bin/python -m tools.meme_info_shadow \
    --out /tmp/warm-module-rN.json --cache-dir /tmp/warm-cache \
    --limit 20 --verify --verify-gate
```

- 依 `docs/pm/SHADOW-EVAL.md` §0.1 的坑：模块必须用 `-m tools.meme_info_shadow`（直接跑 `.py` 会被 `tools/meme_info.py` 遮蔽包）。
- app 侧 3 轮均 `HOTMEME_LLM_MODE=off / HOTMEME_LLM_EXTRACT=0`（零 LLM 调用，`llm_mode=off`）。
- r1 两侧缓存均冷：app `comment_cached=0 / danmaku_cached=0`，模块 `cache_hits=0`；r2/r3 复用 → app `comment_cached=180 / danmaku_cached=10`，模块 `cache_hits=160 / 149`。
- key 从 `~/.hotmeme_llm_key` 读取（36B 文件存在），全程未打印内容。

| 段 | 开始 | 结束 | 墙钟 | 类型 |
|---|---|---|---:|---|
| app r1 | 00:47:19 | 00:49:15 | **116.0s** | 冷 |
| module r1 | 00:49:16 | 00:52:44 | **208.7s** | 冷 |
| app r2 | 00:52:44 | 00:53:09 | **25.0s** | warm |
| module r2 | 00:53:09 | 00:54:20 | **70.3s** | warm |
| app r3 | 00:54:20 | 00:54:42 | **22.0s** | warm |
| module r3 | 00:54:42 | 00:55:53 | **70.7s** | warm |

- 总跨度 **00:47:19–00:55:53（8 分 34 秒）**，6 段首尾相接、串行，任意时刻仅 1 个采集进程（≤2 rps）。
- 模块内部 `elapsed_total`（不含 LLM 审核墙钟）= r1 147.8s / r2 68.5s / r3 67.1s；与墙钟的差 ≈ LLM 审核时间（r1 37 次调用差 61s，r2/r3 各 1–2 次调用差 2–4s）。
- **工具坑（不影响数据）**：app 快照后处理脚本引用了不存在的 `s.hype_streak` 列，导致每轮 app 快照 JSON 未写出；但 `pipeline.run_cycle()` 已正常提交到 `/tmp/warm-app.db`，快照由该库**只读重建**（耗时/请求/统计取自 `cycle` 表）。三轮 app 采集各只跑了一次，无重复请求。

## 1. 三轮总表

| 轮 | 侧 | 状态 | 耗时 | 请求 | 视频 | 评论 | 弹幕 | families→usable | 缓存 | 错误 |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|
| r1 | app | done | 116.0s | 223 | 377 | 642 | 1704 | 629→111 | 冷（评论缓存 0） | `errors=[]`、`comment_page_fail=0` |
| r1 | 模块 | ok | 208.7s | 198 | 943 | 1013 | 2000 | 1815→current 23 / 2×2 13 | `cache_hits=0` | `errors=[]`、`source_errors=[]`、`search_fail=0` |
| r2 | app | done | 25.0s | 31 | 379 | 642 | 1704 | 628→111 | 评论缓存 180 / 弹幕 10 | 同上，0 失败 |
| r2 | 模块 | ok | 70.3s | 38 | 944 | 1025 | 2000 | 1852→current 22 / 2×2 13 | `cache_hits=160` | 同上，`budget_exhausted=false` |
| r3 | app | done | 22.0s | 35 | 376 | 675 | 1704 | 634→113 | 评论缓存 176 + 新翻 4 页 | 同上，0 失败 |
| r3 | 模块 | ok | 70.7s | 49 | 943 | 1037 | 2000 | 1816→current 22 / 2×2 13 | `cache_hits=149` | 同上，`budget_exhausted=false` |

- app warm 轮 **25s/22s、31/35 请求**（冷轮 116s/223 请求）——`comment_cached` 全命中后不再翻评论页；warm-history 目标达成（r2 起 `is_new`/`previous_rank`/`hype_streak` 有历史）。
- B站限流：app `comment_page_fail=0 / comment_degraded=false`；模块 `source_errors=[] / search_fail=0`；未出现限流风暴，无需重试。
- 模块三轮都 **`budget_exhausted=false`**（请求 198/38/49，预算 330），warm 轮请求量仅冷轮的 1/5–1/4。

## 2. app 旧 pipeline（三轮）

### 2.1 Top10（全部命中种子表）

| # | r1 | r2 | r3 |
|---:|---|---|---|
| 1 | 抽象 89.4 hot | 抽象 89.5 hot | 抽象 88.8 hot |
| 2 | 离谱 85.0 | 上头 78.6 hot | 上头 78.3 hot |
| 3 | 上头 78.8 | 整活 71.5 hot | 整活 71.5 hot |
| 4 | 整活 71.7 | 离谱 68.9 | 咕咕嘎嘎 63.0 hot |
| 5 | 咕咕嘎嘎 63.2 | 咕咕嘎嘎 62.9 hot | 离谱 62.3 |
| 6 | 吃瓜 58.3 | 吃瓜 58.2 | 吃瓜 58.2 |
| 7 | 嘉豪 44.1 | 嘉豪 44.0 | 嘉豪 46.4 |
| 8 | yyds 42.9 | yyds 42.8 | yyds 43.0 |
| 9 | 绝绝子 30.6 | 绝绝子 30.6 | 绝绝子 30.8 |
| 10 | 那咋了 16.6 | 那咋了 16.6 | —（本轮仅 9 条 rank>0） |

- **种子表命中：10/10、10/10、9/9（100%）**；三轮 Top10 名单 Jaccard **1.0 / 0.9 / 0.9**（r3 只少 `那咋了`）。warm-history 没有改变 app 旧榜的「种子表 ∩ 热度」结构。
- 用户 16 锚点召回：app Top10 **0/16**；app 全部 signals（111/111/113 条）**0/16** —— app 旧 pipeline 三轮都没有碰到 16 条当前梗。

### 2.2 ⚠️ app `list=new` 噪声在 warm 库上依旧存在（r1–r3 top5）

| 轮 | list=new Top5（全部 `rank=0`、`status=observing`、comment_backed） |
|---|---|
| r1 | `ai` 87.9 / `b站` 86.6 / `ge` 86.0 / `on` 85.8 / `这句` 85.5 |
| r2 | `ai` 88.2 / `b站` 86.6 / `11` 86.2 / `on` 86.1 / `ge` 85.9 |
| r3 | `ai` 88.3 / `b站` 87.4 / `11` 86.1 / `on` 86.0 / `20` 85.8 |

- 三轮稳定复现：`list=new` 98–106 条中，按 `score DESC` 排在前面的仍是 `rank=0` 的评论碎片观察项，真正的新榜梗（`咕咕嘎嘎`/`嘉豪`/`那咋了`）被挤出。**这验证了 `REPLACE-NEW-LIST.md` §1 的结构性判断与 `SHADOW-EVAL.md` §7 动作 2 的验收点：旧 `list=new` 在 warm-history 上也不可作为切榜基线，必须修或由模块替代。**

## 3. meme_info 模块（三轮，`--limit 20 --verify --verify-gate`）

### 3.1 Top10 逐轮名单

| # | r1 | r2 | r3 |
|---:|---|---|---|
| 1 | 吃了不烧心 ✅ P | 吃了不烧心 ✅ P | 吃了不烧心 ✅ P |
| 2 | 司凤音乐 ✅ P | 闪身步 — P | MJ宇宙 ✅ P |
| 3 | 灭建国 ✅ P | 司凤音乐 ✅ P | 吴家村 ✅ P |
| 4 | 扎啤 ✅ P | 扎啤 ✅ P | 司凤音乐 ✅ P |
| 5 | 闪身步 — P | 灭建国 ✅ P | 闪身步 — P |
| 6 | 蓝色大肥鱼 AI✅ | 蓝色大肥鱼 AI✅ | 蓝色大肥鱼 AI✅ |
| 7 | 中国人能飞 ✅ P | 中国人能飞 ✅ P | 灭建国 ✅（本轮非 P） |
| 8 | 吴家村 ✅ P | doge AI✅(evergreen) P | 扎啤 ✅ P |
| 9 | 心生爱慕 AI✅ | 吴家村 ✅ P | doge AI✅ P |
| 10 | 不小心惹了上帝 — | 心生爱慕 AI✅ | 中国人能飞 ✅ P |

- `✅/❌/❓` = 用户 16 盲标；`AI✅` = 补标 4 条（`蓝色大肥鱼` current、`心生爱慕` current、`doge` evergreen、`雪豹眉眉` unclear），**单独标注、不混入用户 16 锚点**；`P` = publishable（`hot/rising` 且 `2×2`）。
- **Top10 用户 ❌ = 0 / 0 / 0；用户 ❓ = 0；用户 ✅ = 6 / 6 / 7；未标 = 4 / 4 / 3（其中 AI 补标 2 / 3 / 2）。** 合并 AI 补标后：8/10、9/10、9/10 为「已确认真梗」，0 条假阳性；仅剩 **`闪身步`**（r1/r2/r3 均 #5/#2/#5、3 轮全 publishable）以及 r1 的 `不小心惹了上帝`（r2 起掉出）未被任何标签覆盖。
- Top20 用户锚点召回：**11✅ / 10✅ / 10✅，❌ 均为 0，❓ 各 1（`雪豹眉眉` #17/#17/#16）**。
- Top20 用户 ❌：无。上一轮 shadow 评估里 Top20 还有 `最怕BLG夺冠之人`(#12)、`腋毛攻击`(#17)；本轮 **3/3 轮都被 gate 剔除**（见 §3.3）。

### 3.2 publishable / 2×2 / 观察项（切榜实际内容）

| 轮 | Top10 publishable | Top20 publishable | current_2x2 | current_families | observing |
|---|---|---|---:|---:|---:|
| r1 | **7**（吃了不烧心/司凤音乐/灭建国/扎啤/闪身步/中国人能飞/吴家村） | **10**（+牛肉面/doge/老牧师） | 13 | 23 | 4 |
| r2 | **8**（同上 + doge） | **11**（+牛肉面/老牧师/给） | 13 | 22 | 3 |
| r3 | **8**（吃了不烧心/MJ宇宙/吴家村/司凤音乐/闪身步/扎啤/doge/中国人能飞） | **11**（+牛肉面/老牧师/给） | 13 | 22 | 2 |

- publishable 数稳定：**Top10 7/8/8（Δ1）、Top20 10/11/11（Δ1）**；`current_2x2` 三轮恒为 13。
- 轮换细节：r1→r2 `doge` 升为 publishable；r2→r3 `MJ宇宙` 进 publishable、`灭建国` 本轮 `2×2` 丢失（掉出 publishable 但仍在 Top10 #7）；`牛肉面`/`老牧师`/`给` 是 Top20 里的 publishable 未标项（`limit=8` 时不进 feed，`limit>10` 需复核）。
- publishable Top10 集合 Jaccard：**0.875 / 0.778 / 0.667**（7–8 条的小集合，1 席变动即明显波动，属正常范围）。

### 3.3 LLM 审核 / gate（3 轮）

| 轮 | attempted | ok | rejected | errors | unverified_omitted | LLM calls | tokens | 缓存 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| r1 | 40 | 23 | **17** | 0 | 9 | **37**（真调用） | 44,666 | 冷 |
| r2 | 40 | 22 | **18** | 0 | 10 | 1（39 条命中缓存） | 849 | warm |
| r3 | 40 | 22 | **18** | 0 | 7 | 2（38 条命中缓存） | 3,083 | warm |

- 用户 3 条 ❌ **3/3 轮均被 gate 剔除**：`非凡之爱`、`最怕BLG夺冠之人`、`腋毛攻击` → `llm_not_meme`；`明码标价` 在规则层（`common_phrase`）未进候选。**gate 修复（`f841d2e`）在 warm 库 3 轮无回归。**
- ⚠️ 口径 caveat：r2/r3 的 gate 结论 **20/20 来自 r1 的 LLM 缓存**（`verified_cached=true`），真实新调用仅 1–2 条。所以「3 轮无 ❌」目前是「r1 真审 + 2 轮缓存复现」；切榜前应再跑 **1 轮 LLM 缓存冷**（新 `--cache-dir`）确认 gate 不是靠缓存稳定。

### 3.4 Top20 稳定性

- 三轮 Top20 Jaccard **0.905 / 0.905 / 0.905（19/20 相同）**；Top10 Jaccard **0.818 / 0.818 / 0.667**。
- **3 轮都在 Top10 的 8 条**：`吃了不烧心`、`司凤音乐`、`灭建国`、`扎啤`、`闪身步`、`蓝色大肥鱼`、`中国人能飞`、`吴家村`。
- Top10 轮换（每轮仅 1 进 1 出）：r1→r2 出 `不小心惹了上帝` / 进 `doge`；r2→r3 出 `心生爱慕` / 进 `MJ宇宙`。
- Top20 变动同样很小（19/20 重合），主要为 `老实赚钱很枯燥吧`（r1 #19 / r2 外 / r3 #18）、`PiuPiuPiu`（r3 掉出）。

## 4. 三轮对比矩阵（回答两问）

| 维度 | r1（冷） | r2（warm） | r3（warm） | 判定 |
|---|---|---|---|---|
| 模块 Top10 用户 ❌ | 0 | 0 | 0 | ✅ 无用户 ❌ |
| 模块 Top20 用户 ❌ | 0 | 0 | 0 | ✅（gate 剔除 3 条已知 ❌） |
| Top10 用户 ✅（16 锚点） | 6 | 6 | 7 | — |
| Top10 未标（含 AI 补标） | 4（2 AI） | 4（3 AI） | 3（2 AI） | 剩 `闪身步` 待用户复核 |
| publishable Top10 / Top20 | 7 / 10 | 8 / 11 | 8 / 11 | ✅ 稳定（Δ1） |
| Top20 Jaccard（相邻轮） | — | 0.905 | 0.905 | ✅ 高稳定 |
| app Top10 种子命中 | 10/10 | 10/10 | 9/9 | app 旧榜未变 |
| app∩模块 Top10 overlap | 0/10 | 0/10 | 0/10 | 内容完全换血 |
| app signals∩模块 Top20 | 0/20 | 0/20 | 0/20 | 模块内容 app 从未召回 |
| app `list=new` top5 噪声 | ai/b站/ge/on/这句 | ai/b站/11/on/ge | ai/b站/11/on/20 | ❌ 结构性噪声仍在 |

## 5. 切榜门槛评估（对照 `REPLACE-NEW-LIST.md` §2.2）

| # | 门槛 | 结果 | 证据 |
|---:|---|---|---|
| 1 | 同参数 shadow 连跑 ≥3 轮，Top10 无用户 ❌，publishable 数稳定 | **✅ 技术项满足** | 3 轮同命令同缓存；Top10/Top20 ❌=0/0/0；publishable 7/8/8（Top20 10/11/11）；Top20 Jaccard 0.905 |
| 2 | 用户复核新的 Top10 未标项（蓝色大肥鱼/doge/心生爱慕） | **⏳ 部分完成 / 仍有缺口** | 4 条补标已由 AI+网页完成（不计入用户锚点）；但 **`闪身步` 连续 3 轮 Top10 且 3 轮 publishable、无任何标签**；r1 `不小心惹了上帝`、Top20 publishable `牛肉面`/`老牧师`/`给` 也未复核 |
| 3 | 产品接受「只发 publishable（约 7–8 条/轮）」 | **⏳ 待产品确认** | 数量稳定 7–8 条；但与旧榜内容 0 重叠 = feed 整体换血，且 Top10 里 2–3 条「hot 但无 2×2」不进 feed |
| 4 | 旧 `list=new` 已从 UI/API 下线或明确标 shadow | **❌ 未做** | `/api/top?list=new` 仍在线；warm 库三轮实测 Top5 被 `rank=0` 碎片占满（§2.2）。此项是 PM/前端动作，可与切榜解耦先做 |

**判定：4 项中 1 项满足、2 项未满足、1 项待产品 → 本轮不建议切榜。**

## 6. 结论与下一轮动作

**结论：继续 shadow，不切 `list=new`。** 理由：稳定性与「无用户 ❌」两条硬指标已达标（3 轮、0 ❌、publishable 稳定、Top20 高度重合），但门槛 2/3/4 未完成；且 warm 轮 gate 结论大部来自 LLM 缓存，需要 1 轮冷缓存真调确认。

下一轮（按优先级）：

1. **用户复核 3 条**（阻塞项）：`闪身步`（3 轮 Top10 + publishable，最高优先）、`不小心惹了上帝`（r1 未标已掉出）、`给`（Top20 publishable，短词可疑）；顺手确认 `牛肉面`/`老牧师`。结果补进 `meme_info/labels/blind_20261114_user.json`（`闪身步` 若为 ❌，需回归 gate）。
2. **产品确认**：publishable 7–8 条/轮是否可接受、Top10 中 2–3 条「hot 无 2×2」是否只在 shadow 展示；确认后再谈切换时间点。
3. **PM 先隐藏/标注旧 `list=new`**（前端或 API 展示层，不动 `app/pipeline.py`），保留数据随时回滚。
4. **补 1 轮 LLM 缓存冷 gate 复验**：新 `--cache-dir`（如 `/tmp/warm-cache-cold`）跑 `--verify --verify-gate`，确认 17–18 rejected / unverified_omitted 不是缓存造成的假稳定。
5. 以上完成后，再按同样方法跑 3 轮 warm shadow 收口，然后切榜。

> 若 1–4 全部通过，切换步骤（备查）：① 先隐藏旧 new —— 前端停用 new tab / `/api/top?list=new` 标注「shadow·不可用」，数据不删；② 新榜数据源 `GET /api/meme-info?only=publishable&limit=8`（只读 adapter 已上线，`only` 仅支持 `all|publishable`）；③ 保留回滚 —— `/api/top?list=all|classic` 与旧 new 数据源原样保留，`meme_info_latest.json` 缺失/stale 时 adapter 返回 `missing/stale` 而非 500；④ 验收：连续 3 轮 publishable ≥7、Top10 无 ❌、300 tests 全绿。

## 7. 约束遵守与证据

| 约束 | 证据 |
|---|---|
| 不启/重启 8000 | 全程 `lsof -iTCP:8000 -sTCP:LISTEN` 为空（开始/结束各查一次）；未起服务 |
| 不跑 scheduler | 未运行 `run.py`；仅显式调用 `pipeline.run_cycle()` 3 次（对应 `/tmp/warm-app.db` cycle 1–3） |
| 不 commit / 不改代码配置 | `git status` 仅既有未跟踪 `.archify/` + 本文件；`app/`、`meme_info/`、配置零改动 |
| 2 rps | app r1 223 req/116s≈1.92 rps、r2/r3 31–35 req/22–25s；模块 198 req/208.7s≈0.95 rps；6 段串行无叠加 |
| 不写 `data/hotmeme.db` | ⚠️ 有一次非预期物理写，见 §8；无业务行/表数据变更（内容 == 00:11 既有副本） |
| 只写 /tmp + 本报告 | `/tmp/warm-app.db`、`/tmp/warm-cache/`、`/tmp/warm-module-r{1,2,3}.json`、`/tmp/warm-app-r{1,2,3}.json`、`/tmp/warm-analysis.json`、`/private/tmp/warm-run.log` |

## 8. ⚠️ 约束偏差（生产库非预期物理写，已止损并取证）

- **事实**：`data/hotmeme.db` 00:47:19 基线 `sha256=00b3e839…f295 / size=5,640,192 / mtime=17:43:52`；00:52:28 变为 `sha256=8b533acb… / size=5,697,536`（此后再未变化）。
- **原因**：app 快照从 DB 重建的辅助脚本（`/private/tmp/warm_recover_app.py`，首版）未设置 `HOTMEME_DB`，误连默认生产库并调用了 `store.init_db()` → WAL checkpoint + 幂等 schema 检查（`executescript` + `_migrate` + `commit`）。非有意写入，也未运行任何采集。
- **影响评估（已只读核对）**：
  - 当前 `data/hotmeme.db` 字节 **完全等于** `/private/tmp/shadow-web.db`（00:11 既有只读副本，`sha256=8b533acb… / 5,697,536B`）→ 逻辑内容 == 00:11 生产快照；
  - `PRAGMA integrity_check=ok`；`cycle=24`、`meme_signal=341`、`meme=1650` 与副本一致；**无新增/修改业务行**（仅 00:47 尚未 checkpoint 的 WAL 被并入主文件，导致 size/mtime/hash 变化）；
  - 00:47 的物理字节（含未 checkpoint WAL）未单独备份，无法按字节还原；为避免扩大影响未做二次写入。
- **后续**：所有脚本已固定 `HOTMEME_DB=/tmp/warm-app.db`；本轮之后未再触碰生产库。建议 lead/PM 复核后决定是否保留此等价状态（若要求严格字节一致，需要 00:47 的备份）。

## 9. 产物

- 本报告：`docs/pm/WARM-EVAL.md`
- 原始数据：`/tmp/warm-app.db`（3 cycles）、`/tmp/warm-app-r{1,2,3}.json`、`/tmp/warm-module-r{1,2,3}.json`（各 ≈2.4MB 完整报告）、`/tmp/warm-cache/`、`/tmp/warm-analysis.json`（三轮矩阵/锚点/Jaccard）、`/private/tmp/warm-run.log`（逐段时间戳）
