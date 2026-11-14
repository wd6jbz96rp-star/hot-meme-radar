# 捕获与统计

## 1. 为什么从 B站 评论/弹幕做起

热搜标题只能说明「有人在讨论这个事件」，不能说明「这个符号被模仿使用」。梗的核心是**模仿性使用**，而弹幕天然是「同一句话被大量人重复」的场所：

- 弹幕整句跨视频重复 → 梗句扩散的直接证据；
- 评论区出现「这是什么梗 / 什么出处」→ 语境依赖的语义证据；
- 评论/弹幕里同一表达覆盖多个 UP 主、多个分区 → 自发扩散而非官方推广。

实测（2026-11，免登录）：

| 接口 | 用途 | 备注 |
|---|---|---|
| `x/frontend/finger/spi` | 取 buvid3/buvid4 | 免登录，用于通过排行榜风控 |
| `x/web-interface/ranking/v2?rid=` | 分区榜，约 90–100 条/区 | 带 view/danmaku/reply/like/coin/fav/share |
| `x/web-interface/popular` | 全站热门补充 | ps ≤ 50 |
| `x/v2/reply/main?type=1&oid=&mode=3&next=0&ps=20` | 热度评论（mode=3）/ 时间序评论（mode=2） | 免 wbi、免登录；首屏 `next=0`，`ps=20`；响应含 `replies`、可选 `top_replies`、`cursor.next` |
| `comment.bilibili.com/{cid}.xml` | 弹幕 | raw-deflate，`zlib.decompress(data, -15)` |
| `x/web-interface/wbi/search/type` | 搜索「热梗/梗百科/热梗盘点」等视频 | 需 wbi 签名 |
| `x/player/pagelist` | 搜索视频补 cid | 搜索结果不含 cid |
| `x/web-interface/search/square` | 热搜旁证 | 暂未参与打分 |

## 2. 四条通道

| 通道 | 内容 | 作用 |
|---|---|---|
| A 视频池 | 8 个分区榜 + 全站热门 | 候选视频与官方统计 |
| B 语料 | 标题、简介、热评、弹幕 | 新词发现与「被使用」证据 |
| C 检索 | 20 组「热梗/梗百科/年度热梗」视频及其标题、标签 | 提升梗的召回，并提供「梗源覆盖」信号 |
| D 知识 | 种子梗表 148 条 + 别名表 | 先验、归并、区分老梗 |

分区权重按造梗密度设定：鬼畜 1.4、生活 1.2、动画 1.1、游戏 1.1、影视 1.0、音乐 0.9、娱乐 0.9、知识 0.7。

### 2.1 语料配额与窗口（v2 P4：45 评论视频）

评论是第一语料区，弹幕降为「梗句扩散」旁证：

| 项 | 口径 |
|---|---|
| 评论视频 | 45 个（分区榜按 `reply` 取 + 10 个梗搜索视频；分区配额不足时按 `reply/view` 补齐），`COMMENT_VIDEOS=45` |
| 评论接口 | `/x/v2/reply/main`，每视频 `mode=3`（热度）1 页 + `mode=2`（时间）3 页，`ps=20`，`COMMENT_PAGES={3:1, 2:3}`；45 视频共 180 评论请求 |
| 每视频保留 | 两种 mode 合并去重后 ≤40 条（`COMMENT_MAX_PER_VIDEO=40`）：热度评论按赞、时间评论按 `ctime` 各取一半，再按赞补齐 |
| 评论窗口 | 只收 `ctime` ≤30 天（`COMMENT_WINDOW_DAYS=30`）；≤7 天单独计数用于增速（`COMMENT_RECENT_DAYS=7`） |
| 评论去重 | 先按 `rpid`，再按归一化文本；同一评论同时出现在两个 mode 只留一条 |
| 评论落库 | `text_sample.raw_time = ctime`（Unix 秒）；`TextDoc` 以动态属性携带 `ctime/mode/rpid`，不改 `extract.TextDoc` 字段 |
| 弹幕视频 | 10 个（`DANMAKU_VIDEOS=10`），每视频随机采样 ≤200 条（`MAX_DANMAKU_PER_VIDEO=200`）；只占约 10 请求 |
| 弹幕定位 | 只作整句扩散旁证与「老梗刷屏」识别，不参与评论证据门控 |
| 请求预算 | 冷轮 ≈210–230（评论 180 + 排行榜 8 + 热门 1 + 搜索 20 + spi/nav 2 + pagelist ≤10 + 弹幕 10 + 偶发重试）；2 rps 下 ≈110–120s |

