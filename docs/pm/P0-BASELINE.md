# P0 基线 · cycle#7（2026-11-14 只读快照）

> 会话：eval-harness（`ee2f1dc8-eb21-4abe-92cb-f5dc33509fe3`），委派方 lead。
> 口径：`docs/pm/EVAL-DESIGN.md`。数据库以 `file:data/hotmeme.db?mode=ro` 只读打开；未联网、未启动服务、未写生产库、未改 `app/`/`tests/`/`data/`。
> 生成命令：`.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7`（`generated_at=2026-11-14T16:17:07+08:00`，`as_of=2026-11-14`）。
> **这不是最终真值**：标签是单人判断（A/B/C 置信度），只用于 v2 前后对比；生产库仍在每 10 分钟采集，累计表数字会漂移，对比时必须 pin `--cycle 7`。

## 0. 基线结论（TL;DR）

| 指标 | 值 | 说明 |
|---|---|---|
| **seed 命中率** | **10/10 = 100%** | `components.reasons.seed_hit=0.35` 与种子表 canonical/alias 两条口径一致，10/10 |
| **常青占比** | **6/10 = 60%**（含 2 条 pure + 4 条 generalized） | 抽象、蚌埠住了、你礼貌吗、整活（已泛化）+ 吃瓜、麻了（纯常青/泛词） |
| **平均 origin 年龄** | **3.87 年**（7/1 中点口径）；名义年差 **3.5 年** | Top10 的 T0 年份均值 2022.5；最早 2020、最晚 2025 |
| 严格 `new_meme`（≤30 天） | **0/10** | 最新只到 2025 年；按 EVAL-DESIGN R1/R3，无一可证 ≤30 天 |
| 近两年新梗（`recent_meme`） | **2/10** | 咕咕嘎嘎（2025）、哈基米（2024） |
| 专名 / 新闻 / 营销 | **1 / 0 / 0** | 嘉豪=专名；Top10 无新闻/营销 |
| 非种子占比 | **0/10 = 0%** | 新梗曝光为 0；非种子可用 0/1032 族 |
| 同族重复 | **0** | 10 个 family_key 互不相同；Top10 内无别名互指 |
| 黄金集召回（进候选） | **5/30 = 16.7%** | 30 个正例 **30/30 都在种子表**，瓶颈在候选发现，不在词典 |

**一句话**：cycle#7 的 Top10 仍是「种子表 ∩ 热度榜」——10/10 种子、6/10 常青、平均 3.87 岁、严格新梗 0 条。与 QA-QUALITY 的 cycle#2 相比，**集合与核心比例完全一致，只有名次互换**；这为 v2「评论区优先」提供了可复现的前置基线。

## 1. 数据口径与限制

| 项 | 值 | 证据 |
|---|---|---|
| 轮次 | cycle#7 · 2026-11-14T14:53:33+08:00 → 14:53:51，`status=done` | `cycle` |
| 语料 | 378 视频 / 90 评论 / 16,444 弹幕；31 请求 / 18.3s / 0 错误 | `cycle.stats_json` |
| 候选/梗族 | candidates=1500、families=1046、usable=14 | `cycle.stats_json` |
| 可用信号 | 14 条，**14/14 种子**；其中 hot 9、observing 5（含 rank10 的吃瓜） | `meme_signal` cycle=7 |
| 观测史 | cycle#1 14:08 起；cycle#7 时仅 45 分钟；写报告时库内已到 cycle#15（约 2 小时） | `cycle` |
| `first_seen` | 全部为 2026-11-14（写库时间），**不是梗首现** | `meme` |
| 表格快照 | 运行时刻 `meme` 1097 行、`candidate` 1565 行；会随采集增长 | 指标脚本 `candidate_pool` |

**限制（决定哪些结论不能下）**：

1. 没有 7 天增速/生命周期：`rising_old`、真正的「新晋/回锅」**[不可测]**；`velocity` 只是本轮频次的代理。
2. `origin` 只有年份，30 天新梗判定缺日级证据；`new_meme` 严格口径只能记 0。
3. `sample` 无来源类型且子串匹配：`抽象`→「抽象化」、`麻了`→「闹麻了」、`今夕是何年`→与标题同形，均为**证据瑕疵**（见 §3）。
4. 未重跑 judge：本基线只读已落库字段，不重建 `quoted/cohesion/df_search`；与 QA-QUALITY 的重建口径不可逐位比较。

## 2. 种子表审计（`data/seed_memes.json`，148 条）

### 2.1 按 origin 年份分布

