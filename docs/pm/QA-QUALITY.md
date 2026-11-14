# 榜单质量审计 · cycle#2

> 只读审计（2026-11-14）。所有 SQL 均使用 `sqlite3.connect("file:data/hotmeme.db?mode=ro", uri=True)`；未改代码/数据/测试，未联网、未起服务。
> 证据分级：**[已验证]** = 库内直接可查或跑过只读重算；**[假设]** = 内存重算/推断，缺字段或需更多轮数据。

## 0. 一句话结论

**当前 Top10 是「种子表 ∩ 热度榜」，不是「新热梗榜」**：1041/1055 个梗族被 `pipeline.py:324` 一处量纲错误（把 0–1 的梗性分与 45 比较）整族判死，只剩 14 条种子命中可打分；Top10 10/10 是种子词，其中 8 条是 2020–2023 的常青/泛化词，真梗（宽松口径）6/10，符合「新热梗」严格口径仅 2/10。

## 1. 数据口径与限制（先看这个）

| 项 | 值 | 证据 |
|---|---|---|
| 轮次 | cycle#1 14:08:10–14:09:16、cycle#2 14:10:18–14:11:01，间隔约 1 分钟 | `SELECT * FROM cycle` |
| 语料 | 377 视频 / 90 评论 / 16444 弹幕（弹幕缓存 50 条，下载全量 62730 条，匹配到 video 表 26 条 cid） | `cycle.stats_json`、`danmaku_cache` |
| 候选/梗族 | candidate 1502、family 1055 | `candidate`、`meme` |
| 可打分（非 rejected） | **14**，其中 hot 7 / observing 7 | `SELECT status,count(*) FROM meme_signal WHERE cycle_id=2` |
| first_seen | 只有 3 个不同值，全部是今天的两轮时间 | `SELECT count(distinct first_seen) FROM meme` |

**限制**（决定哪些结论不能下）：

1. 两轮相隔 1 分钟，**没有生命周期、没有 7 日增速**；`velocity` 实际是 `(count+1)/(past/7+1)`，`past` 来自同一小时的 `candidate_cycle`，≈0，所以 velocity 退化为「本轮频次」的代理。**[已验证]**
2. 库里 `first_seen` 是「入库时间」不是「梗首次出现时间」；今天刚建库，所有条目都是今天。因此「新晋/回锅/>14 天」类标签无法用库内数据验证。**[已验证]**
3. 我重跑 judge 时缺 `quoted / cohesion / entropy / df_search / is_line` 的真实值（未落库），用「弹幕整句 + 候选 df」重建，属于**下界**估计；文中标注 **[假设]**。
4. 弹幕缓存只覆盖 50 个 cid、能关联到 video 的 26 个；「跨视频数」我用 candidate.df_video（全量 377 视频口径）与弹幕缓存（26 视频口径）两个数并列，避免混淆。

## 2. cycle#2 Top10 逐条判定

判定方法：按 `docs/01-meme-definition.md` §2 五条判据（可复制性 / 模仿性扩散 / 语境依赖 / 语义增量 / 生命周期）逐条打分，再按 §3 的「不算」条款贴标签。证据列里 `df_video/df_zone` 来自 `candidate` 表，`弹幕视频/弹幕条数/分区` 来自 `danmaku_cache` 重建，`评论` 来自 `text_sample`。

