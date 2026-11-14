# meme_info · 热梗信息模块

独立、可回放的热梗发现与热度打分库。解决两个问题：

1. **准确地查到当前热梗**：不是把热搜标题当梗，而是同时看「谁在专门讲它」和「谁在真实使用它」；
2. **准确地给热梗热度打分**：把元话语、真实使用、增速、互动、广度、先验分开计量，输出 0–100、可解释、带置信度和时间衰减的分数。

模块不依赖 `app/`、不写 SQLite、不起服务；HTTP/LLM 客户端都可注入。PM 组装时只需做 adapter。

---

## 1. 快速开始

```bash
# 端到端（真机；缓存与报告都写 /tmp）
python -m tools.meme_info query \
  --out /tmp/meme_report.json \
  --limit 30 \
  --comment-videos 45 \
  --danmaku-videos 10 \
  --max-requests 330 \
  --cache-dir /tmp/meme_cache \
  --expand-limit 20

# 只采集 / 只发现 / 只打分
python -m tools.meme_info collect --out /tmp/corpus.json --cache-dir /tmp/meme_cache
python -m tools.meme_info discover --corpus /tmp/corpus.json --out /tmp/candidates.json
python -m tools.meme_info score --corpus /tmp/corpus.json --out /tmp/scored.json --limit 50

# 离线 fixture（不联网）
python -m tools.meme_info query \
  --offline-fixtures tests/fixtures \
  --out /tmp/offline.json --now 2025-04-10T12:00:00+08:00 --quiet

# 评测（银标，见 §8）
python -m tools.meme_info eval \
  --pred /tmp/meme_report.json \
  --labels meme_info/labels/current_20261114.json \
  --k 10 --markdown

# 可选的 LLM 语义审核（shadow；--verify-gate 才按结论剔除）
python -m tools.meme_info query --verify --verify-limit 20 --out /tmp/report_llm.json
```

要求 Python 3.10+；运行时只用标准库。`tools/meme_info.py` 只是 `meme_info.cli:main` 的入口。

---

## 2. 架构

```
B站公开接口                 知识库（只读 data/）
ranking/popular/search  ─┐   seed/alias/blocklist
comments/danmaku        ─┤
                          ▼
                    collect_corpus
          （视频、评论、弹幕、标题/简介；可缓存/回放）
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
   标题元话语抽取               使用证据抽取
   「梗百科/梗指南/盘点」      引号、上下文、n-gram、
   「X 是什么梗」              弹幕整句、种子/别名
             └────────────┬────────────┘
                          ▼
                 extract_candidates
                          ▼
                   merge_families
              （别名优先 + 字符相似归并）
                          ▼
                      HeatModel
       元话语/使用/增速/互动/广度/先验 → 0–100
                          ▼
                HotMemeReport（JSON）
                          ▼
             可选 SemanticVerifier（LLM）
              shadow 或 gate，失败自动降级
```

模块边界：

- 可以读 `data/` 的种子/别名/黑名单；
- 不 import `app.api` / `app.store` / `app.pipeline`；
- 不写生产库、不启动 FastAPI、不改 `app/static`；
- 真机验证默认写 `/tmp`，key 只从环境变量/key 文件读取，不打印、不入库。

---

## 3. 查询：怎么做到「当前」

### 3.1 多通道，各自只回答一个问题

| 通道 | 回答的问题 | 用法 |
|---|---|---|
| 分区榜 / 全站热门 | 最近什么视频在被看 | 曝光底数与视频统计；不直接当梗 |
| 搜索（10 组梗意图词） | 谁在专门讲梗 | 「梗百科/梗指南/梗学堂/盘点/是什么梗」标题是**元话语证据** |
| 评论（热度 + 时间序） | 梗有没有被真实使用 | 带 `ctime`/`like`，是使用线主证据 |
| 弹幕 | 整句是否被重复刷 | 模仿性扩散旁证，文本短、重复高 |
| 热搜 square | 当前事件是什么 | 只记录，不参与打分；避免把新闻当梗 |
| 定向搜索（expansion） | 这个候选有没有人当梗讲 | 对「有使用但没元话语」的候选查 `X 梗`，补元话语/关注度 |