| 年份 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 未知 | 合计 |
|---|---|---|---|---|---|---|---|---|
| 条数 | 26 | 31 | 24 | 27 | 23 | 11 | 1 | **148** |
| 占比 | 17.6% | 20.9% | 16.2% | 18.2% | 15.5% | 7.4% | 0.7% | 100% |

区间外年份单列（不硬塞进 2020–2025 桶）：**2018=1、2019=3、2026=1**。三者为 `extras_outside_2020_2025`，计入总数但不计入上表。

| 分组 | 规则 | 条数 | 占比 |
|---|---|---|---|
| classic | origin ≤2023 | **112** | 75.7% |
| recent | origin ≥2024 | **35** | 23.6% |
| 未知 | 无年份（`刘德华解冻`：`春节 年度梗`） | **1** | 0.7% |

> 校验：112 + 35 + 1 = 148。类型分布：词 82、句式 36、谐音 17、名场面 11、缩写 2。
> 与文档差异：`docs/02` §2 写 148（一致）；`BOARD.md:97` 记录「种子 148 vs docs 写 132」是历史偏差，现行文件为 148。

### 2.2 Top10 对应种子的 origin

| # | canonical | origin（种子） | 年份 | 分组 |
|---|---|---|---|---|
| 1 | 抽象 | `2023 离谱好笑` | 2023 | classic |
| 2 | 咕咕嘎嘎 | `2025 空耳` | 2025 | recent |
| 3 | 蚌埠住了 | `2021 绷不住` | 2021 | classic |
| 4 | 哈基米 | `2024 猫咪空耳` | 2024 | recent |
| 5 | 嘉豪 | `2025 校园梗` | 2025 | recent |
| 6 | 今夕是何年 | `2023 怀旧` | 2023 | classic |
| 7 | 你礼貌吗 | `2022 反问` | 2022 | classic |
| 8 | 麻了 | `2021 麻木` | 2021 | classic |
| 9 | 整活 | `2021 整蛊表演` | 2021 | classic |
| 10 | 吃瓜 | `2020 围观` | 2020 | classic |

Top10 origin 分布：**2020=1、2021=3、2022=1、2023=2、2024=1、2025=2**；classic(≤2023)=7、recent(2024+)=3。**Top10 里最新的是 2025 年，没有 2026 年新梗。**

> 注：`咕咕嘎嘎`、`嘉豪`、`今夕是何年` 的 `meme.origin` 为空，年份取自种子表；其余 7 条两边一致。这 3 条是 B 级证据的直接来源。

## 3. Top10 逐条判定（cycle#7）

**判定方法**：按 `docs/01-meme-definition.md` §2 五判据与 §3 排除项，结合 EVAL-DESIGN §3 标签规则；证据列取 `meme_signal.sample`、`components_json.reasons`、`candidate.df_video/df_zone/total_count`。`seed_hit` 一律为 `0.35`（10/10），下表不再重复分值。