| # | 条目 | 类型 | 五判据（✓/✗/?） | 证据（代表用法、跨视频、分区、首现、增速） | 标签 |
|---|---|---|---|---|---|
| 1 | 抽象 | 词 | 可复制✓ 扩散✓ 语境✓ 语义✓ 周期? | df_video=61/df_zone=7（全库第一）；弹幕 8 视频/27 条，8 个分区；代表「好抽象啊」「你是在玩抽象对吧？」；评论 5706 赞长句含「抽象化」；源 2023 | **真梗（已泛化/常青化）** |
| 2 | 咕咕嘎嘎 | 谐音 | 可复制✓ 扩散✓ 语境✓ 语义✓ 周期? | df_video=11/df_zone=4；弹幕 4 视频/8 条，4 分区；代表「咕咕嘎嘎」「！？咕咕嘎嘎？！」；首现本轮（cycle#1 无信号）；源 2025 空耳 | **真梗（萌芽/爆发期）** |
| 3 | 蚌埠住了 | 谐音 | 可复制✓ 扩散✓ 语境✓ 语义? 周期? | df_video=10/df_zone=5；弹幕 7 视频/17 条，7 分区；代表「蚌埠住了」×10；源 2021 | **真梗（已常青化）** |
| 4 | 哈基米 | 谐音 | 可复制✓ 扩散✓ 语境✓ 语义✓ 周期? | df_video=9/df_zone=3；弹幕 3 视频/3 条；代表「这跟哈基米有什么区别？」「？神人哈基米」；源 2024 猫空耳 | **真梗（泛化期）** |
| 5 | 嘉豪 | 词 | 可复制? 扩散✗ 语境✓ 语义? 周期? | df_video=6/df_zone=2；弹幕仅 2 视频/2 条：「约德尔嘉豪」「刚才我模仿嘉豪调声音」；无评论区用法；指向具体人（选手/UP） | **专有名词（人名）** |
| 6 | 整活 | 词 | 可复制✓ 扩散✓ 语境✗ 语义✓ 周期? | df_video=6/df_zone=3；弹幕 2 视频/2 条：「这个整活确实可以」「是在整活还是认真的」；源 2021 | **真梗（已泛化/常青化）** |
| 7 | 今夕是何年 | 句式 | 可复制✓ 扩散? 语境? 语义✓ 周期? | df_video=9/df_zone=**1**；弹幕缓存 **0 视频/0 条**、评论 0；唯一 sample 来自标题/简介；实为苏轼词句，被当「怀旧感叹」用 | **常青/诗句（疑似标题词，证据不足）** |
| 8 | 你礼貌吗 | 句式 | 可复制✓ 扩散✓ 语境✓ 语义✓ 周期? | df_video=19/df_zone=4；弹幕 3 视频/64 条，其中同一视频「怪兽：你礼貌吗」31+20 条；源 2022 综艺 | **真梗（已常青化）** |
| 9 | 麻了 | 词 | 可复制✓ 扩散✓ 语境✗ 语义✗ 周期? | df_video=6/df_zone=2；弹幕 4 视频/4 条但全是「酒麻了/赢麻了/闹麻了/头皮发麻」，无独立「麻了」；sample 即「闹麻了」；源 2021 情绪词 | **泛化情绪词** |
| 10 | 吃瓜 | 词 | 可复制✓ 扩散✗ 语境✗ 语义✗ 周期? | df_video=**4**/df_zone=2；弹幕 1 视频/2 条（「吃瓜吃到自己家」）；评论 0；源 2016 常青 | **常青词** |

**判定汇总**：真梗（含已泛化老梗）6/10；严格「新热梗」仅 2/10（咕咕嘎嘎、哈基米）；常青/泛词 2/10（吃瓜、麻了）；泛化老梗另 4 条（抽象、整活、蚌埠住了、你礼貌吗）；专名 1（嘉豪）；常青/诗句 1（今夕是何年）。

> 与 `BOARD.md:61` 的差异：看板写「吃瓜/麻了等常青词、抽象/整活等泛词」，本次逐条核后还需补 **嘉豪=人名、今夕是何年=标题词** 两类；且 `tests/golden/memes.json` 把「抽象」「整活」列为 positive，与 docs/01 §3「已泛化/常青不算新热梗」自相矛盾（见 §8.3）。

## 3. 量化指标

| 指标 | 值 | 算法/证据 |
|---|---|---|
| Top10 真梗率（宽松，docs/01 §2） | **60%（6/10）** | §2 表 |
| Top10 真梗率（严格，docs/01 §3 排除已泛化） | **20%（2/10）** | 只算咕咕嘎嘎、哈基米 |
| 常青 + 泛化词占比 | **60%（6/10，含已泛化）/ 20%（2/10，纯常青+纯泛词）** | 吃瓜、麻了 + 抽象、整活、蚌埠住了、你礼貌吗 |
| 新闻事件 / 营销 | **0/10** | Top10 无 news/marketing 命中 |
| 同族变体重复率 | **0/10（无重复）**，但 1 例**过合并** | 10 个 family_key 互不相同；「麻了」把「闹麻了/赢麻了」并进来（sample 即「闹麻了」），属异义合并 |
| 证据缺失率（sample 空） | **0/10**（Top10）；**4/4**（observing 4 条全空） | `meme_signal.sample` |
| 证据不符率（sample 与条目不符） | **2/10（20%）** | 麻了→「闹麻了」（异义变体）；今夕是何年→仅标题、无弹幕/评论使用 |
| 「新晋」标签显示率 | **10/10** | `index.html:134-138`：`first_seen` 距今 <36h |
| 「新晋」语义准确率 | **0/10**（若按「新出现的梗」）；2/10（若按 2024–2025 新梗） | 抽象 2023、吃瓜 2020、麻了 2021…；标签实际含义是「新入库」 |
| 黄金集召回（30 正例） | **端到端 16.7%（5/30）**；语料出现口径 **35.7%（5/14）**；无种子豁免口径 **21.4%（3/14）** | 见 §3.1 |
| 黄金集误入（30 负例） | **0%（0/30）** | 见 §3.2 |
| 非种子族被误杀 | **1040/1055 = 98.6%** | `pipeline.py:324` |
| judge 正向分可达上限（非种子） | **0.25**（重建），实测非种子最高 0.25 | 只有 breadth 0.15 + danmaku_repeat 0.10 |

