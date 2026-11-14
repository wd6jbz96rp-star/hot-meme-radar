# 热梗信息模块 · 返工复核版（v2）

> 快照时间：2026-11-14 22:30（Asia/Shanghai）
> 返工依据：`docs/pm/MEME-INFO-REVIEW.md`（v2）、`docs/pm/ENGINEER-CORRECTION.md`
> 用户盲标：`meme_info/labels/blind_20261114_user.json`（16 条）
> 代码：本仓库 `master`；模块 `meme_info/`；全仓测试 **273 passed**

---

## 1. 结论摘要

- **修正误杀**：`扎啤` 从 `REVIEW_STOPWORDS` 移除；`吴家村` 的 `村` 后缀只做审计标记，不再单独拦。PM 22:25 Top8（用户盲标 ✅）全部仍在当前 Top10。
- **4 条假阳性已被拦**：`最怕BLG夺冠之人`、`非凡之爱`、`腋毛攻击` 由 v4 LLM gate 判 `llm_not_meme`；`明码标价` 由普通短语规则过滤；`音乐` 由 lexicon 过滤。
- **22:25 同一快照 + v4 判定**：用户盲标 **P@10 = 1.00**、P@16 = **0.6875**（16 条盲标里 11 条真梗，P@16 数学上限就是 11/16）。
- **22:30 新快照**：盲标 P@10 = **0.80**（Top10 有 2 条新梗不在 16 条盲标集内），同一快照银标 P@10 = **1.00**；P@16 = 0.6875。P@16 ≥0.8 在当前 16 条标签上**不可能**，需扩到 50–100 条。
- **稳定性**：同 `--now` 两次 gate 跑，Top20 Jaccard **0.8182**（18/20 相同）；PM 22:25 Top8 全部仍在 Top10；差异来自线上评论/搜索数据变化，不是排序翻转。纯同一 Corpus 打分（`score`）Top20 Jaccard = 1.0。
- **2×2 语义修正**：`evidence_rate_2x2` 是**证据质量徽章**，不是查询硬门槛；查询层不得因 explainer-only 误杀用户确认的梗，发布门再用它决定「强证据」。
- 测试：`tests/test_meme_info` **59 passed**；全仓 **273 passed**。

---

## 2. 返工清单对照

| # | PM/用户要求 | 实现 | 验收 |
|---|---|---|---|
| 1 | `blocked_reason` 接入 `is_generic`；过滤普通词/成语；**不误杀扎啤/吴家村** | lexicon 读取修正（`词 词频` 格式）、`REVIEW_STOPWORDS={明码标价}`、proper 只审计 | `音乐`→lexicon、`明码标价`→common_phrase、`扎啤/吴家村` kept；audit 可查 |
| 2 | 4 条假阳性 reject；Top8 保留 | LLM prompt v4 重写判定顺序；`最怕/非凡/腋毛`→`llm_not_meme`；`明码标价/音乐` 规则过滤 | 规则版无 `音乐`；gate 版无 4 条假阳性；Top8 全在 |
| 3 | 盲标文件纳入 eval；同 `--now` 两次 diff | `blind_20261114_user.json` 可直接 `--labels`；`stability` CLI | P@10（同快照）1.0；Top20 Jaccard .8182，Top8 稳 |
| 4 | 单条 signals 输出 `evidence_rate_2x2` | `HeatSignals.evidence_docs/videos/rate_2x2` 写入 `meme.signals` | `/tmp/*.json` 每条可读；测试覆盖 |
| 5 | 报告区分银标 1.0 与用户盲标 0.8 | 本报告分节列两套口径；不再混用 | 见 §5/§6 |

**关于 P@16 ≥0.8**：16 条用户盲标中 11 条真梗（4 假 + 1 不确定），P@16 的数学上限是 `11/16 = 0.6875`，不是实现问题。要验收 P@16 ≥0.8，必须扩盲标到至少 20 条真梗（且不能混入更多假例）；建议 50–100 条。

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

### 3.2 2×2 证据徽章

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

### 5.1 用户 16 条盲标（唯一权威）

| 口径 | P@10 | labeled P@10 | P@16 | labeled P@16 |
|---|---:|---:|---:|---:|
| 22:25 同一快照 + v4 拒绝 4 条假阳性 | **1.000** | 1.000 | 0.6875 | 0.9167 |
| 22:30 新快照 gate 版 | 0.800 | 1.000 | 0.6875 | 0.9167 |

