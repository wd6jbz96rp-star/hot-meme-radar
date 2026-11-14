# P6 · 审核池优先级修复与真实 DeepSeek shadow 重跑

> 会话：gate-priority（`bc67f997-357f-46fa-bc4b-61afd9aab626`），委派方 lead。
> 项目根：`/Users/wanjunqiang/Desktop/hot_meme_search`，HEAD `cb31279`（LLM TLS 修复）。
> 范围：`app/pipeline.py`、`app/config.py`、`tools/shadow_run.py`、`tests/test_p6_gate_priority.py`、本文件。
> **未修改** `app/llm.py`、`app/llm_extract.py`、`app/review.py`、`app/judge.py`、`app/score.py`、
> `app/extract.py`、`static/*`、`data/*`、`docs/01`、`README`；未 commit、未装依赖、未起服务、
> 未碰 :8000、未写生产库；key 只从 `~/.hotmeme_llm_key` 读取，未打印、未落盘。

## 0. 结论（TL;DR）

| 项 | 修复前（P6 证据 `/tmp/shadow_p6.db`） | 修复后（P7 `/tmp/shadow_p7.json`） |
|---|---|---|
| 审核池 | 50 条，只按 score；**池内 extract 0 条** | 24 条，extract > comment_backed > score；**extract 2/0/4 条** |
| 实际审到 | 18/18/18，全是 ai/co/ge 等规则垃圾 | **24/24/24**，每轮 3 条池外上限内全覆盖 |
| 抽取候选被审 | **0 条**（21/16/16 条抽取候选一条没进审核） | **6 条次**（2/0/4；R2 抽取调用 `schema` 失败） |
| approve | 0/0/0 | **1**（R1「神人」；R2/R3 0） |
| fallback | 3 轮全部 `budget`（18 调用触顶） | **0 轮 fallback**（R2 为采集降级，非审核） |
| 脚本收尾 | `conn.closed` AttributeError，未写 JSON | 3 轮完整写 `/tmp/shadow_p7.json`，exit 0 |
| 成本 | 18 调用/轮，未写 JSON 无完整成本 | 合计 **¥0.222/3 轮**；审核 ¥0.043–0.053/轮，抽取 ¥0.025–0.029/轮 |
| 测试 | 207 | **214 passed**（+7，`tests/test_p6_gate_priority.py`） |

**一句话**：池优先级修复生效了——抽取候选第一次被真实 LLM 审到并拿到 approve；但「15–24 条抽取候选全部进首轮」
在当前规则闸门下**做不到**：P7 每轮 `list=new` 里的抽取候选只有 4/0/10 条，过梗性/评论门后只剩 2/0/4 条可送审。
这是**证据不足**（多数抽取候选只有 1 条评论/1 个视频），不是审核池排序问题，也不应靠放水解决。

## 1. 根因复盘（修复前）

P6 真实 shadow（`/tmp/shadow_p6.db`，DeepSeek，3 轮）可复现：

| 轮 | extract 候选 | 规则 new 项 | 旧池（top50） | 池内 extract | 实审 | approve | fallback |
|---|---|---|---|---|---|---|---|
| 1 | 21 | 100 | 50 | 0 | 18 | 0（17 reject + 1 unclear） | budget |
| 2 | 16 | 109 | 50 | 0 | 18 | 0（17 reject + 1 unclear） | budget |
| 3 | 16（缓存） | 108 | 50 | 0 | 18 | 0（15 reject + 3 unclear） | budget |

旧 `review_pool` 只有一个排序键 `-score`，再切 `LLM_REVIEW_LIMIT=50`。抽取候选（「给我整不会了/油腻老登/
小孩哥小孩姐/没绷住/吃刀子/爱蚊TV/满嘴烂梗/最晚之人/精神污染/赛级…」）规则分低，全部排在 50 名之外；
而 `LLM_MAX_CALLS=18` 连池的前 18 条都审不完，于是每轮都在审 `ai/co/ge/in/…`，抽取出好候选零曝光。

DB 复核（P6 第 3 轮）：

```
new 项 108，旧池 50，extract 0，comment_backed 50，池最低分 76.64
llm_review 18 行：ai/b站/on/ge/看完/10/…；extract 候选 0 行
```

结论：这是**优先级 + 预算双问题**，不是抽取没产出。

## 2. 修复内容

### 2.1 审核池三级优先级（`app/pipeline.py:624-676`）

新增 `_review_item_extract_meta` / `_review_item_comment_backed` / `review_pool_kind`：