### 3.1 黄金集 30 梗召回（逐条）

`tests/golden/memes.json` 30 个 positive，与库中候选对照：

| 状态 | 数量 | 条目 |
|---|---|---|
| 进入候选表 | **5** | 那咋了、抽象、整活、老六、爷青回 |
| 语料出现但未成候选 | **9** | 栓Q、yyds、绝绝子、科目三、包的、社死、干饭、凡尔赛、打工人 |
| 语料完全没出现 | **16** | 尊嘟假嘟、芭比Q、退退退、雪糕刺客、电子榨菜、显眼包、搭子、特种兵式旅游、挖呀挖、恐龙抗狼、city不city、已老实求放过、偷感、松弛感、上号、我勒个豆 |
| 被判为梗（seed 豁免或 judge≥0.45） | 5/5 候选 | 那咋了(0.35/seed)、抽象(0.60)、整活(0.50)、老六(0.60)、爷青回(0.43，靠 seed 才过) |

**结论**：召回瓶颈**不在判定，在候选发现**——14 个「语料里出现过」的正例，只有 5 个被 n-gram 抽成候选（14→5，损失 64%），剩下 9 个是低频/被通用词表过滤。**[已验证]**
端到端召回 5/30=16.7%，与 docs/02 §6「黄金集检查召回」的验收目标（未给具体数值，但 Top10 真梗率≥60%）明显不匹配。

### 3.2 黄金集 30 非梗误入

30 条负例**全部未进入 candidate/meme 表，误入率 0/30**。但这不能说明规则有效：
- 负例全是**长新闻句**（「原价129万支天价药进医保」等），`judge.py:138` 的 `len>12` 一票就压到 0；短负例（「真的吗」「这样啊」）也不在语料里。
- 黄金集**没有一类负例覆盖本项目实际误入类型**（常青词、泛情绪词、专有名词、标题词），所以「误入率 0%」是**测试集盲区**，不是质量证明。**[已验证]**

## 4. 归因：为什么是这 10 条

### 4.1 P0：`pipeline.py:324` 量纲错误（已定位）

```python
# app/pipeline.py:324
if not item.get("seed_hit") and item["components"].get("meme", 0.0) < config.OBSERVE_MIN:
    item["status"] = "rejected"; continue
```

- `components["meme"]` 是 `judger.judge()` 的 0–1 分；`config.OBSERVE_MIN = 45.0`。
- **0–1 永远 < 45** → 所有非种子族（1040/1055）在这一行被判 rejected，不进入打分展示。
- 结果：`meme_signal` 只有 14 条，**14/14 都是 seed_hit**；`families=1053, usable=14`（`cycle.stats_json`）。
- 同源 bug：`judge.py:152-157` 的 `decide()` 也拿 0–1 分比 55/45，且**全项目无人调用**（`grep -rn "decide(" app/` 仅定义处），是死代码，但说明阈值量纲从设计起就混用。

### 4.2 P0：judge 正向证据天花板 0.45 不可达

`judge.py:75-110` 正向 7 条，非种子最多能拿：

| 信号 | 值 | 非种子可得？ |
|---|---|---|
| seed_hit | +0.35 | ✗（就是非种子） |
| quoted | +0.10 | 少数（14 条里 2 条） |
| suffix | +0.15 | 少数 |
| breadth（df_video≥3 且 df_zone≥2） | +0.15 | ✓ 575/1055 |
| danmaku_repeat（整句+df≥3） | +0.10 | ✓ 390/1055 |
| cohesion/entropy | +0.08 | 需 ≥3 字且非种子，少量 |
| meme_source（df_search≥2） | +0.15 | 搜索视频标题命中，少量 |