| # | canonical | score | seed_hit | origin | 标签 | 证据（sample / 组分 / 广度） | 置信 |
|---|---|---|---|---|---|---|---|
| 1 | 抽象 | 89.31 | Y | 2023 | `evergreen`（generalized） | sample 长评论含「…抽象化…」；reasons `seed .35 + breadth .15 + meme_source .12`；df_video=61/df_zone=7（全库第一）；meme=0.62 | A |
| 2 | 咕咕嘎嘎 | 81.10 | Y | 2025 | `recent_meme` | sample 独立整句「咕咕嘎嘎」；`seed + breadth + danmaku_repeat .10 + meme_source`；df 11/4；meme=0.72 | B |
| 3 | 蚌埠住了 | 77.32 | Y | 2021 | `evergreen`（generalized） | sample 独立整句；`seed + breadth + danmaku_repeat + meme_source`；df 10/5；meme=0.72 | A |
| 4 | 哈基米 | 72.57 | Y | 2024 | `recent_meme` | sample「？神人哈基米」（含词）；`seed + breadth + meme_source`；df 9/3；meme=0.62 | B |
| 5 | 嘉豪 | 68.16 | Y | 2025 | `proper_noun` | sample「不如嘉豪」；`seed + quoted .10 + breadth + meme_source`；df 6/2；meme=0.72；seed 自称「校园梗」，指向具体人 | B |
| 6 | 今夕是何年 | 65.32 | Y | 2023 | `unclear` | sample 与 canonical 同形（疑标题）；`seed + breadth_mid .10 + danmaku_repeat + meme_source + evergreen −.05`；df 9/**1**；meme=0.62 | C |
| 7 | 你礼貌吗 | 64.58 | Y | 2022 | `evergreen`（generalized） | sample「怪兽：你礼貌吗」；`seed + breadth + danmaku_repeat`；df 19/4；meme=0.60 | A |
| 8 | 麻了 | 59.74 | Y | 2021 | `evergreen`（pure） | sample「闹麻了」（子串/异义，证据瑕疵）；`seed + breadth + meme_source + evergreen −.05`；df 6/2；meme=0.57 | A |
| 9 | 整活 | 55.55 | Y | 2021 | `evergreen`（generalized） | sample「这个整活确实可以」；`seed + breadth + meme_source`；df 5/3；velocity=**0.001**（无回锅增速） | A |
| 10 | 吃瓜 | 53.32 | Y | 2020 | `evergreen`（pure） | sample「吃瓜吃到自己家」；`seed + quoted + breadth + meme_source + evergreen −.05`；df 4/2；meme=0.67；**status=observing 却带 rank10 进榜** | A |

### 3.1 标签汇总

| 标签 | 条数 | 条目 |
|---|---|---|
| `evergreen` | **6** | 抽象、蚌埠住了、你礼貌吗、整活（generalized）+ 吃瓜、麻了（pure） |
| `recent_meme` | **2** | 咕咕嘎嘎（2025）、哈基米（2024） |
| `proper_noun` | **1** | 嘉豪 |
| `unclear` | **1** | 今夕是何年 |
| `new_meme` | **0** | 无（严格 30 天口径） |
| `rising_old` / `news_event` / `marketing` | **0** | 本轮不可测或无命中 |

### 3.2 证据瑕疵（与 QA-QUALITY 的 R8 同源，仍存在）

| 条目 | 问题 | 影响 |
|---|---|---|
| 抽象 | sample 是长评论，命中「抽象化」子串，不是独立词「抽象」 | 高估「被当梗使用」的直接性；不改变 evergreen 判断 |
| 麻了 | sample「闹麻了」，与「麻了」异义（族归并过宽） | 证据与条目不符；QA 归因为 `extract._similar` 包含规则 |
| 今夕是何年 | sample 与标题同形，df_zone=1，无评论区用法 | 无法区分「标题引用」与「当梗使用」→ 只能 `unclear` |
| 吃瓜 | `status=observing` 但 rank=10，仍进 Top10 | 展示层把 observing 当榜单，稳定性口径失效 |

## 4. 指标明细

| 指标 | 值 | 算法/来源 |
|---|---|---|
| seed 命中率 | **100%（10/10）** | `reasons.seed_hit` 10/10；种子表匹配 10/10 |
| origin 年份分布（Top10） | 2020:1、2021:3、2022:1、2023:2、2024:1、2025:2 | `meme.origin` 优先，空则 `seed.origin` |
| classic(≤2023) / recent(2024+) | **7 / 3** | 同上 |
| 平均年龄 | **3.87 年**（中点）；**3.50 年**（名义） | `as_of=2026-11-14`；见 EVAL-DESIGN R4 |
| 常青占比 | **60%（6/10）** | pure 2 + generalized 4 |
| 新闻/营销/专名 | **0 / 0 / 1** | 人工标签快照 |
| 同族重复 | **0** | 10 个 family_key 唯一；`meme_alias` 无互指 |
| 非种子占比 | **0%（0/10）** | 可用 14 条全为种子 |
| 候选池 families | **1046** | `cycle.stats_json`（单轮口径） |
| 候选池 candidates | **1500** | 同上 |
| `meme_signal` 可用数 | **14**（hot 9 / observing 5） | `meme_signal where cycle_id=7` |
| 非种子可用数 | **0**（非种子族 1032，全部不可用） | 1046 − 14 种子族 |
| 表快照（运行时刻） | `meme` 1097 / `candidate` 1565 | 会随采集增长，非单轮口径 |
| 黄金集正例进候选 | **5/30 = 16.7%**（那咋了、抽象、整活、老六、爷青回） | `candidate_cycle` cycle=7 |
| 黄金集正例被种子覆盖 | **30/30 = 100%** | 词典不是召回瓶颈 |
| 黄金集负例误入 | **0/30** | 负例全为长新闻句，测试集盲区，不能证明规则有效 |

## 5. 与 QA-QUALITY 旧基线（cycle#2）对照

QA-QUALITY 的审计对象是 cycle#2（14:10–14:11），本基线是 cycle#7（14:53–14:53）。两者相隔约 43 分钟、7 轮，Top10 集合相同、名次有互换。

| 项 | QA-QUALITY（cycle#2） | 本次（cycle#7） | 差异/解释 |
|---|---|---|---|
| Top10 集合 | 抽象、咕咕嘎嘎、蚌埠住了、哈基米、嘉豪、整活、今夕、你礼貌吗、麻了、吃瓜 | 同 10 条 | **集合完全一致** |
| Top10 顺序 | 整活#6、今夕#7、你礼貌吗#8、麻了#9 | 今夕#6、你礼貌吗#7、麻了#8、整活#9 | 仅名次互换；整活 velocity≈0 掉到第 9 |
| seed 命中 | 10/10 | 10/10 | 一致；种子依赖未变 |
| 常青 + 泛化占比 | 60%（6/10） | 60%（6/10） | 同一组 6 条（抽象/整活/蚌埠住了/你礼貌吗/吃瓜/麻了） |
| 严格新热梗 | 20%（2/10，咕咕嘎嘎、哈基米） | 20%（2/10，`recent_meme`） | 标签名不同，实质一致；严格 30 天口径本次为 **0/10** |
| 专名 / 常青诗句 | 嘉豪 1 / 今夕 1 | 同 | 今夕本次仍只能 `unclear`（C 级） |
| 新闻 / 营销 | 0/10 | 0/10 | 一致 |
| 非种子可用 | 0（QA 记录约 1040/1055 被误杀） | 0（非种子 1032 族全不可用） | 根因仍是 `pipeline.py:324` 量纲错误 |
| 黄金集召回 | 5/30 = 16.7% | 5/30 = 16.7% | 一致；新增事实：30/30 正例都在种子里 |
| 同族重复 | 0/10（但 1 例过合并：麻了→闹麻了） | 0/10；麻了 sample 仍为「闹麻了」 | 过合并未修 |
| 平均 origin 年龄 | 未报告 | **3.87 年**（名义 3.5） | 新增指标 |
| 证据瑕疵 | 2/10（麻了、今夕） | 3/10（抽象子串、麻了、今夕） | 抽象 sample 子串问题本次一并计入；属口径细化，不是新回归 |
| observing 进 Top10 | 咕咕嘎嘎 82.43 标 observing 仍第 2 | 吃瓜 53.32 标 observing 仍第 10 | 同一展示层问题的另一次复现 |
| `is_new` / 新晋 | QA-QUALITY 记录：前端按 `first_seen<36h` 全标「新晋」 | cycle#7 `is_new` 全 0；cycle#14 起 yyds 才 `is_new=1` | `is_new`=首次进入本系统观测，不是「新梗」；评测不以 UI 标签为证据 |

**差异结论**：两次基线的**结构完全相同**（10/10 种子、6/10 常青、2/10 近两年新梗、0 新闻/营销、5/30 黄金召回）。差异只在名次与证据瑕疵计数（2→3，因把「抽象」子串纳入统计）。因此 cycle#7 可作为 v2 的稳定对照基线，不需要回测 cycle#2。

> 注意：QA-QUALITY 的「1040–1041/1055」与本处「1032/1046」是按各自 cycle 的 families 算的，比例同为 ~99%；两轮 families 相差 9 族，属候选集波动，不是修复。

## 6. 当前基线**不是**什么（防止误用）

1. **不是真值**：标签是单人判断，含 A/B/C 置信度；`unclear` 1 条、`proper_noun` 1 条都可能有争议。
2. **不是全量质量**：只评 Top10，不覆盖 1046 族；不能据此算「全库精确率」。
3. **不是新梗率**：`new_meme=0` 是「当前榜单没有 ≤30 天新梗」，不是「全网没有新梗」。
4. **不是生命周期结论**：观测史仅 45 分钟（cycle#7 时），不能判断「兴起—峰值—衰退」。
5. **不是可跨轮直接比较的累计表**：所有单轮指标必须 `--cycle` 固定；表总数（1097/1565）只是运行时刻快照。

## 7. 复现与产物

```bash
cd /Users/wanjunqiang/Desktop/hot_meme_search
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7            # JSON + Markdown
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7 --format md
```

- 脚本：`eval/metrics.py`（标准库，只读 URI + `PRAGMA query_only=ON`）
- 标注模板：`eval/labeling-template.csv`（50 行：cycle#7 Top10 + 40 条候选；`label` 留空）
- 口径：`docs/pm/EVAL-DESIGN.md`
- 旧基线：`docs/pm/QA-QUALITY.md` §2–§4（cycle#2）