翻页必须把上一页返回的 `cursor.next` 原样传给下一页（实测是 1682 这类 opaque 游标，不是 20/40 偏移量：传 20 会翻到 2024 年旧页并 `is_end`）。翻页停止只认 `cursor.next == 0`（`is_end`），不能按「短页」提前停。缓存键 `(aid, mode, page)` 并持久化 `next_cursor`，warm 轮命中缓存即可判断是否还有下一页，命中不产生请求。

旧 `/x/v2/reply` 实测匿名仅返回 3 条 → 一轮 90 条评论；`reply/main` 单页 ~19–20 条，45 视频 ×（mode3 1 页 + mode2 3 页）≈ 单轮 900+ 条候选（30 天窗口内按 P1 实测约 13–20 条/视频落地）。

### 2.2 评论页失败分类、重试与失败统计（P4）

评论页失败不再「一律返回空 + 记 error」，而是先分类再决定是否重试：

| 类别 | 判定 | 是否重试 |
|---|---|---|
| `http_412` / `http_403` | `HttpError.status in (412,403)`（风控；urllib 与 curl 兜底都失败） | 是，且在首次重试前强制重新 `bootstrap`（重取 buvid3/buvid4），每页最多一次 |
| `http_5xx` | HTTP 5xx | 是 |
| `html` | 2xx 但响应体以 `<` 开头（风控 HTML 页） | 是，且强制重新 bootstrap |
| `empty` | 2xx 但响应体为空/空白 | 是，且强制重新 bootstrap |
| `non_json` | 2xx 但 `json.loads` 失败（非 HTML） | 是 |
| `code:-352` | 合法 JSON 但 `code=-352`（风控） | 是，且强制重新 bootstrap |
| `code=-404/12002/12061/12071` | 无评论/视频不可见 | **否**；视为有效空页（可缓存），不记 error |
| 其他业务 code / 网络错误 | 合法 JSON 业务错误，或 urllib 网络失败（`http.get` 内部已重试） | 否 |

- 重试退避：`0.5s → 1s → 2s`（指数），最多 3 次重试（共 4 次尝试，`COMMENT_MAX_ATTEMPTS=4`）；单轮重试总预算 `COMMENT_RETRY_BUDGET=40`，防止风控风暴把冷轮拖长（超出预算的页直接记失败，不再退避）。
- **只缓存有效 JSON 页**：`comment_page_ex` 返回 `(items, next_cursor, ok)`，`ok=True` 才写 `comment_cache`（含 `-404` 这类有效空页）；失败页不写缓存，下一轮会重新请求（warm 轮因此可能残留少量评论请求，这是正确行为，不是缓存失效）。
- **单页失败不整轮降级**：失败页计入 `comment_page_fail`，重试计入 `comment_page_retry`；只有 `comment_page_fail / comment_requests > 20%`（`COMMENT_FAIL_RATE_MAX`）才把 cycle 标 `degraded`。评论 error 仍写入 `stats.errors` 与 `source_error`，用于追溯，但不再触发「有任何 error 就 degraded」的旧逻辑。
- **失败也产出榜单**：评论、标题、弹幕、种子/候选池互相独立；评论全失败时 `comment_evidence_ready=false`、评论门控跳过，仍用标题/弹幕/已有候选正常出榜，不空榜。

## 3. 候选发现

1. **归一化**：NFKC、去 emoji/零宽、合并重复字符（保留数字梗原形，23333 → 2333）、按标点切段。
2. **模式抽取**：引号内容、`XX 是什么梗/出处` 上下文、`#话题#`、【】标题、弹幕整句（3–20 字）。
3. **新词发现**：2–6 字 n-gram，要求
   - 频次 ≥ 3
   - 凝固度 `min PMI ≥ 3.0`（log2）
   - 左右邻接熵 `min ≥ 1.0`
   - 文档频率 ≥ 2
   - 不在通用词表/口语常用词表中（种子词除外）