**重建实测：非种子最高 0.25**（breadth 0.15 + danmaku 0.10），距离 0.45 差 0.20；即使把 quoted/cohesion/meme_source 全加上，上限 0.48，仍需 4 个信号同时命中。**0.45 的实际效果 = 必须命中种子。** **[假设：缺 quoted/cohesion/df_search 真值；但 14 条种子外无一 ≥0.45，方向确定]**

### 4.3 种子表吞掉全部排序方差

- `data/seed_memes.json` 实际 **148 条**（docs/02 §2 写 132，需同步）。
- 148 条里只有 **14 条**在本轮语料中出现（`candidate` 表命中），Top10 即这 14 条中的前 10。
- Top10 的 judge 分：seed_hit 10/10、breadth 9/10、meme_source 8/10、danmaku_repeat 3/10、quoted 2/10。**排序几乎完全由热度分决定**：`components.meme` 只占 `WEIGHTS["meme"]=0.12`，10 条里 meme 贡献 8%–17%。
- 「仅靠曝光分高」的定量检验（去掉 exposure 分项后重算）：只有 **3/10 仍 ≥55**（抽象 63.8、咕咕嘎嘎 56.5、蚌埠住了 55.1）；**7/10 跌破 55**（嘉豪 46.4、整活 45.7、今夕 44.8、麻了 41.1、吃瓜 38.1…）。

### 4.4 15 条负向规则的实际命中（实际只有 11 条）

`judge.py:113-146` 共 **11 条负向规则**（不是 15；docs/01 §6 列 9 条，代码与文档也不一致）。在 1055 族上重建命中：

| 规则 | 代码行 | 命中族数 | 备注 |
|---|---|---|---|
| sentence_fragment | 125 | **449 (42.6%)** | 最高频，靠句首/句尾虚词 |
| short_ascii | 144 | 67 | |
| proper_noun | 128 | 17 | 后缀「人/国/市…」，把「一个人/评论区」误伤 |
| numeric | 141 | 16 | 「20/66/888」 |
| news_like | 113 | 10 | 抽样均为「2026年/2018年」日期，未见机构名/新闻词命中 |
| evergreen | 135 | **7** | 三连/笑死/名场面/一键三连/投币/好家伙/绷不住了 |
| length | 138 | 5 | |
| marketing | 116 | **0** | 营销词表 27 个，一条没命中 |
| common_word | 119 | **0** | 候选发现阶段已过滤，双重保险全空 |
| lexicon | 122 | **0** | 唯一命中的「抽象」因 `and not seed` 被跳过 |
| owner_name | 132 | **0** | UP 主名与候选无交集 |

**漏网案例（为什么吃瓜/麻了/抽象/整活没被压住）**：

1. `data/blocklist.json` 的 `evergreen` 只有 22 个词，**不含**吃瓜、麻了、抽象、整活、蚌埠住了、赢麻了、闹麻了（逐词核过）；`common_words.txt`(621)、`lexicon.txt`(35035) 也都不含其中 6 个（只含「抽象」）。
2. `judge.py:122/125/128/135` 全部带 `and not seed` / `not seed`：**种子天然豁免所有负向规则**。所以「抽象」命中 lexicon 也不扣分，「爷青回」在 evergreen 也不扣分。
3. 种子 +0.35 又把分数托到 0.6–0.75，远高于非种子 0.25，形成「种子永远赢」。

### 4.5 展示/标签层

- `pipeline.py:340`：`display = [hot 或 observing][:10]` —— **observing 也进 Top10**，与 docs/02 §4「连续两轮 ≥55 才稳定上榜」矛盾。实测 `咕咕嘎嘎` 82.43 分、排第 2，状态却是 `observing`（因为它没有 cycle#1 信号，`hype_streak` 拿不到上一轮）。
- `CONFIRM_ROUNDS=2` 在 `pipeline.py:328` 实际传的是 `rounds=1`，**配置被硬编码覆盖**。
- `NEWCOMER_PERCENTILE=0.95` 被 `or item["seed_hit"]`（`pipeline.py:330-333`）旁路：哈基米曝光分位 0.769<0.95，靠 seed 直接 hot。
- 「新晋」=`first_seen < 36h`（`index.html:134-138`），不是 `is_new`；且 store 的 `top_items()` 根本不返回 `is_new`。今天建库 → **10/10 全标新晋**，语义为 0。
- `extract.py:478 sample_text()` 用**子串**匹配并跨 kind 取最短/最高赞：`麻了` 取到「闹麻了」，`今夕是何年` 取到标题。sample 不带来源类型，无法区分「弹幕使用」与「标题出现」。