1. `extract`：`item.llm_extract`、`components.llm_extract`、`raw_signals.llm_extract` 或
   `source` 含 `llm_extract`（兼容 P5 三种写入形状）；
2. `comment_backed`：`item.comment_backed` / `components.comment_backed` / `raw_signals.comment_backed`；
3. `score`：其余。

组内仍按 `(-score, canonical)` 稳定排序；先拼 extract、再 comment、最后 score，再切
`LLM_REVIEW_LIMIT`。池上限与 `LLM_REVIEW_LIMIT` 对齐（默认 24），不再固定 50。

**边界**：池仍只收 `list=new` 且 `status in (hot, observing)` 的可发布候选；没有为了凑 15–24 条
把 `rejected` 塞进审核池（那会让 approve 无法发布，或变成绕过规则闸门）。

### 2.2 预算（`app/config.py:153-158`）

```python
LLM_MAX_CALLS = max(0, _env_int("HOTMEME_LLM_MAX_CALLS", 24))
LLM_REVIEW_LIMIT = max(1, min(50, _env_int("HOTMEME_LLM_REVIEW_LIMIT", 24)))
```

理由：24 条池 + 24 调用可保证「池内每条都能审到」（配额 15 + 9，两轮审完）；实测审核单条
≈ ¥0.0018–0.0022，24 条 ≈ ¥0.043–0.053/轮，仍在预算量级内。旧 18 调用连 24 条池都覆盖不了，
只要池里垃圾多，抽取候选必然被饥饿。env 覆盖保留，`HOTMEME_LLM_MAX_CALLS=18` 可回退。

### 2.3 stats 可观测性（`app/pipeline.py:659-676, 902-916`）

每轮 stats 新增：

- `llm_pool_extract` / `llm_pool_comment` / `llm_pool_score`：**截断后**池构成；
- `llm_pool_details`：`[{family_key, canonical, score, pool_kind, known}]`，供 shadow 报告映射 verdict；
- `llm_extract_new_total` / `llm_extract_admitted_total`：`list=new` 中带 extract metadata 的总数
  vs 过了规则闸门可送审数——这两个数的差距直接暴露「证据不足被规则拒绝」，不再让池构成背锅。

### 2.4 known 处理保持一致

`known=True` 的抽取候选照常按 extract 优先入池、照常送审，不自动 approve；只有 LLM 明确
approve 才计数。测试 `test_gate_stats_composition_and_known_not_auto_approved` 锁定：known 候选被
fake 审核器 reject 时 `llm_approved=0`。P7 实测「那咋了」（known）3 轮全是 `unclear`，没有放水。

### 2.5 `tools/shadow_run.py` 收尾修复

1. **`sqlite3.Connection` 没有 `.closed`**：`finally` 改为无条件 `conn.close()`（close 幂等），
   并删除 except 分支里的重复 close。修复后 3 轮完整写 JSON、`main()` 返回 0。
2. **每轮新建 ChatClient**：`app/llm.py` 的 `max_calls` 是**进程内累计**硬上限（默认 60）；
   旧脚本 3 轮共用一个 client，R1+R2 已用 48 次，R3 审到第 12 条就抛 `budget`，被误记成审核预算耗尽。
   现在每轮新建 client，R3 的 `llm_budget` 误报消失（最终 P7 三轮 24/24/24，无 fallback）。
3. 报告增强：`review.pool_extract/pool_comment/pool_score`、`review.extract_reviewed`（逐条 verdict+理由）、
   `review.extract_in_new/admitted`、`review.pool_details`、`new_list`（`store.list_items(...,"new")` 同口径）。

### 2.6 测试（`tests/test_p6_gate_priority.py`，+7）

| 用例 | 覆盖 |
|---|---|
| `test_review_pool_extract_first_then_comment_then_score` | 三级优先级、组内 score 降序、stats 构成 |
| `test_review_pool_low_score_extract_not_crowded_out_by_garbage` | 20 条低分 extract 全部进池，垃圾只能占剩余名额 |
| `test_review_pool_capped_at_llm_review_limit_and_skips_rejected` | 上限 24；rejected 不混入 |
| `test_review_pool_recognizes_raw_signals_source_shape` | `raw_signals.source` / `components.llm_extract` 形状 |
| `test_review_pool_limit_and_call_budget_defaults_aligned` | 默认 24/24 |
| `test_gate_stats_composition_and_known_not_auto_approved` | stats 构成、known 不自动通过、审计落库 |
| `test_shadow_run_three_rounds_writes_json_and_exits_0` | fake extractor/reviewer 3 轮、JSON、exit 0、每轮独立 client、key 不落 JSON |