**为什么不能只搜「热梗」**：热搜/榜单标题绝大多数是新闻、赛事、PV；梗的核心是「可复制的符号被模仿使用」。
**为什么不能只看评论 n-gram**：大量高频串是句子碎片；单独的「音乐/游戏/原创」也会因为评论多而冒头。
两条线必须交叉验证：**有人专门讲** + **有人在用**，或强到足以单独成立的使用证据。

### 3.2 元话语抽取（discover.parse_title_meta）

支持：

- `【梗百科】X是啥梗？`、`【梗指南】X是什么梗`、`X是什么梗【梗学堂】`；
- 盘点/合集标题冒号后的列表：`盘点近期网络热梗：A、B、C`；
- 栏目名后的问号分段：`【梗百科】雪豹眉眉？冷静点？...`；
- 别名/种子命中时以种子 canonical 为准（人工别名优先级最高）。

刻意不做：从所有「含梗标题」里泛抽引号/话题标签（实测会把《原神》《循环歌单》当梗）、把 `X是啥样的`
当成 `X是啥梗`。宁缺毋滥。

### 3.3 使用抽取

- 引号内容、`X是什么梗/出处` 上下文、`#话题#`；
- 2–6 字 n-gram：频次、文档频率、PMI 凝固度、左右邻接熵；
- 弹幕整句（3–20 字）重复；
- 种子/别名直接扫描；
- **回扫**：每个候选（尤其元话语候选）都要回扫全语料补使用计数，否则「标题里有、评论里也用」的梗会漏掉使用线；
- 证据 `quote` 必须能通过 `find_quote` 回映到原文；找不到就不产出证据（不编造）；
- ASCII 候选按词边界匹配（`shi` 不会命中 `bushi`）。

### 3.4 归并与当前性

- 种子/人工别名强制归并；
- 字符级 Jaccard ≥0.8、包含且差 ≤2（2 字中文词只允许差 1）、编辑距离 ≤1（仅 ≥3 字）；
- 2 字词不做「差一个字」归并（`上头` 不会并 `上课`，`出圈` 不会并 `出道`）；
- 梗族的 canonical 依次取：种子 > 最高置信元话语 > 最高频候选；
- `is_current`：近 60 天有梗视频/搜索关注，或近 7 天有使用，或近 30 天 ≥2 条使用；
- 无近 30 天证据 → `stale`，不进入当前榜；只有旧梗百科、没有近期使用/关注 → 不算当前；
- **2×2 证据徽章**：`signals.evidence_docs ≥ 2` 且 `evidence_videos ≥ 2`（只数评论/弹幕，且排除该梗自己的梗视频下的评论）。它**不是查询硬门槛**——用户盲标确认 `吴家村/灭建国/MJ宇宙/狗尾草` 等 explainer-only 条目也是真梗；发布门再用该字段决定是否只发「强证据」；
- **过滤规则**：`Knowledge.blocked_reason` 接入 lexicon/common words/proper noun（地名/机构后缀）与 PM 复核普通短语；被过滤项写入 `stats.discovery_audit`，可看到 `doc_mentions → candidate_before_filter → kept/reason`。

---

## 4. 热度模型

### 4.1 信号与权重

每个梗族先算原始信号，再归一化到 0–1。权重（`HeatConfig.weights`，合计 1.0）：

| 信号 | 权重 | 原始量 | 归一化 |
|---|---:|---|---|
| `meta_reach` | 0.45 | 近 90 天梗视频播放：`editorial + 0.35 × search_interest` | `log1p(v)/log1p(5,000,000)` 截断 |
| `meta_coverage` | 0.18 | 专门讲梗的视频数/盘点数/搜索相关数分档 | 1 个 dedicated → 0.60；2 个 → 0.73；1 个盘点 → 0.625 |
| `organic` | 0.13 | 评论/弹幕使用量、覆盖视频、加权点赞 | 0.45 量 + 0.30 扩散 + 0.25 点赞质量 |
| `velocity` | 0.07 | 近 7 天 vs 前 23 天的使用与梗视频速率 | `log2` 比值映射到 0–1 |
| `engagement` | 0.09 | 梗视频赞/播、使用评论平均赞 | 0.6 元话语 + 0.4 使用 |
| `breadth` | 0.04 | 使用视频/分区/元话语 UP 数 | 饱和加权 |
| `prior` | 0.04 | 已知梗/专门梗视频/盘点/引号模式 | 分层先验 |