## 5. 阈值敏感性（内存重算，**[假设]**）

方法：以 cycle#2 的 14 条 `meme_signal` 为池，用库内 `components`（分位、concentration、decay、previous）重算；权重扫描不重跑分位（分位固定），阈值扫描用现有分数。**注意：池只有 14 条，结论只说明「在现有候选池内」的弹性，不能外推到 1055 族。**

| 参数 | 现状 | 扫描 | Top10 变化 | 建议 |
|---|---|---|---|---|
| MIN_SCORE | 55 | 50 / 55 / 60 / 65 | 50 与 55 无变化；60 只把吃瓜降为 observing（**仍显示**，因为 display 含 observing）；65 后 hot 只剩 6 条 | **保持 55**；真正要改的是 display 只取 hot |
| OBSERVE_MIN | 45 | 45 / 50 | 只影响 `那咋了`(49.1) 是否留观察池，Top10 不变 | 与 judge 量纲解耦后改为 judge 阈值 0.45 |
| NEWCOMER_PERCENTILE | 0.95 | 0.85 / 0.90 / 0.95 | **0 变化**：新晋路径已被 seed_hit 旁路 | 修掉 seed 旁路后再谈；本轮数据无法验证 |
| CONFIRM_ROUNDS | 2 | 1 / 2 / 3 | 代码实际用 1；改 2 会把 6 条「仅上轮有分」的降级，Top10 空位由咕咕嘎嘎/嘉豪/今夕/那咋了补 | 先把 `rounds=1` 改成 `rounds=config.CONFIRM_ROUNDS` |
| WEIGHTS | .30/.22/.18/.18/.12 | meme→0.20；breadth→0.28；velocity→0.06 | 池内仅 1–3 个名次互换（如 breadth+ 时你礼貌吗升到第 5、整活降到第 6），**集合不变** | meme 0.12→0.20、exposure 0.30→0.24；但单靠权重救不了，先修 gate/词表 |

**关键结论**：在当前数据下，调权重/阈值**不会**换掉 Top10 的任何一条——因为 1041 条非种子根本没有分。**要先修 `pipeline.py:324` 与 judge 天花板，权重才有意义。**

## 6. 根因假设（映射到代码/数据）

| # | 根因 | 位置 | 类型 | 影响 |
|---|---|---|---|---|
| R1 | judge 0–1 分与 45 阈值比较，非种子全判 rejected | `app/pipeline.py:324`（同源死代码 `app/judge.py:152-157`） | **已验证** | 1040/1055 族被杀，usable=14，Top10 全种子 |
| R2 | 正向证据上限 0.45 不可达，0.45 实际=种子豁免 | `app/judge.py:75-110` + `:154` | 已验证（非种子最高 0.25） | 无种子先验的新梗永远进不来 |
| R3 | 种子表 148 条、Top10 10/10 种子，且年份 2020–2025 混杂 | `data/seed_memes.json` | 已验证 | 榜单=老梗榜；docs 写 132 条不一致 |
| R4 | 种子豁免所有负向规则 | `app/judge.py:122,125,128,135` 的 `not seed` | 已验证 | 抽象/爷青回/蚌埠住了 不受 lexicon/evergreen 约束 |
| R5 | 词表缺口：吃瓜/麻了/整活/蚌埠住了 不在 blocklist/common/lexicon | `data/blocklist.json`、`data/common_words.txt`、`data/lexicon.txt` | 已验证 | 常青/泛词无任何抑制 |
| R6 | observing 也进 Top10，CONFIRM_ROUNDS 被硬编码 1 | `app/pipeline.py:328,340` | 已验证 | 咕咕嘎嘎 82 分却标 observing；稳定性口径失效 |
| R7 | 「新晋」=入库 36h，非梗的新鲜度；API 不返回 is_new | `app/static/index.html:134-138`、`app/store.py:390-426` | 已验证 | 10/10 误标新晋 |
| R8 | sample 子串匹配、不分来源；标题词被当使用证据 | `app/extract.py:478-487` | 已验证 | 麻了→闹麻了；今夕是何年 0 弹幕却有 sample |
| R9 | `_similar` 包含规则把异义短词并族（闹麻了/赢麻了→麻了） | `app/extract.py:359-370` | 已验证（members 表 + sample） | 泛词进一步变大，无法区分语义 |
| R10 | 候选发现过滤掉 9/14 黄金正例 | `app/extract.py:146-330`、`data/common_words.txt` | 已验证 | 端到端召回 16.7% |
| R11 | 弹幕解压按 zlib(-15) 写文档，实际缓存是标准 zlib（78 9c） | `docs/02` §7 vs `danmaku_cache.payload` | 已验证 | 文档误导；代码另有回退 |
| R12 | 搜索源（df_search）对非种子几乎不可用 | `app/config.py:46-52`、`app/pipeline.py:307` | 假设 | 非种子少 0.15 正向分 |