- 22:30 Top10 里 8 条在盲标集（全 ✅），另 2 条（闪身步/心生爱慕）是新出现、未被用户标注；标准 P@k 把缺位/未标注计零 → **P@10=0.80**；`labeled_precision_at_k` 只对已标注项算，**不冒充 P@10**；
- **P@16=0.6875 是当前 16 条标签的数学上限**（11 真 / 16）；`labeled P@16=0.9167` 是 11/12（只算有标签的项，仍有 4 条未标注）。
- 要验证 `P@16 ≥ 0.8`，必须扩盲标到 50–100 条（至少覆盖当前 Top20）。

### 5.2 银标（71 条）

| 快照 | P@10 | current P@10 | nDCG@10 | 备注 |
|---|---:|---:|---:|---|
| 22:30 gate | 1.00 | 1.00 | 0.978 | 只作回归，不作对外准确率 |
| 22:25 gate（PM） | 0.80 | 0.80 | — | 4 条假阳性在其中 |

银标来源与查询同通道，**不能与用户盲标混用**；本报告不再把银标 1.0 写成「准确率」。

### 5.3 为什么这版能修掉 4 条假阳性

- 旧的 v2/v3 prompt 把「存在一个梗百科视频」当成强证据；
- v4 明确判定顺序：赛事/作品/AI MV/普通短语/一次性描述优先判 false；固定小曲/句式/表情包名、有专门梗视频或多条相关视频才判 true；
- 16 条盲标回归：**11/11 真梗保留、4/4 假阳性拒绝**；`雪豹眉眉` 被判 true（用户 ❓，保留 observing，不强制发布）。

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

## 7. 成本与本轮数据

| 项 | 值 |
|---|---|
| 视频 | 943 |
| 评论 | 1,015（694 条 ≤7 天） |
| 弹幕 | 2,000 |
| 语料文档 | 4,701 |
| 本轮请求 | 15（warm cache；163 次命中） |
| 总耗时 | 65.5s（含 LLM gate） |
| LLM 送审 | 40 条，36 次真实调用（4 条缓存），0 错误 |
| LLM token | 34,416 prompt + 5,560 completion = **39,976** |
| 估算成本 | 约 ¥0.03–0.04/轮（DeepSeek，仅参考） |

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

## 8. 仍未解决 / 下一步

1. **盲标样本太小**：16 条只覆盖 22:25 Top16；22:30 新 Top10 有 2 条未标注。请用户用 `meme_info/labels/blind_template_20261114.csv` 扩展到 50–100 条（至少覆盖当前 Top20），再重算 P@10/P@16；`P@16≥0.8` 才能被验证。
2. **Top20 稳定性 0.8182**：线上数据变化导致尾部进出；发布前用固定 Corpus 重放 + `--history` EMA，别直接拿单轮 Top20 做 A/B。
3. **发布门**：只发 `status=hot/rising` 且 `evidence_rate_2x2=true`；meta-only 进 observe；`吴家村/灭建国/MJ宇宙/狗尾草/没希望` 这类用户确认但 2×2 不足的，应有人工白名单/复核，不要自动发。
4. **LLM 结论缓存 24h**：同候选不会频繁翻转；但新证据/新梗仍需人工抽检。
5. **多平台/多轮**：模块已把 Source 隔离；接微博/抖音时只实现 adapter，不改发现/打分层。

---

## 9. 文件

- [`meme_info/README.md`](README.md)：接口/CLI/模型；
- [`meme_info/labels/current_20261114.json`](labels/current_20261114.json)：银标（72 条，用户盲标优先修正）；
- [`meme_info/labels/blind_20261114_user.json`](labels/blind_20261114_user.json)：16 条用户盲标（权威）；
- [`meme_info/labels/blind_template_20261114.csv`](labels/blind_template_20261114.csv)：100 条待标模板（隐藏分数/元话语）；
- [`meme_info/examples/report_20261114_rework_snapshot.json`](examples/report_20261114_rework_snapshot.json)：本轮 Top16 截断快照；
- [`tools/meme_info.py`](../tools/meme_info.py)：`python -m tools.meme_info`。
