# 热梗信息模块 · 返工复核版（v3，口径修正）

> 快照时间：2026-11-14 22:30（Asia/Shanghai）
> 返工依据：`docs/pm/MEME-INFO-REVIEW.md`（v3 §8）、`docs/pm/ENGINEER-CORRECTION.md`
> 用户盲标：`meme_info/labels/blind_20261114_user.json`（16 条）
> 代码：本仓库 `master`；模块 `meme_info/`；全仓测试 **300 passed**

---

## 1. 结论摘要

- **修正误杀**：`扎啤` 从 `REVIEW_STOPWORDS` 移除；`吴家村` 的 `村` 后缀只做审计标记，不再单独拦。PM 22:25 Top8（用户盲标 ✅）全部仍在当前 Top10。
- **4 条假阳性已被拦**：`最怕BLG夺冠之人`、`非凡之爱`、`腋毛攻击` 由 v4 LLM gate 判 `llm_not_meme`；`明码标价` 由普通短语规则过滤；`音乐` 由 lexicon 过滤。
- **16 条用户锚点（验收口径）**：22:30 gate raw **P@10 = 0.80、P@16 = 0.6875**；labeled P@10=1.00、labeled P@16=0.9167。raw 是验收数，**未达到 PM 的 P@10≥0.9**。
- **62 条补标**（60 条 AI/网页检索 + 用户 16 条锚点中的 62 行）对 22:30 gate：P@10 1.00、P@16 0.875；但它是 **AI+网页补标**，不是独立人工盲标，**不能当验收数**，只作为 shadow 前的补充回归。
- **稳定性**：同 `--now` 两次 gate 跑，Top20 Jaccard **0.8182**（18/20 相同）；PM 22:25 Top8 全部仍在 Top10；差异来自线上评论/搜索数据变化，不是排序翻转。纯同一 Corpus 打分（`score`）Top20 Jaccard = 1.0。
- **gate 漏网 2 条已修**：00:40 shadow 轮 40 条送审、18 条 rejected、`unverified_omitted=9`；`最怕BLG夺冠之人`、`腋毛攻击`（以及 `非凡之爱`）均为 `llm_not_meme`，Top10 7✅/0❌/3未标。
- **`list=new` 处置**：旧 app `list=new` 被 rank=0 的 comment_backed 噪声占满；本会话**不改 app/**，明确由模块 `only=publishable` 替代，见 [`REPLACE-NEW-LIST.md`](REPLACE-NEW-LIST.md)。
- **2×2 语义修正**：`evidence_rate_2x2` 是**证据质量徽章**，不是查询硬门槛；查询层不得因 explainer-only 误杀用户确认的梗，发布门再用它决定「强证据」。
- **判定：条件性通过，可做 shadow adapter，不能自动发布/替换旧榜**；用户需再复核 2–3 轮。
- 测试：`tests/test_meme_info` **62 passed**；全仓 **300 passed**。

---

## 2. 返工清单对照

| # | PM/用户要求 | 实现 | 验收 |
|---|---|---|---|
| 1 | `blocked_reason` 接入 `is_generic`；过滤普通词/成语；**不误杀扎啤/吴家村** | lexicon 读取修正（`词 词频` 格式）、`REVIEW_STOPWORDS={明码标价}`、proper 只审计 | `音乐`→lexicon、`明码标价`→common_phrase、`扎啤/吴家村` kept；audit 可查 |
| 2 | 4 条假阳性 reject；Top8 保留 | LLM prompt v4 重写判定顺序；`最怕/非凡/腋毛`→`llm_not_meme`；`明码标价/音乐` 规则过滤 | 规则版无 `音乐`；gate 版无 4 条假阳性；Top8 全在 |
| 3 | 盲标文件纳入 eval；同 `--now` 两次 diff | 16 条用户锚点 + 62 条补标均可 `--labels`；`stability` CLI | 16 锚点 raw P@10 .800/P@16 .6875；62 补标 1.0/.875（非纯盲标）；Top20 Jaccard .8182 |
| 4 | 单条 signals 输出 `evidence_rate_2x2` | `HeatSignals.evidence_docs/videos/rate_2x2` 写入 `meme.signals` | `/tmp/*.json` 每条可读；测试覆盖 |
| 5 | 报告区分银标 1.0 与用户盲标 0.8 | 本报告分节列两套口径；不再混用 | 见 §5/§6 |

**口径说明**：16 条用户锚点中 11 真/16，raw P@16 上限 0.6875；62 条补标的 0.875 来自 AI+网页补标，不是独立人工真值，不能拿来宣称验收通过。PM 的 P@10≥0.9/P@16≥0.8 需等用户扩充并复核纯人工盲标后再验。

---

## 3. 22:30 gate 版 Top16（用户盲标覆盖项标 ✅/❌/❓）

| # | 梗 | 分 | 状态 | 独立使用 | 梗视频 | LLM | 用户 |
|---:|---|---:|---|---:|---:|---:|---|
| 1 | 吃了不烧心 | 79.0 | hot | 30/5 | 2 | 72 | ✅ |
| 2 | 司凤音乐 | 71.4 | hot | 2/2 | 2 | 78 | ✅ |
| 3 | 闪身步 | 69.4 | hot | 30/5 | 2 | 62 | — |
| 4 | 扎啤 | 68.6 | hot | 3/3 | 1 | 62 | ✅ |
| 5 | 中国人能飞 | 63.7 | hot | 40/5 | 0 | 72 | ✅ |
| 6 | 灭建国 | 63.6 | hot | 1/1 | 2 | 72 | ✅ |
| 7 | 吴家村 | 60.2 | hot | 1/1 | 1 | 62 | ✅ |
| 8 | 心生爱慕 | 58.7 | hot | 1/1 | 1 | 42 | — |
| 9 | MJ宇宙 | 53.1 | hot | 0/0 | 1 | 62 | ✅ |
| 10 | 老实赚钱很枯燥吧 | 52.9 | hot | 0/0 | 1 | 72 | ✅ |
| 11 | 不小心惹了上帝 | 52.2 | hot | 1/1 | 1 | 35 | — |
| 12 | 老牧师 | 43.7 | rising | 2/2 | 0 | 42 | — |
| 13 | PiuPiuPiu | 42.6 | observing | 0/0 | 1 | 62 | ✅ |
| 14 | 狗尾草表情包 | 42.6 | rising | 0/0 | 1 | 62 | ✅ |
| 15 | 雪豹眉眉 | 41.2 | rising | 0/0 | 1 | 42 | ❓ |
| 16 | 没希望，佩不上 | 40.9 | rising | 0/0 | 1 | 22 | ✅ |

- PM 22:25 Top8（吃了/司凤/扎啤/老实/灭/中国人/吴家村/MJ）**全部在**当前 Top10（顺序因线上分数变化略有移动）。
- 4 条假阳性 `最怕BLG夺冠之人 / 非凡之爱 / 腋毛攻击 / 明码标价` 不在当前榜；`明码标价`、`音乐` 在规则层已被过滤。

### 3.1 4 条假阳性的落地方式

| 候选 | 规则层 | v4 gate | 最终 |
|---|---|---|---|
| 最怕BLG夺冠之人 | 放行（避免误杀赛事梗） | `is_meme=false`，heat 0，conf .30 | rejected |
| 非凡之爱 | 放行 | `is_meme=false`，识别为 AI MV 作品标题 | rejected |
| 腋毛攻击 | 放行 | `is_meme=false`，一次性描述 | rejected |
| 明码标价 | `common_phrase` | 不进入 gate | filtered |
| 音乐 | `lexicon`（修复词频格式后命中） | 不进入 gate | filtered |

### 3.2 00:40 gate 修复轮（shadow 同参数）

- 40 条送审、18 条 rejected、`unverified_omitted=9`；`limit=40`（limit20×2）避免「剔掉后未审项补位」。
- `最怕BLG夺冠之人`、`腋毛攻击`、`非凡之爱` → `llm_not_meme`；`明码标价` 规则层；`音乐` lexicon。
- Top10：7✅ / 0❌ / 3未标（`蓝色大肥鱼`、`doge`、`心生爱慕`）；`publishable=7/10`。
- 这说明 gate 漏网 2 条已修；仍需再跑 2 轮确认稳定。

### 3.3 2×2 证据徽章

`signals` 现在每条都有：

```json
{"evidence_docs": 1, "evidence_videos": 1, "evidence_rate_2x2": false}
```

- 只数 comment/danmaku，且**排除该梗自己的梗视频下的评论**；
- 用户盲标确认 `吴家村/灭建国/MJ宇宙/狗尾草/没希望` 等 explainer-only 也是真梗，因此它**不用于查询硬过滤**；
- 发布门/UI 可用它把条目分「强证据 / 观察」，但查询榜不 top-up、不硬杀。

---

## 4. 过滤审计（PM 返工 #6）

来自 22:30 gate 版 `stats.discovery_audit`：

| 名称 | 语料提及 | 过滤前候选 | 保留 | 原因 |
|---|---:|---|---|---|
| 音乐 | 53 篇 | 否（n-gram 未成候选） | 否 | `lexicon` |
| 扎啤 | 4 篇 | 是 | **是** | 用户盲标 ✅，不得误杀 |
| 明码标价 | 0 篇 | 否 | 否 | `not_present`（若出现则 `common_phrase`） |
| 吴家村 | 2 篇 | 是 | **是** | `proper_like=true` 仅审计，不拦 |

全量计数（同一轮）：

```
raw_candidates=2361, kept=2217
blocked_by_reason: numeric 35, lexicon 38, marketing 3, generic_word 10,
                   news_like 7, sensitive 1, short_ascii 45, no_evidence 5
```

结论：普通词/成语/新闻/数字/短 ASCII 被规则层拦；地名不再一刀切。

---

## 5. 评测：两种口径分开

### 5.1 盲标/补标（用户 16 锚点 + 60 条 AI 网页补标）

| 口径 | P@10 | labeled P@10 | P@16 | labeled P@16 | 覆盖率 | 验收 |
|---|---:|---:|---:|---:|---:|---|
| 16 条用户锚点（22:30 gate，验收口径） | **0.800** | **1.000** | **0.6875** | 0.9167 | 0.75 | **未达 raw P@10≥0.9** |
| 62 条补标（AI+网页+用户锚点） | 1.000 | 1.000 | 0.875 | 0.933 | 0.9375 | 只作 shadow 补充，**非验收** |

- 16 锚点 raw P@10=0.80：Top10 里 8 条有标签且全真，另 2 条（闪身步/心生爱慕）不在 16 条锚点内；`labeled P@10=1.0` 只算已标注的 8 条，**不能对外写 raw 1.0**。
- 16 锚点 P@16=0.6875：11 真/16，且 Top16 中 `老牧师` 也未标；`labeled P@16=0.9167`。
- 60 条模板漏了 `老实赚钱很枯燥吧`（用户 16 条锚点 ✅）；补入 `老实赚钱很枯燥吧` 与 `雪豹眉眉`（❓）后为 62 条；
- 62 条是「AI 网页检索 + 用户 16 条锚点」，不是纯盲标；62 条 P@10=1.0 不能当独立人工准确率。

### 5.2 银标（71 条）

| 快照 | P@10 | current P@10 | nDCG@10 | 备注 |
|---|---:|---:|---:|---|
| 22:30 gate | 1.00 | 1.00 | 0.978 | 只作回归，不作对外准确率 |
| 22:25 gate（PM） | 0.80 | 0.80 | — | 4 条假阳性在其中 |
| 71 条银标独立重跑（PM） | 0.70 | — | — | 与查询同通道，循环性，不作验收 |

银标来源与查询同通道，**不能与用户盲标混用**；本报告不把银标 1.0 当「准确率」，也不把 62 条 AI 补标当验收。

### 5.3 为什么这版能修掉 4 条假阳性

- 旧的 v2/v3 prompt 把「存在一个梗百科视频」当成强证据；
- v4 明确判定顺序：赛事/作品/AI MV/普通短语/一次性描述优先判 false；固定小曲/句式/表情包名、有专门梗视频或多条相关视频才判 true；
- 16 条盲标回归：**11/11 真梗保留、4/4 假阳性拒绝**；`雪豹眉眉` 被判 true（用户 ❓，保留 observing，不强制发布）。注意：这只证明 v4 gate 能拒 4 条假阳性，不等于 raw P@10 达标。

---

## 6. 稳定性

同一 `--now 2026-11-14T22:30:00+08:00`、同一 cache、同一 gate 参数连跑两次：

```
Jaccard(20) = 0.8182, common 18/20
added:   嘟嘟、司凤
removed: 不小心惹了上帝、吃瓜
PM 22:25 Top8: 全部仍在 Top10（顺序有 1–2 位移动）
```

- 差异来源：两次采集之间评论/搜索数据变化，Top20 尾部条目进出；不是排序随机翻转；
- 已加入确定性 tie-break（`family_key`、`bvid`、`title`、`comment_id`）与 `compare_reports`/`stability` 命令；
- **纯打分稳定性**：同一 Corpus 用 `score` 连跑两次，Top20 Jaccard = **1.0**（无网络/LLM 参与），说明排序本身确定。
- 结论：Top8 稳定；Top20 的 0.8182 属于「线上数据变化」，可解释，但离 PM 理想的 1.0 仍有差距；发布前建议用固定 Corpus 快照做回归。

---

## 7. `list=new` 噪声处置：明确由模块替代

同轮 app 临时库的 `list=new` Top10 被 `rank=0` 的 comment_backed 观察项占满：

```
ai 87.5 / 看完 86.3 / b站 85.8 / ge 85.5 / 这句 85.5 /
on 85.5 / 11 85.2 / 20 85.1 / 到了 85.1 / in 84.8
```

这些项不占主榜名次，但 `store.list_items(list=new)` 只按 `score DESC` 排、不过滤 rank=0，
把真正的新梗挤出 new Top10。**旧 `list=new` 不能作为切榜基线。**

决定：**由模块 `only=publishable` 替代**，不修改 `app/`（PM 负责组装/下线旧榜）：

- `GET /api/meme-info?only=publishable&limit=N`；
- 只含 `status∈(hot,rising)` + `evidence_rate_2x2=true` + LLM gate 通过（未审项不进 `memes`）；
- 00:40 gate 轮 `publishable=7/10`，Top10 7✅/0❌/3未标；
- 切榜前仍需 3 轮 shadow、用户复核 3 条未标、产品接受「只发 7–8 条/轮」。

详细字段映射与切榜门槛见 [`REPLACE-NEW-LIST.md`](REPLACE-NEW-LIST.md)。若 PM 要保留旧 pipeline，
则需改 `app/store.py::list_items` 排除 rank=0；本会话不改 `app/`，选择模块替代路径。

## 8. 成本与本轮数据

| 项 | 值 |
|---|---|
| 视频 | 943 |
| 评论 | 1,012（697 条 ≤7 天） |
| 弹幕 | 2,000 |
| 语料文档 | 4,688 |
| 本轮请求 | 158（warm cache；20 次命中） |
| 总耗时 | 183.5s（采集 121.4s + 打分/LLM） |
| LLM 送审 | 40 条，19 次真实调用（锚点/缓存复用），18 rejected，0 错误 |
| LLM token | 16,491 prompt + 2,777 completion = **19,268** |
| 估算成本 | 约 ¥0.02–0.03/轮（DeepSeek，仅参考） |

复现：

```bash
python -m tools.meme_info query \
  --out /tmp/mi_fix_gate.json \
  --limit 30 --comment-videos 45 --danmaku-videos 10 \
  --max-requests 330 --cache-dir /tmp/mi_cache \
  --expand-limit 20 --verify --verify-gate --verify-limit 40 \
  --now 2026-11-14T22:30:00+08:00 --quiet

python -m tools.meme_info eval --pred /tmp/mi_fix_gate.json \
  --labels meme_info/labels/blind_20261114_user.json --k 10

python -m tools.meme_info eval --pred /tmp/mi_check_gate_v4.json \
  --labels meme_info/labels/blind_20261114_user.json --k 10
```

（`mi_check_gate_v4.json` 是 PM 22:25 快照应用 v4 判定/剔除 4 条假阳性后的对照文件；仅用于同快照对比。）

---

## 9. 仍未解决 / 下一步

1. **验收口径仍不达标**：16 用户锚点 raw P@10=0.80（目标 ≥0.9）；62 条 AI 补标 1.0 不能替代人工盲标。需要用户复核 2–3 轮或补纯人工盲标；`腋毛攻击` 存在用户 ❌ vs 网页“热梗”冲突，继续按用户锚点 non_meme。
2. **Top20 稳定性 0.8182**：线上数据变化导致尾部进出；发布前用固定 Corpus 重放 + `--history` EMA，别直接拿单轮 Top20 做 A/B。
3. **发布门 / list=new**：只发 `status=hot/rising` 且 `evidence_rate_2x2=true`；meta-only 进 observe；旧 app `list=new` 必须下线或路由到 `only=publishable`，不要再用 rank=0 噪声榜做基线。`吴家村/灭建国/MJ宇宙/狗尾草/没希望` 这类用户确认但 2×2 不足的，应人工白名单/复核，不自动发。
4. **LLM 结论缓存 24h**：同候选不会频繁翻转；但新证据/新梗仍需人工抽检。
5. **多平台/多轮**：模块已把 Source 隔离；接微博/抖音时只实现 adapter，不改发现/打分层。

---

## 10. 文件

- [`meme_info/README.md`](README.md)：接口/CLI/模型；
- [`meme_info/REPLACE-NEW-LIST.md`](REPLACE-NEW-LIST.md)：old `list=new` 由模块 publishable 替代的契约；
- [`meme_info/labels/current_20261114.json`](labels/current_20261114.json)：银标（72 条，用户盲标优先修正）；
- [`meme_info/labels/blind_20261114_user.json`](labels/blind_20261114_user.json)：16 条用户盲标（权威）；
- [`meme_info/labels/blind_template_20261114.csv`](labels/blind_template_20261114.csv)：62 条补标（60 条 AI+网页检索稿 + 老实/雪豹 2 条用户锚点；隐藏分数/元话语）；
- [`meme_info/examples/report_20261114_rework_snapshot.json`](examples/report_20261114_rework_snapshot.json)：本轮 Top16 截断快照；
- [`meme_info/examples/eval_anchor16_20261114.json`](examples/eval_anchor16_20261114.json)：16 条用户锚点 raw P@10=.80 的评测原始输出；
- [`meme_info/examples/eval_blind_20261114.json`](examples/eval_blind_20261114.json)：62 条补标的补充评测原始输出；
- [`tools/meme_info.py`](../tools/meme_info.py)：`python -m tools.meme_info`。