设计说明：

- **播放不是热度本身**：一个 300 万播放的「梗百科」只说明有人做了解释；还要看有没有人真的在用。所以 `meta_reach` 权重高但不到一半，`organic`/`velocity` 仍有实质影响。
- **搜索相关视频打折**：定向搜索 `X 梗` 命中的视频，标题不一定是专门讲梗，按 0.35 计入播放、低权重计入覆盖。
- **不用批内百分位做主轴**：百分位会让「同一批里矮子拔将军」，跨轮不可比；这里用固定锚点（5M 播放、40 条使用、12 个视频）做绝对归一，报告可跨轮比较。
- **先验只做小权重**：种子先验不能把老梗直接抬上榜；它主要帮助归并与过滤。

### 4.2 当前性、置信度与惩罚

```
base       = Σ w_i × s_i
recency    = 0.5 ^ (days_since_last / half_life)      # meta 21d / usage 10d，混合取均值
confidence = 0.2 + 0.45×volume + 0.35×diversity      # dedicated +0.08；无 organic ×0.85
score      = 100 × base × recency × (0.7 + 0.3×confidence) × penalty
```

- `days_since_last` 取最近一条元话语/使用证据；没有近期证据自然掉分。
- `penalty`：单视频占比 >0.7 → 最多 ×0.7；只有 1 个梗视频且无使用 → ×0.9；常青词且无专门梗视频 → ×0.75。
- 分数 0–100；`hot` ≥50，`rising/observing` ≥15，低于 15 为 `weak`；无证据/旧证据/单视频/低梗性 → `rejected`；
- `HotMemeReport.memes` 放入本轮 `hot/rising/observing` 的当前项（查询不因 2×2 硬杀掉 explainer-only 真梗）；`report.observing` 是其中 `status=observing` 的子集，供发布门/UI 标「观察」。发布门应只发 `hot/rising` 且 `evidence_rate_2x2=true` 的强证据项。
- 每次输出 `components`（raw/weight/points）与 `reasons`（中文），可逐项核对。
- 历史平滑：传入 `history` 时输出 `smoothed_score = 0.65×本轮 + 0.35×上轮`，排序优先用平滑分；`score` 保留本轮原始值。

### 4.3 热度分级

| 分数 | 级别 | 含义 |
|---:|---|---|
| ≥70 | 爆发 | 多个近期梗视频 + 使用/关注强 |
| 50–70 | 高位 | 当前明确在场 |
| 15–50 | 上升/活跃 | 有近期证据，但深度或广度有限 |
| <15 | 低证据 | 仅作候选，不进入当前榜 |

---

## 5. 对外接口

```python
from meme_info import (
    BiliSource, CollectionPlan, collect_comments, collect_corpus,
    extract_candidates, extract_meta_mentions, merge_families,
    HeatConfig, HeatModel, Knowledge,
    QueryConfig, find_hot_memes, query_from_corpus,
    evaluate, evaluate_discovery,
    ChatClient, SemanticVerifier, review_candidates,
)
```

交接单要求的最小接口：

```python
collect_comments(targets, client, *, window_days=30, max_per_video=40) -> list[Comment]
extract_candidates(comments, llm, *, max_calls=3, max_comments=450, prompt_version=...) -> dict
review_candidates(candidates, llm, *, max_rounds=2, target=10, max_calls=20) -> dict
evaluate(predictions, labels) -> dict
```

补充：

- `extract_candidates(docs, knowledge, now=..., meta_mentions=...)` 是规则抽取主入口；
  `meme_info.verify.extract_candidates(comments, llm, ...)` 是 LLM 抽取入口（同名不同命名空间）。