全仓：`.venv/bin/python -m pytest -q` → **214 passed**（基线 207 + 7）。

## 3. 真实 DeepSeek 重跑（P7）

命令（key 来自文件，未打印）：

```bash
HOTMEME_LLM_API_KEY_FILE="$HOME/.hotmeme_llm_key" \
  .venv/bin/python tools/shadow_run.py \
  --rounds 3 --db /tmp/shadow_p7.db --json /tmp/shadow_p7.json
```

退出码 0，`/tmp/shadow_p7.json` 生成；未写 `data/`，未起服务。

### 3.1 每轮提取候选

| 轮 | 抽取调用 | 候选 | 新增/合并 | known | 候选（节选） |
|---|---|---|---|---|---|
| 1 | 3 | 14 | 13/1 | 1 | 神人、那咋了、给梗办葬礼、最晚之人、黄梁一梦、这波我赢了兄弟、有丝竹之乱耳、画地为牢、爱蚊TV、老黄现在活着就算成功、用时66秒、鼓励式教育、我，最晚、压压惊 |
| 2 | 1 | **0** | 0/0 | 0 | **`reason=schema`**：抽取返回未过 schema，重试 1 次仍失败 |
| 3 | 3 | 20 | 14/6 | 4 | 烂梗、油腻老登、神人、考古、小孩哥小孩姐、抽象、那咋了、万恶之源、最晚之人、AI蚊子、松弛感、甲亢哥、这波我赢了兄弟、蚊子开演唱会、画地为牢、爱蚊TV、破防、鼓励式教育、怪上头、压压惊 |

R2 的 `schema` 失败导致该轮 `extract_in_new=0`，审核池全为 comment_backed——这是 P7 唯一的
「抽取候选没进池」轮次，原因是抽取调用失败，不是池排序。

### 3.2 审核池构成（修复后）

| 轮 | 池上限 | 池内 extract | comment_backed | score 补足 | `list=new` extract 总数 | 过规则闸门可送审 |
|---|---|---|---|---|---|---|
| 1 | 24 | **2** | 22 | 0 | 4 | 2 |
| 2 | 24 | 0 | 24 | 0 | 0（抽取 schema 失败） | 0 |
| 3 | 24 | **4** | 20 | 0 | 10 | 4 |

对比修复前：池 50 / extract 0 / 实审 18；修复后：池 24 / extract 2、0、4 / 实审 24。
R1 池内 extract =「神人」(57.91) +「那咋了」(17.03, known)；R3 =「ai」(87.24) +「烂梗」(80.33) +
「考古」(67.32) +「那咋了」(15.3, known)。其中 `ai` 是 n-gram 家族吸收了 `AI蚊子` 的 metadata
（`n-gram+llm_extract`），按优先级算 extract，LLM 正确判 reject。

### 3.3 实际审到的 extracted candidate 与 verdict

| 轮 | 候选 | verdict | meme_prob | 理由（摘要） |
|---|---|---|---|---|
| 1 | 神人 | **approve** | 0.62 | 可独立成词并套「逆天神人/一群神人」，评论+弹幕反复出现，有语义增量 |
| 1 | 那咋了（known） | unclear | 0.30 | 仅 1 条评论/1 个视频，扩散与出处太薄，按规则不得 approve |
| 2 | —（无 extract 候选） | — | — | 抽取 `schema` 失败；本池 24 条全为 comment_backed |
| 3 | ai | reject | 0.05 | 高频缩写/普通词，无模板、无出处 |
| 3 | 烂梗 | reject | 0.05 | 对梗的贬义泛称，评论在讨论它而非使用它 |
| 3 | 考古 | reject | 0.15 | B 站长期通用「回看旧视频」用语，非新梗、无明确出处事件 |
| 3 | 那咋了（known） | unclear | 0.30 | 证据仍只有 1 条评论/1 个视频 |

审核总量：

| 轮 | approve | reject | unclear | merge | fallback |
|---|---|---|---|---|---|
| 1 | 1 | 21 | 2 | 0 | 无 |
| 2 | 0 | 22 | 1 | 1 | 无（采集 degraded，非审核） |
| 3 | 0 | 22 | 2 | 0 | 无 |