4. **合并**：模式候选与 n-gram 候选合并，按 `频次 × log(1+视频数)` 排序取前 1500。

### 3.1 LLM 候选抽取（P5，默认关）

`HOTMEME_LLM_EXTRACT=1` 时，在 `discover(docs)` **之前**把 `kind=="comment"` 的近期评论交给
`app/llm_extract.run_extraction`（近 30 天、高赞优先、450 条/3 批上限；见
`docs/pm/P4-LLM-EXTRACT.md`）。候选按 `normalize_surface(canonical)` 与 n-gram 候选合并：

- **已有同 surface**：只补 `type/meaning/origin_quote/confidence/known` 到 `metadata`
  （`source="n-gram+llm_extract"`），**不重复计数**；
- **新候选**：`count/df_video/df_zone` 由 `evidence_ids` 回映射评论/视频计算，
  `source="llm_extract"`，作为新 candidate/family 入池；
- **known=True**（命中 `data/seed_memes.json` 种子/别名）：标 `llm_known`，**不走**
  「评论证据绕过梗性闸门」的新梗旁路（种子已有自己的通道）；
- **降级**：无 key / 抽取失败 / 开关关 → 空列表 + `llm_extract_reason`，规则榜照旧；
  任何异常都不中断采集。

凝固度公式：对词 w 的每种切分 (a,b)，`PMI = log2( p(w) / (p(a)p(b)) )`，取最小值。

邻接熵：对左/右邻字符分布求信息熵，取左右较小值；熵越高说明该词越能独立成词。

## 4. 热度模型

每轮对每个梗族计算：

| 信号 | 定义 |
|---|---|
| 曝光 E | Σ log1p(视频播放) × 分区权重 |
| 互动 G | Σ log1p(弹幕 + 2×评论 + 点赞/20) |
| 广度 B | 视频数、分区数、UP 数的分位均值 |
| 增速 V | (本轮频次+1) / (过去 7 天日均+1) |
| 梗性 P | 判定分 0–1（含「梗源覆盖」：出现在热梗类搜索视频中的条数） |
| 评论证据 | 见 §4.1：窗口计数、年龄归一赞、7 天增速、弹幕刷屏标记 |
| novelty N | 见 §4.2：评论活跃度 × 年龄新鲜度（0–1），旧梗/空口碑不奖励 |

### 4.1 评论证据（P0）

按梗族汇总 ≤30 天评论证据，写入 `components.raw`（API 原样透传），同时留 `components.comment`：

| 字段 | 定义 |
|---|---|
| `comment_count_recent` | ≤30 天命中该族的评论文档数 |
| `comment_7d_count` | ≤7 天命中数；`comment_mid_count` = 7–30 天命中数 |
| `comment_growth_7d` | 7 天增速 = `(comment_7d_count+1) / (comment_mid_count+1)`，>1 表示在加速 |
| `comment_like_weighted` | `Σ log1p(like) × 0.5^(age_days / 14)`（`COMMENT_LIKE_HALF_LIFE_DAYS=14`），点赞按年龄归一 |
| `comment_df_video` | 命中该族的评论视频数 |
| `earliest_comment_at` / `age_days` | 窗口内最早评论时间（ISO）/ 距今天数 |
| `seed_origin_year` / `known_age_years` | 种子表 `origin` 解析出的年份 / 已知梗年龄（按 7/1 中点，年）；非种子为 `null` |
| `novelty` | §4.2 的 0–1 新意分，同时写入打分组件 |
| `danmaku_only` | 弹幕命中 ≥3 且 `comment_df_video<2`、`comment_7d_count<2` → 老梗刷屏旁证 |

**P0 门控**：当本轮 `comments_recent ≥ COMMENT_GATE_MIN_DOCS=200`（评论采集健康）时，非种子候选若 `comment_count_recent == 0` → `rejected`（原因 `no_recent_comment`）。种子仍走原有规则；评论采集不健康时整体跳过门控，避免单源失败清空候选池。