## 7. 可执行的改法（按收益/风险排序）

> 只给建议，不改代码。每条含预期收益与副作用。

### P0-1 修 gate 量纲（`app/pipeline.py:324`）

```python
JUDGE_MIN = 0.45  # app/config.py，0-1 量纲
if not item.get("seed_hit") and item["components"].get("meme", 0.0) < config.JUDGE_MIN:
    item["status"] = "rejected"; continue
```

- 预期：非种子不再一刀切；结合 §7 P0-2 后 Top10 有望换掉 2–4 条（吃瓜/麻了/嘉豪/今夕）。
- 副作用：judge 天花板 0.25，**单独修这条只会让 usable 从 14 变成约 0–12，Top10 可能空位**，必须同时做 P0-2。

### P0-2 给非种子一条可达的正向路径（`app/judge.py:92-110`）

- 方案 A（推荐）：`breadth` 条件从 `df_video>=3 且 df_zone>=2` 放宽为 `df_video>=2 且 df_zone>=1` 给 0.10；新增「评论/弹幕跨 2 视频整句重复」+0.10；把 `JUDGE_MIN` 降到 **0.35**。
- 方案 B：`quoted` 从 0.10 提到 0.20（引号是强梗信号），`cohesion` 0.08→0.12。
- 预期：非种子可达 0.35–0.45，Top10 出现 2–5 条非种子。
- 副作用：泛词/专名会一起进来（390 族有 danmaku_repeat），必须同时上 P0-3 词表。

### P0-3 补词表（`data/blocklist.json` + `data/common_words.txt`）

| 词 | 应入 | 建议权重 |
|---|---|---|
| 吃瓜、今夕是何年 | evergreen | −0.10 |
| 麻了、赢了、笑死、无语、舒服了、好家伙 | evergreen（泛情绪） | −0.10 |
| 抽象、整活、蚌埠住了、你礼貌吗 | 新增 `generalized`（已泛化老梗） | −0.15，且**不豁免种子** |
| 嘉豪、迪迦、卡卡罗、遐蝶 | 新增 `proper_noun` 词表 | −0.20 |
| 闹麻了、赢麻了 | 从「麻了」族拆出，或 alias_map 显式标注 `variant_of` | 防止异义合并 |

- 预期：Top10 直接去掉吃瓜/麻了/嘉豪/今夕 4 条，真梗率 60%→约 80%（宽松口径）。
- 副作用：需要人工维护；「抽象/整活」在黄金集里是 positive，需先统一口径（见 §8.3），否则测试会红。

### P0-4 去掉种子的负向豁免（`app/judge.py:122,125,128,135`）

- 把 `and not seed` 改成「种子只豁免 length/numeric/short_ascii，不豁免 evergreen/lexicon/proper_noun」。
- 预期：爷青回（现在 32.8，靠 seed 留在观察池）、蚌埠住了 等会被压；配合 P0-3 效果叠加。
- 副作用：种子表里确实有梗（如尊嘟假嘟）若同时进 evergreen 会误伤，需人工核 148 条。

### P1-1 修展示与稳定性口径（`app/pipeline.py:328,340`）

- `rounds=config.CONFIRM_ROUNDS`；display 只取 `status=="hot"`，observing 放独立「观察」区。
- 预期：Top10 不再出现 82 分却标 observing 的条目；「连续两轮」真正生效。
- 副作用：冷启动首轮 Top10 可能不足 10 条（需要 empty/降级态，已有兜底）。