R2 `status=degraded` 的原因是采集只拿到 175 个视频（R1/R3 为 373/381），有分区抓取失败；
`llm_fallback=false`，审核本身完整跑了 24 条。

### 3.4 Top10 与 new 榜候选

| 轮 | Top10（rank: 梗/分） | new 榜前 5（score 排序，shadow 未过滤） |
|---|---|---|
| 1 | 抽象 89.0 / 上头 77.7 / 哈基米 76.4 / 整活 74.3 / 咕咕嘎嘎 67.7 / 离谱 59.9 / 吃瓜 56.6 / 嘉豪 45.0 / yyds 43.7 / 绝绝子 31.4 | ai(reject)、b站(reject)、看完(reject)、on(reject)、er(reject) |
| 2 | 抽象 87.5 / 离谱 73.2 / 咕咕嘎嘎 69.9 / 人设 67.0 / 哈基米 66.3 / 整活 62.4 / 嘉豪 62.2 / 绝绝子 26.6 / yyds 24.7 / 那咋了 15.1 | 没人(reject)、st(reject)、ai(reject)、li(reject)、b站(reject) |
| 3 | 抽象 86.0 / 上头 79.0 / 哈基米 69.2 / 整活 68.5 / 咕咕嘎嘎 66.0 / 离谱 64.1 / 吃瓜 57.7 / 破防 54.6 / 嘉豪 49.3 / yyds 34.4 | ai(reject)、on(reject)、ge(reject)、10(reject)、in(reject) |

**new 榜仍不能发布**：shadow 模式只写审计、不过滤展示；即使切 `demote`，被审的 24 条 reject 被滤掉后，
后面**未审**的规则垃圾（如「5年/也能/给你/he/鬼畜/个月」）会顶上来。审核覆盖 24 条，而 new 榜有
99–326 条同口径候选，覆盖率不足是独立问题。

### 3.5 成本 / 耗时

| 轮 | 耗时 | 抽取 ¥ | 审核 ¥ | 合计 ¥ | 审核调用 |
|---|---|---|---|---|---|
| 1 | 254.1s | 0.0248 | 0.0434 | **0.0681** | 24 |
| 2 | 220.9s | 0.0292 | 0.0529 | **0.0820** | 24 |
| 3 | 65.0s | 0.0262 | 0.0457 | **0.0719** | 24 |
| 合计 | 540.0s | 0.0802 | 0.1420 | **0.2221** | 72 |

- 审核 ¥0.043–0.053/轮，符合「24 调用 ≈ ≤¥0.07」；含抽取的首轮合计 ¥0.068–0.082，R2 因语料更大
  （1617 条评论、1064 候选）略超 ¥0.07。
- 抽取调用 7 次（R2 失败 1 次），R3 仍为冷缓存（评论集合变化），未出现 R1 式 3 批全缓存。

## 4. 为什么「15–24 条抽取候选全部进首轮」没做到

这是 P7 最重要的发现，直接回答任务 #6：**候选质量/证据不足**，不是 rubric 过严，也不是池排序。

- `llm_extract_new_total`：4（R1）/ 0（R2）/ 10（R3）；`admitted`：2 / 0 / 4。
- 第一道漏斗是 `classify_list`：14（R1）/ 20（R3）条抽取候选里只有 4 / 10 条被判 `new`，
  其余是 `classic`。抽取采样的是 **30 天**高赞评论，而 new 榜要求「≤7 天有评论、≤90 天」；
  很多候选唯一证据评论在 8–30 天前，天然进不了 new（这不是审核池能修的）。
- 第二道漏斗才是状态：new 的 4 / 10 条里只有 2 / 4 条过梗性/评论门；
  其余被 **meme gate** 拒绝（全轮 `meme_gate_rejected` 469/459/471，`comment_gate_rejected=0`）。
- 典型证据（P7 R3 candidate 表）：`油腻老登` 1 评论/1 视频、`小孩哥小孩姐` 1/1、`玩梗归玩梗` 1/1、
  `精神污染` 1/1、`最晚之人` 1/1、`爱蚊TV` 1/1、`赛级` 1/1。`comment_backed` 门槛是
  ≥3 条 ≤30 天评论、≥2 视频、≤7 天 ≥1；同时 `judge` 梗性 <0.45，于是 `rejected`，连审核池都进不了。
- 能进池的 `考古` 有 2 评论/2 视频，`神人` 3 评论/多视频；这正好说明「证据够的能进、不够的不能进」，
  优先级修复只解决「审核池排序」，解决不了「上游没收进来」。