**P1 评论证据通道 `comment_backed`**：非种子候选同时满足 `comment_count_recent ≥ 3`、`comment_df_video ≥ 2`、`comment_7d_count ≥ 1` → `comment_backed=True`，可绕过 `MEME_MIN=0.45` 进入 `observing`（**不直接 hot、不占主榜名次**，只在 `list=new` 曝光；种子不走该通道）。同时写入 `components.raw.comment_backed`、`components.raw.comment_evidence` 与 `cycle.stats.comment_backed_count`（标记数）/`comment_backed_admitted`（实际经该通道入池数）。

### 4.2 novelty 与年龄

`novelty = 新鲜度 × 活跃度`（0–1）：

- **有效年龄**：种子取种子表 `origin` 年份（已知梗年龄，7/1 中点）；非种子取最早评论 `age_days`；两者都有时取较大值（老梗不能靠窗口内最早的近期评论装年轻）。
- **新鲜度** `= 0.5^(有效年龄年数 / 2)`：2 年半衰。2024 梗≈0.44、2022 梗≈0.22、2020 梗≈0.11；非种子（最早评论 ≤30 天）≈0.97。
- **活跃度** `= 0.5×量 + 0.3×扩散 + 0.2×加速度`：
  - 量 `= min(1, log1p(comment_7d_count) / log1p(20))`（20 条封顶，压缩刷屏）；
  - 扩散 `= min(1, comment_df_video / 5)`；
  - 加速度 `= clip((comment_growth_7d − 1) / 4, 0, 1)`。
- 没有任何评论证据 → 活跃度 0 → `novelty=0`，不奖励只有弹幕/曝光的老梗。

**新旧榜 `list`**：`seed_origin_year >= 2024` 的种子，或「非种子且 `age_days <= 90` 且有 ≤7 天评论」→ `new`，否则 `classic`。种子无 origin 年份、非种子无 7 天评论一律 `classic`（不为凑数把老梗标 new）。

归一化：每个信号在本轮候选池内取百分位，避免量纲与轮次波动。

综合分：

```
base  = 0.25E + 0.22G + 0.18B + 0.18V + 0.12P + 0.05N
score = 100 × base × 0.5^(age_hours / 48) × (0.9 + 0.1×first_seen_boost)
S     = 0.6 × score + 0.4 × 上一轮      # 首轮无历史时直接使用 score
```

- `N` 是 §4.2 的 novelty（直接使用 0–1 原值，不做分位归一：大量无评论族并列 0 时分位会把 0 抬到约 0.5，反而给老梗送分）。
- P1 权重调整：exposure 0.30→0.25、新增 novelty 0.05，合计仍为 1.0。旧梗/常青梗（novelty 低）分数略降，近两年或评论活跃的新梗/回锅梗（novelty 高）略升；变化幅度上限 5 个百分点 × 新鲜度，不足以单独翻榜，主要作用是让评论证据从「只看门控」变成「参与排序」。
- `first_seen_boost` 仍是「本系统首次观测 <72h」的 0.9/1.0 系数，与 novelty 不是同一个概念。

约束：

- 单视频命中占比 > 0.8 → ×0.5，防单视频刷屏；
- 梗性判定分（0–1）≥ 0.45 才入候选池（`MEME_MIN`），种子命中豁免该阈值；
- 连续两轮 ≥ 55（`CONFIRM_ROUNDS=2`）才稳定上榜；首次出现、曝光分位 ≥ 0.95 可直接上榜并标「新晋」；
- 梗性达标的候选按分数补齐 Top10，尾部条目在面板中显示为「观察」；
- 老梗回锅（>14 天再出现且增速 ≥ 2）重新入池并标「回锅」；
- 至少出现在 2 个视频中（`MIN_VIDEOS_FOR_RANK`）才进稳定榜；
- 「新晋」由 `meme_signal.is_new` 持久化：梗族首次出现在本系统观测历史中即本轮为真，跨轮/重启后仍可读。

## 5. 榜单输出

Top10 以梗族为单位，每条包含：热度分、名次变化、类型、首次发现时间、代表弹幕/评论、分项得分、判定依据。

P1 起 `store.top_items` 与每条榜单条目新增 `list` 字段（`new` / `classic`，规则见 §4.2），排行池落 `RANK_N=30` 条（Top10 展示不变）。API：

```
GET /api/top?list=new|classic|all   # 默认 all，保持旧行为；limit 默认 10（钳位 1–50）
```