### P1-2 修「新晋」（`app/static/index.html:134` + `app/store.py:390`）

- API 返回 `is_new`（家族是否本轮新建），前端用它；或把标签改成「入库 <36h」并注明。
- 预期：新晋准确率从 0/10 到可用（哈基米本轮新建 = 真新晋）。
- 副作用：`is_new` 依赖 `meme.first_seen`，需在 `upsert_memes` 前算好；本轮只有 1 条新建，样本太少。

### P1-3 修 sample 来源（`app/extract.py:478`）

- 记录 `sample_kind`（comment/danmaku/title），优先 comment/danmaku；标题只作 fallback 并标注「标题」。
- 预期：证据缺失率从 2/10 降到 0；「今夕是何年」会被标为「仅标题」而非代表用法。
- 副作用：标题命中的条目可能 sample 为空 → 需要前端显示「暂无使用证据」。

### P1-4 修弹幕解压文档（`docs/02` §7）

- 实测 `danmaku_cache.payload` 以 `78 9c` 开头，是标准 zlib；文档写 `zlib.decompress(data, -15)` 与代码回退链不一致。建议文档改为「优先标准 zlib，raw-deflate 回退」。
- 副作用：无（纯文档）。

## 8. 需要更多数据才能下的结论

1. **生命周期判据**：只有 2 轮、间隔 1 分钟，`first_seen` 全是今天。要判断「兴起—峰值—衰退」至少需要 3–7 天、每天 1 轮的数据。**[假设]**
2. **真·首现时间**：当前 `meme.first_seen` 是入库时间，不是梗首次出现时间；需要接搜索/历史语料或用视频 `pubdate` 回溯，否则「新晋/回锅/老梗」都不可信。
3. **非种子能否进 Top10**：我只能在 14 条池内做敏感性；1041 条非种子没有 score，无法回答「修 gate 后谁会上榜」。需要在临时 DB 上离线重跑一轮（`HOTMEME_OFFLINE=1` + `HOTMEME_DB=/tmp/...`，不碰生产库）验证。**[假设]**
4. **阈值最优值**：样本 14 条，MIN_SCORE 55/60 的差异只有 1 条；至少 200+ 条可用候选、3 轮以上才能拟合。
5. **嘉豪/今夕是何年**：需要评论区/搜索语境确认是「人名」还是「校园梗」；当前只有 2 条弹幕，证据不足。
6. **搜索源 df_search**：未落库，无法量化 `meme_source` 对非种子的实际贡献；建议把 `df_search` 写进 `components`。

## 9. 附：复现命令

```bash
# 只读连接（Python；sqlite3 CLI 在本机打开生产库报 CANTOPEN，改用 Python）
python3 - <<'PY'
import sqlite3
c=sqlite3.connect("file:data/hotmeme.db?mode=ro", uri=True)
for r in c.execute("select s.rank,s.family_key,s.score,s.status,s.components_json from meme_signal s where s.cycle_id=2 order by s.score desc"):
    print(r[0],r[1],r[2],r[3])
PY

# 状态分布
sqlite3 -readonly data/hotmeme.db "select status,count(*) from meme_signal where cycle_id=2 group by status"
# 输出：hot 7 / observing 7

# 非种子被误杀的证据（等价逻辑）
python3 - <<'PY'
import sqlite3,json
c=sqlite3.connect("file:data/hotmeme.db?mode=ro", uri=True)
rows=list(c.execute("select s.family_key,s.components_json from meme_signal s where s.cycle_id=2"))
print(sum(1 for k,j in rows if json.loads(j).get("meme",0)<45), "of", len(rows), "usable have meme<45")
print("non-seed families:", c.execute("select count(*) from meme").fetchone()[0]-15)
PY

# 黄金集
python3 -c "import json;d=json.load(open('tests/golden/memes.json'));print(len(d['positives']),len(d['negatives']))"
```

---

**审计人**：qa-quality（会话 `ecccf328-ee07-4089-86be-d024c6c56295`）
**结论摘要**：真梗率 60%（宽松）/ 20%（严格）；Top10 全种子、7/10 靠曝光；最该改的三处：`pipeline.py:324` 量纲、`judge.py` 正向天花板/种子豁免、`blocklist.json`+种子表的常青/泛词缺口。