因此**没有**为了凑数把 rejected 抽取候选塞进池，也没有降 `MEME_MIN`/`comment_backed` 门槛。
把 15–24 条硬拉进首轮只能靠放水，与「不要为了 approve 放水」冲突。

## 5. 质量判断与下一步

### 5.1 这次 approve 可信吗？

- R1「神人」approve（0.62）是**边界通过**：它是通用汉语词/调侃称谓，不像「爱蚊TV/给梗办葬礼」这类
  新表达；人工看更适合观察而非直接发布。
- 同一 P7 流程第一次运行（保留 `/tmp/shadow_p7_run1.*`）R2 曾 approve「考古」（0.72），
  最终 P7 R3 又把「考古」reject（0.15，理由：B 站长期通用语、非新梗）。**同一候选跨轮 verdict 反转**，
  说明 rubric 在「既有通用词 vs 新梗」边界不稳定；temperature=0 也复现。
- 结论：**有 1 条 approve ≠ 可发布**。不切 `demote/require_approve`，不硬调阈值。

### 5.2 下一步（按优先级）

1. **抽取证据门槛前置（P7 主修）**：`llm_extract` 只保留 evidence_ids ≥2 个不同评论、且跨 ≥2 视频的候选；
   或在 `merge_llm_candidates` 后对 1 评论候选不建 family（P7 多数抽取候选只有 1 条评论，注定被 meme gate 拒）。
   这会减少抽取候选数，但让「抽取→审核」漏斗不再空转。
2. **抽取 schema 稳定性**：R2 `reason=schema` 0 候选，需在 `llm_extract`（冻结文件，需另一会话）
   增加容错/一次修复重试/更严格 JSON 提示；否则 3 轮里 1 轮直接断供。
3. **审核覆盖与榜单展示对齐**：new 榜 99–326 条、审核 24 条，`demote` 后仍有大量未审垃圾。
   要么把 new 榜展示限制在已审 top N，要么对规则层做停用词/功能词清洗（`judge.py` 冻结，需另派）；
   否则不能灰度。
4. **rubric 明确「新」的定义**：approve 应要求「近 N 天首次出现/扩散」还是「只要可模仿」；当前 prompt
   两者混用，导致 `考古/神人` 这类老词边界摇摆。`app/review.py` 冻结，需原 owner 改 prompt 后再 shadow。
5. **重跑观测**：修抽取门槛后跑 3 轮，验收指标改为
   `extract_in_new ≈ admitted`、`extract_reviewed ≥ 10`、`approve ≥1 且人工复核非老梗`，
   而不是「池里塞满 15–24 条」。

## 6. 验证与证据

```bash
# 全仓测试
.venv/bin/python -m pytest -q          # 214 passed

# 真实 shadow（exit 0；key 不打印）
HOTMEME_LLM_API_KEY_FILE="$HOME/.hotmeme_llm_key" \
  .venv/bin/python tools/shadow_run.py --rounds 3 \
  --db /tmp/shadow_p7.db --json /tmp/shadow_p7.json

# 产物
/tmp/shadow_p7.json        # 3 轮完整报告（status=ok）
/tmp/shadow_p7.db          # 临时 DB，非生产
/tmp/shadow_p7_run1.json   # 修 fresh-client 前的对照（R3 llm_budget）
```

DB 校准（P7）：

```sql
SELECT id,status,
       json_extract(stats_json,'$.llm_pool'),
       json_extract(stats_json,'$.llm_pool_extract'),
       json_extract(stats_json,'$.llm_approved'),
       json_extract(stats_json,'$.llm_fallback_reason')
FROM cycle;   -- 1 done 24 2 1 ''; 2 degraded 24 0 0 ''; 3 done 24 4 0 ''
```

## 7. 文件清单

| 文件 | 改动 |
|---|---|
| `app/config.py` | `LLM_MAX_CALLS` 18→24、`LLM_REVIEW_LIMIT` 50→24（env 可覆盖） |
| `app/pipeline.py` | 审核池三级优先级、池构成/溢出 stats、extract 在 new 的供给 stats |
| `tools/shadow_run.py` | 修 `conn.closed`；每轮新建 ChatClient；报告池构成/extract verdict/new_list |
| `tests/test_p6_gate_priority.py` | 新增 7 个回归用例 |
| `docs/pm/P6-GATE-PRIORITY.md` | 本文件 |

未 commit（按任务要求）。