- `list=all`：返回 Top10（与旧版一致）；
- `list=new|classic`：读本轮全部可用信号（含 rank=0 的 `comment_backed` 观察项）按 `list` 过滤后按分数取 `limit` 条；非法值返回 400；
- 响应里新增回显字段 `list`；每条新增 `llm_review`（未审核为 `null`），响应顶层新增 `llm` 摘要（见 §5.1）；
- `classic` 不做 LLM 过滤；`all` 仍按 `rank` 返回，审核结论不改变主榜成员与名次。

### 5.1 LLM 审核发布门（P3）

`list=new` 在发布前经过 LLM 审核（`app/review.py` 的 `review_candidates`，只审当前候选，不新增家族）；`classic` 与默认 `all` 保持规则榜。**默认 `off`：不构造客户端、零调用、零行为变化。** 无 key/超时/预算/错误一律降级为规则榜，绝不阻塞采集。

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `HOTMEME_LLM_MODE` | `off` | `off / shadow / demote / require_approve`（非法值按 off） |
| `HOTMEME_LLM_MAX_ROUNDS` | 3 | 有界重排轮数（P2 循环） |
| `HOTMEME_LLM_MAX_CALLS` | 18 | 单轮审核调用上限（与 `app/llm.py` 的客户端上限同名变量共用） |
| `HOTMEME_LLM_TARGET` | 10 | 期望 approve 条数；不足时少发并标 shortfall |
| `HOTMEME_LLM_REVIEW_LIMIT` | 50 | 单轮最多送审的候选数（钳位 1–50） |
| `HOTMEME_LLM_BUDGET_SECONDS` | 60 | 审核阶段总耗时闸门，超时即降级规则榜 |

模式矩阵（只作用于 `list=new`）：

| 模式 | 新梗榜行为 | 审计 | 说明 |
|---|---|---|---|
| `off` | 规则榜原样 | 不写 | 默认；零调用 |
| `shadow` | 规则榜原样，只观察 | 写 `llm_review` | 上线前观察通过率/延迟/成本 |
| `demote` | `reject`/`merge` 撤下；`approve`/`unclear`/未审保留 | 写 | 推荐首开，只做剔除 |
| `require_approve` | 仅 `approve` 发布；未审（超出 top50）也撤下 | 写 | 严格榜；0 通过时沿用上一轮（见下） |

**候选**：在 `score_families` 与 status 计算之后，取 `list=new` 且 `status ∈ (hot, observing)` 的条目按 `score` 降序前 50（含 rank=0 的 `comment_backed` 观察项）。证据从 `components.comment` / `components.raw`（或 `raw_signals`）映射为 P2 的 `EvidencePacket` 字段：`age_days`、`seed_origin_year`、`comment_7d_count`、`comment_df_video`、`comment_like_weighted`、`recent_comments`（≤10）、`videos`（≤5）、`danmaku`（≤3）；缺字段不崩，P2 按 `unclear` 兜底。

**撤下口径**：
- rank=0 的新梗项被撤下时 `status=rejected`，不落 `meme_signal`；
- rank>0 的新梗项（可能同时在 `all`）**保留规则 status/rank**，由 `list_items(list=new)` 按 `llm_review.verdict` 过滤，保证 `classic`/`all` 不变；
- `merge` 只记录 `merge_into`，不做物理合并。

**require_approve 0 通过**：本轮 `approved=0` 时，从 DB 找上一轮 `llm_approved>0` 且未降级（`llm_gate_applied`）的 cycle（不限模式），`list=new` 改读该轮并**只保留其中 `approve` 条目**（`cycle.stats.llm_new_list_cycle`）；没有则返回空榜并把 cycle 置 `degraded`，`llm_fallback_reason` 写明「审核未通过」，**绝不退回未审项**。

**降级**：无 key（`not_configured`）、审核阶段超 `LLM_BUDGET_SECONDS`（`timeout_budget`）、P2 的 `budget`/`llm_auth`/`llm_error_streak`、任意异常 → 置 `llm_gate_applied=false`，`list=new` 整轮回本轮规则榜（严格模式不会因缺 verdict 误判成空榜）；原因进 stats；`cycle.status` 不因审核失败而置失败（0 通过且无历史除外）。`classic/all` 从不因降级改变。