- 组装时 app 只依赖 `find_hot_memes(...).to_dict()` 或 `query_from_corpus(corpus, ...)`；
  `Corpus` 可 JSON 回放，`HotMemeReport` 字段稳定并带 `schema_version`。

### 数据契约

与 `docs/pm/ENGINEER-HANDOFF.md` 兼容，主要字段：

```json
Comment  = {"id": "bvid:rpid", "bvid": "...", "text": "...", "ctime": 1790000000,
            "like": 123, "zone": "...", "video_title": "..."}

Evidence = {"comment_id": "...", "quote": "原文子串", "ctime": 1790000000,
            "like": 123, "bvid": "...", "kind": "comment|danmaku|title"}

Candidate = {"candidate_id": "sha1(norm)", "surface": "...", "canonical": "...",
             "type": "词|句式|谐音|缩写|名场面", "evidence": [...],
             "confidence": 0.0, "known": false, "comment_7d_count": 0,
             "comment_df_video": 0, "age_days": 0.0, ...}

Meme = {"family_key": "...", "canonical": "...", "members": [...],
        "score": 0.0, "status": "hot|rising|observing|weak|rejected",
        "publishable": false, "components": {...},
        "signals": {"evidence_docs": 0, "evidence_videos": 0,
        "evidence_rate_2x2": false, ...}, "reasons": [...], ...}

HotMemeReport = {"schema_version": "meme-info/v1", "generated_at": "...",
                 "memes": [...], "observing": [...], "candidates": [...],
                 "stats": {...}, "config": {...}, "notes": [...]}
```

---

## 6. 可选 LLM 审核

- 没有 key → `not_configured`，规则榜照常，不伪造。
- 支持 `MEME_INFO_LLM_*` 或项目已有的 `HOTMEME_LLM_*`；默认读 `~/.hotmeme_llm_key`。
- Prompt 只给证据（视频标题/时间/播放、评论原文/时间/点赞），**不给规则分数**，避免锚定。
- 输出严格校验：`is_meme`/`is_current` 布尔、`heat` 0–100、`confidence` 0–1，`evidence_ids` 必须在证据白名单内。
- 默认 shadow：只记录 `verified`，不改规则榜；`verify_gate=true` 时才按结论剔除。
- 失败（无 key/超时/坏 JSON/预算）只降级，不阻塞查询。
- 缓存键含 `prompt_version + model + 梗族 + 证据包`，重复跑不产生重复费用。

---

## 7. 复现与成本

- 默认 2 rps；`ranking 8 + popular 1 + hot_search 1 + search 10 + comments 135 + danmaku 10` 左右。
- 45 视频 / 10 弹幕 / 10 搜索词的一轮：约 **300 请求、150–210s**（冷缓存），评论 900+、弹幕 2000。
- HTTP 文件缓存按 URL + TTL；warm 轮显著减少请求。
- LLM 审核 20 条约 2 万 tokens（DeepSeek 约 ¥0.002–0.01/轮，价格随供应商变化）；报告里写 `verification.llm` 的 token 数。
- 真机验证不写生产库；报告/缓存都在 `/tmp`。

---

## 8. 测试与评测

```bash
pytest -q tests/test_meme_info          # 62 passed
pytest -q tests                         # 全仓 300 passed
```

测试覆盖：

- 文本归一化、原文定位（找不到返回空，不编造）；
- 短词不误归并（上头/上课、出圈/出道、野生/野生狗奶）；
- 标题元话语/盘点抽取；不把《原神》《循环歌单》当梗；
- 2 条评论 + 2 个视频的独立使用徽章、同族合并；
- 热度单调性/时间衰减/单视频/常青惩罚/历史平滑；
- 端到端 JSON、离线 fixture、CLI exit code；
- LLM 坏 JSON/预算/未配置/证据白名单；
- 模块不 import `app`。

评测口径：