**审计表 `llm_review`**（幂等迁移；shadow 也写）：

```
(cycle_id, family_key, verdict, meme_prob, novelty, origin, reason,
 evidence_hash, mode, model, prompt_version, merge_into, created_at)
PRIMARY KEY(cycle_id, family_key)
```

**stats**：`llm_mode`、`llm_calls`、`llm_rounds`、`llm_approved`、`llm_rejected`（reject+unclear+merge）、`llm_reviewed`、`llm_merged`、`llm_shortfall`、`llm_pool`、`llm_gate_applied`、`llm_fallback`、`llm_fallback_reason`；0 通过沿用历史时另有 `llm_new_list_cycle`。`/api/top` 返回 `llm` 摘要（mode/calls/rounds/approved/rejected/gate_applied/fallback/fallback_reason）。

### 5.2 LLM 候选抽取（P5）

在 `discover(docs)` 之前用近期评论补候选池（`pipeline.run_cycle` → `run_llm_extraction`），
与 n-gram 候选按 `normalize_surface(canonical)` 合并；抽取置信度只作 metadata/排序证据，
**不作为审核 approve 依据**。抽取与 P3 审核同轮执行：抽取发生在候选池构建阶段，审核仍在
score/status/rank 之后。

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `HOTMEME_LLM_EXTRACT` | `0` | `1` 开；非法值/空按 0（关闭时零调用、零行为变化） |
| `HOTMEME_LLM_EXTRACT_MAX_CALLS` | 3 | 抽取批预算（有效 1–3，越界回默认；与 P4a 3 批硬上限一致） |
| `HOTMEME_LLM_EXTRACT_MAX_COMMENTS` | 450 | 采样评论上限（有效 1–2000，越界回默认） |
| `HOTMEME_LLM_API_KEY_FILE` | 空 | key 文件路径；`HOTMEME_LLM_API_KEY` 为空时才读，strip 后为空视为未配置 |

**metadata**：candidate 动态属性 / family 的 `llm_extract` 字段（`source/type/meaning/
origin_quote/evidence_ids/confidence/known/prompt_version`），item 上另落
`components.llm_extract` 与 `components.raw.llm_extract`（`components.llm_known` 便于审计）。

**known 处理**：`known=True` 不丢，但与 n-gram/种子合并后标 `llm_known`；当梗性闸门未过时
**不允许**用评论证据旁路入 observing（种子走自己的 `seed_hit` 通道），被拦次数记
`llm_extract_known_blocked`。

**降级路径**（全部只记账、不中断采集）：

| 触发 | `llm_extract_reason` | 行为 |
|---|---|---|
| `HOTMEME_LLM_EXTRACT=0` | `disabled` | 不构造评论、不调用 |
| 无 `kind=="comment"` 文档 | `empty_input` | 不调用 |
| 无 key（env 与 key 文件都空） | `not_configured` | 不构造客户端、零调用 |
| 超时/限流/坏 JSON/schema/鉴权/预算 | 原样（`timeout/rate_limit/bad_json/schema/auth/budget` 等） | 保留已得候选，规则榜照旧 |
| extractor 注入实现抛异常 | `error`（附 `llm_extract_error`） | 空候选，cycle 照常收尾 |

**stats**：`llm_extract_enabled/reason/input/sampled/calls/batches/cache_hits/candidates/
merged/added/known/known_families/known_blocked/skipped/prompt_tokens/completion_tokens/
total_tokens/details`；`details` 保留前 30 条候选（surface/canonical/type/confidence/known/
meaning/origin_quote/evidence_ids）供 shadow 报告。

**shadow**：`tools/shadow_run.py` 固定 `HOTMEME_LLM_MODE=shadow` + `HOTMEME_LLM_EXTRACT=1`，
3 轮写 `/tmp/shadow.db`（可 `--db`），每轮输出 cycle/extract/review/Top10，汇总 JSON 默认
`/tmp/shadow_report.json`；无 key 打印 `not_configured` 并 exit 2，不启动服务、不写生产库。

## 6. 验证