- `meme_info/labels/current_20261114.json` 是**银标**（来自梗百科/梗指南/盘点标题 + 人工核对），不是 ground truth；
- `meme_info/labels/blind_20261114_user.json` 是**用户 16 条盲标**（2026-11-14），冲突时**以用户盲标为准**；`扎啤/吴家村` 不得误杀；
- `eval` 输出 precision@k、current precision@k、new precision@k、labeled precision@k、nDCG@k、recall、evidence/freshness 率、Spearman/MAE 校准；P@k 分母固定为 k，预测不足 k 条时按缺位计，不抬高 shortfall 的精度；`labeled_*` 只对有标签的预测算，必须与标准 P@k 分栏报告；
- 单信号基线（meta_views / usage_docs / recency）在同一批候选上对比，避免只报绝对值；
- `stability --left a.json --right b.json --k 20`：比较两次同 `--now` 查询的 Top20（Jaccard/added/removed/order_changed/score_delta）；
- `blind --pred report.json --out labels.csv --n 100 --top 8`：生成盲标模板（只给 canonical + 评论/弹幕证据，隐藏分数/元话语）；用户填好后 `eval --labels labels.csv` 直接读 CSV。

---

## 9. 集成建议

1. app 层写 adapter：`Comment/Candidate/Meme` → 现有 `pipeline/store`；不要反向 import。
2. 采集阶段用 `collect_corpus` 产出的 `Corpus` 落对象存储/缓存，规则/打分可离线重放。
3. 发布门读 `HotMemeReport.memes` 与 `signals/evidence`；`status=weak/rejected` 不下发；证据不足 2 评论/2 视频的条目建议标「观察」而不是直接发布。
4. LLM 审核建议先 shadow，用 `verified` 统计通过率/一致性，再决定 `verify_gate`。
5. 历史平滑：把上一轮 `family_key → score` 写入状态文件，`--history` 传入；不要写进本模块。
6. 多平台扩展：实现新的 `Source`（同 `ranking/search/comments` 形状）即可，打分与发现层不变。
7. **旧 `app` 的 `list=new` 有结构性噪声**（rank=0 comment_backed 按 score 挤占前排）；本模块明确由 `only=publishable` 替代，见 [`REPLACE-NEW-LIST.md`](REPLACE-NEW-LIST.md)。工程师会话不改 `app/`，切换/下线由 PM 做。

---

## 10. 已知局限

- 只覆盖 B站；微博/抖音/小红书未实现。
- 文本可检测：图像梗、表情包梗、BGM/空耳梗不在范围内（`↑↓←→` 这类符号梗例外）。
- 谐音变体依赖种子/别名和字符相似度，不能自动发现同音不同字。
- 评论/弹幕是采样：证据深度不均，meta-only 条目仍可能出现；它们会标 `status=observing`、进入 `report.observing` 子集，`signals.evidence_rate_2x2` 明示，发布门不得当强证据。
- 2×2 徽章会让强证据发布列表出现 shortfall；模块查询不 top-up，也不因徽章误杀用户确认的 explainer-only 梗；需要更多强证据时扩评论/弹幕采样。
- 搜索接口会触发 `v_voucher`/风控；单条失败只记账，不影响整轮，但会降低个别梗的召回。
- 热度是「当前页面的可见热度」，不是全网真实热度；平台采样、推荐偏差都会影响。
- 银标评测存在循环性：标签来自同一搜索通道，precision 偏高；不要对外宣称 ground-truth 准确率。

---

## 11. 文件布局

```
meme_info/
  models.py      数据契约
  text.py        归一化/切段/相似度/原文定位
  knowledge.py   种子/别名/黑名单
  http.py        HTTP/限速/缓存
  bili.py        B站适配器与纯解析函数
  collect.py     多通道语料采集
  discover.py    元话语/使用候选、梗族归并
  heat.py        热度模型
  query.py       端到端查询与报告
  verify.py      可选 LLM 抽取/审核
  evaluate.py    评测指标
  cli.py         命令行
  labels/        银标评测集
  examples/      真机快照（截断）
  README.md
  REPORT.md
tools/
  meme_info.py   CLI 入口
tests/test_meme_info/
```