- 固定 fixture 回放：离线跑通，排序稳定。
- 黄金集：30 个已知梗 + 30 个非梗候选，检查判定分与 Top10 精度。
- 真机验收：连续 3 轮，Top10 真梗率 ≥ 60%，无明显同源变体刷屏，单源失败不空榜。

## 7. 风险与合规

| 风险 | 应对 |
|---|---|
| -352 / -412 风控 | spi 取 buvid3、限速 2 rps、评论页分类重试（0.5/1/2s，最多 3 次后仍失败则整页放弃）、失败率 >20% 才降级、回退上一轮 |
| 弹幕 raw-deflate | 显式 `zlib(-15)`，失败回退 gzip |
| Python 默认 CA 失败 | cafile 回退链 `/etc/ssl/cert.pem` → certifi |
| 接口变更 | adapter 隔离 + fixture 回归 |
| 冷启动 | 首轮无历史时跳过 EMA，直接用本轮得分；以广度 + 词典 + 梗源先验排序 |
| 合规 | 公开数据、限速 2 rps、10 分钟低频、不存用户身份、敏感词过滤、单源可停用 |

### 7.1 采集缓存与请求指标

- **弹幕缓存**：24h（`CACHE_TTL_SECONDS`），按 cid 命中。
- **评论缓存**：30 分钟（`HOTMEME_COMMENT_CACHE_SECONDS=1800`，`0` 关闭），按 `(aid, mode, page)` 命中（旧库启动时幂等迁移，历史行补 `mode=3`、`next_cursor=0`）；缓存内容带 `ctime` 与 `next_cursor`。命中时**不产生请求**，但仍按当前 cycle 写入 `text_sample`，`stats.comments` 统计口径不变；warm 轮靠缓存的 `next_cursor` 判断是否继续翻页。
- **cid 持久化**：搜索视频首轮补 `x/player/pagelist` 后回写 `video.cid`；后续轮 `upsert_videos` 用 `COALESCE` 保留，warm 轮 `pagelist_requests=0`。
- `cycle.stats_json` 的请求指标：`requests`（HTTP 总请求数，含 412 后的 curl 兜底与重试）、`endpoint_counts`（ranking/popular/search/pagelist/comments/danmaku/spi/nav，`reply/main` 计入 `comments`）、`comment_requests`（评论页实际拉取次数，不含 curl 兜底，含重试）、`comment_cached`（评论缓存命中页数）、`danmaku_cached`、`pagelist_requests`。
- 评论页失败指标：`comment_page_fail`（最终失败页数，一页只计一次）、`comment_page_retry`（重试次数）、`comment_page_fail_kinds`（按 `http_412/http_5xx/html/empty/non_json/code:-352` 等分类计数）、`comment_page_fail_rate`（失败页/拉取页）、`comment_degraded`（失败率是否 >20%）；`comment_evidence_ready` 仍按 `comments_recent ≥ 200` 判断。
- 评论语料指标：`comments`（本轮保留条数）、`comments_recent`（≤30 天）、`comments_7d`（≤7 天）、`comment_modes`（各 mode 保留条数）、`comment_dupes`（rpid/文本去重数）、`comment_filtered_old`（窗口外丢弃数）、`danmaku`/`danmaku_docs`（弹幕文档数）、`comment_evidence_ready`、`comment_gate_checked`（通过梗性门控、进入评论门控的候选数）、`comment_gate_rejected`（被 P0 门控拒绝的候选数）、`meme_gate_rejected`（被梗性门控拒绝的候选数）、`comment_backed_count`（满足评论证据通道门槛的候选数）、`comment_backed_admitted`（实际经该通道绕过 `MEME_MIN` 入池数）。

## 8. 扩展点

- 接入微博/抖音/头条热榜（adapter 接口已隔离，只需实现 `fetch`）。
- LLM 复核 Top 候选已接入（P3，§5.1）：规则先筛，LLM 只审 `list=new` 前 50 条；后续可评估并发/异步以降低 45 次串行的延迟。
- LLM 候选抽取已接入（P5，§3.1/§5.2，默认关）：拿 key 后先跑 `tools/shadow_run.py` 3 轮，人工核对抽取准确率/幻觉率与审核通过率，再决定是否默认开启。
- 图像/表情包梗识别、BGM 空耳检测。
