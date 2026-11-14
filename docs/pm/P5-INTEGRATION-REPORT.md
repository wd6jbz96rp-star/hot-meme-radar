# P5 · LLM 候选抽取接入流水线 · 集成与验收报告

> 会话：llm-pipeline（`4556afb6-ad99-446b-8428-917bcb7ef1be`），委派方 lead。
> 项目根：`/Users/wanjunqiang/Desktop/hot_meme_search`，HEAD `7db775e`。
> 范围：`app/pipeline.py`、`app/config.py`、`app/llm.py`（仅 key-file/超时小改）、
> `tests/test_p5_llm_pipeline.py`、`tools/shadow_run.py`、`docs/02-capture-and-scoring.md`、本文件。
> **未修改** `app/llm_extract.py`、`app/review.py`、`app/judge.py`、`app/score.py`、`app/extract.py`、
> `static/*`、`data/*`、`docs/01`、`README`；未 commit、未装依赖、未起服务、未碰 8000、未写生产库。
> 真机 shadow **未执行**：环境无 key（`~/.hotmeme_llm_key` 不存在、`HOTMEME_LLM_API_KEY` 未设置），
> 按任务要求只跑 fake 注入集成测试并如实报告阻塞。

## 0. 结论（TL;DR）

| 项 | 结果 |
|---|---|
| 接入点 | `run_cycle` 在 `collect_docs` 之后、`discover(docs)` **之前**调用 `run_llm_extraction`；抽取→合并→merge_families→审核同轮完成 |
| 合并口径 | 按 `normalize_surface(canonical)`；已存在只补 metadata、不重复计数；新候选按 evidence_ids 回映射评论/视频算 `count/df_video/df_zone` |
| known | 不丢、补 `llm_known`，**不允许**走「评论证据绕过梗性闸门」的新梗旁路；种子仍走自己的 `seed_hit` 通道 |
| 降级 | 关闭/无评论/无 key/失败 → 空候选 + `llm_extract_reason`；任何异常不中断采集 |
| Key | 新增 `HOTMEME_LLM_API_KEY_FILE`（仅当 env key 为空时读、strip、空=未配置）；`redacted()`/`repr(LlmConfig)` 不泄露 key |
| 测试 | **全仓 207 passed**（基线 192 + 本会话新增 15，`tests/test_p5_llm_pipeline.py`） |
| fake 集成 | 15/15 通过：关闭/空输入/无 key/失败降级/合并去重/known 旁路/同轮审核/metadata 落库/shadow 无 key 退出 2 |
| 真机 shadow | **阻塞：key 未配置**；`tools/shadow_run.py` 已就绪，拿 key 后可直接 3 轮 |
| 质量结论 | **未验证**：无真实 key，抽取质量/真实成本/3 轮 Top10 对比均未测；不得把 fake 结果当质量证据 |

## 1. 接入点与数据流

```
run_cycle
  fetch / collect_docs                                         （未动）
  >>> video_meta = {bvid: video}
  >>> run_llm_extraction(docs, video_meta, extractor=...)
        ├─ 只取 kind=="comment" 的 TextDoc
        │    text/likes/bvid/zone + 动态 ctime/rpid；title 由 bvid 关联 video_meta
        ├─ llm_extract.run_extraction(comments, llm, max_calls, max_comments)
        │    近 30 天 / 高赞 / 文本去重 / ≤450 条 / ≤3 批；无 key 返回 not_configured
        ├─ evidence_ids 稳定 id：rpid；无 rpid → h:<sha12(normalize_text)>
        └─ candidates + comment_index + llm_extract_* stats
  discover(docs)                                               （未动）
  >>> merge_llm_candidates(candidates, llm_candidates, comment_index)
        ├─ 已存在（normalize_surface(canonical) 命中）：只补 metadata，不重复计数
        └─ 新候选：Candidate(surface, norm=key, count/df_video/df_zone 来自 evidence_ids,
                             kinds={"llm_extract"}, zones=...)
  merge_families                                               （未动）
  >>> attach_llm_metadata(families, candidates)
        └─ family["llm_extract"] / family["llm_known"]（多成员按置信度聚合）
  compute_usage / judge / score_families / status / rank        （未动）
        ├─ item["components"]["llm_extract"|"llm_known"]
        ├─ item["raw_signals"]["llm_extract"|"llm_known"]（随 components 落库）
        └─ known：bypass = comment_backed and not llm_known
  run_llm_gate（P3）                                            （未动）
```

- 抽取在候选池构建阶段，**不在** judge/review 之后；P3 审核仍只作用于 `list=new` top50。
- LLM 候选与 n-gram 候选同权进 `merge_families`/`compute_usage`/`judge`/`score`；抽取置信度
  只作 metadata 与排序证据，不作为审核 approve 依据。
- `run_cycle(..., extractor=...)` 新增可选注入：默认 `DEFAULT_EXTRACTOR` 走 env 构建的
  `run_extraction`；也可注入 `chat_json` 客户端或 callable（返回 report dict / candidate list）。
  `extractor=None` 显式禁用（`reason="no_extractor"`），仅供测试/运维。

## 2. 配置

| env | 默认 | 代码 | 行为 |
|---|---|---|---|
| `HOTMEME_LLM_EXTRACT` | `0` | `config.LLM_EXTRACT_ENABLED` | `1/true/yes/on` 开；`0/false/no/off` 关；非法值回默认 0 |
| `HOTMEME_LLM_EXTRACT_MAX_CALLS` | 3 | `config.LLM_EXTRACT_MAX_CALLS` | 有效范围 1–3；非整数/越界回默认 3（与 P4a 3 批硬上限一致） |
| `HOTMEME_LLM_EXTRACT_MAX_COMMENTS` | 450 | `config.LLM_EXTRACT_MAX_COMMENTS` | 有效范围 1–2000；非整数/越界回默认 450 |
| `HOTMEME_LLM_API_KEY_FILE` | 空 | `LlmConfig.from_env()` | 仅当 `HOTMEME_LLM_API_KEY` 为空时读；`expanduser`、`strip`；缺失/空/不可读=未配置 |

关闭时零调用、零请求、stats 仍带全量 `llm_extract_*` 键（`reason="disabled"`），便于监控和
与旧库兼容。`HOTMEME_LLM_API_KEY` 优先于文件；env key 也做 strip。

## 3. metadata 与合并去重

**candidate**（`extract.Candidate` 的动态属性，`extract.py` 冻结不加字段）：

```json
{"source": "llm_extract | n-gram+llm_extract", "type": "词|句式|谐音|缩写|名场面",
 "meaning": "…", "origin_quote": "…", "evidence_ids": ["…"],
 "confidence": 0.0, "known": false, "prompt_version": "p4a-extract-v1"}
```

缺字段一律默认（`type="词"`、空串、`[]`、`0.0`、`known=False`），不抛。

**family**：`family["llm_extract"]`（多成员按 confidence 取最高、meaning/origin 取首个非空、
evidence 并集 ≤5、known 取或）与 `family["llm_known"]`。

**item / 落库**：`item["components"]["llm_extract"]`、`item["components"]["llm_known"]`、
`item["raw_signals"]["llm_extract"|"llm_known"]`；随 `meme_signal.components_json` 持久化，
便于 P5 后按来源评估召回/准确率（测试断言了落库 JSON）。

**去重规则**：

- 键 = `normalize_surface(canonical)`，空则退回 `normalize_surface(surface)`；
- 已在 n-gram/种子池：`count/df_video/df_zone` **原样保留**，仅补 metadata，`source` 标
  `n-gram+llm_extract`；同 canonical 的重复 LLM 项只处理一次；
- 新候选：`count=len(evidence 命中的评论)`、`df_video=去重 bvid 数`、`df_zone=去重 zone 数`；
  `evidence_ids` 指向的评论不存在时跳过该 id（不崩），允许 count=0（保守，不编造）；
- 新候选的 `count` 只用于候选池初始权重/落库；后续热度信号仍由 `compute_usage` 扫全量
  `docs` 重算，不进最终 score 的重复计数。

## 4. known 处理（设计说明）

P4a 已用种子表把命中 canonical/alias 的候选标 `known=True` 且 canonical 对齐种子写法。
P5 的处理：

1. **不丢弃**：仍合并进候选池，补 `llm_known`/metadata，保留证据；
2. **不走新梗旁路**：`bypass = comment_backed_candidate(...) and not llm_known`。即梗性闸门
   `passes_meme_gate` 未过时，known 候选不能借「近 30 天 ≥3 评论 / ≥2 视频 / 7 天 ≥1」的
   评论证据通道进 `observing`；种子/known 已有 `seed_hit` 的正常通道，不受影响。
   被拦次数记 `llm_extract_known_blocked`；
3. 新旧榜分类 `classify_list` 继续按种子 origin 年判断（>=2024 的近期种子仍可进 new），
   **没有**为了 known 强行改 classic，避免误伤 2024+ 新种子。

## 5. 降级路径（绝不阻塞采集）

| 触发 | `llm_extract_reason` | 行为 |
|---|---|---|
| `HOTMEME_LLM_EXTRACT=0` / 非法值 | `disabled` | 不收集评论、不构造客户端、零调用 |
| 无 `kind=="comment"` 文档 | `empty_input` | 零调用，空候选 |
| 无 key（env 与文件均空） | `not_configured` | `run_extraction` 不构造 `ChatClient`、零请求 |
| 注入 `extractor=None` | `no_extractor` | 显式关闭（测试/运维） |
| 超时/限流/网络/HTTP/坏 JSON/schema/鉴权/预算 | 原样（`timeout/rate_limit/network/http/bad_json/schema/auth/budget`） | 保留已得候选（预算截断时），规则榜照旧 |
| extractor 抛任意异常 | `error`（`llm_extract_error` 记录类型+前 160 字） | 空候选，cycle 照常收尾 |
| report 结构异常 | `error` | 空候选，不崩 |

stats：`llm_extract_enabled/reason/input/sampled/calls/batches/cache_hits/candidates/merged/
added/known/known_families/known_blocked/skipped/prompt_tokens/completion_tokens/total_tokens/details`。
`details` 保留前 30 条候选摘要供 shadow 报告。

## 6. Key 文件与保密

- 读取顺序：`HOTMEME_LLM_API_KEY`（strip 后非空）→ `HOTMEME_LLM_API_KEY_FILE`（`expanduser`、
  `strip`）；文件缺失/空/不可读/编码错误 → 视为未配置，不抛。
- `LlmConfig.api_key` 设为 `repr=False`：`repr(LlmConfig(...))` 不再带 key；`redacted()` 仍返回
  `"***"`；异常消息/日志不含 key；本会话未读取或打印任何真实 key。
- 测试覆盖：文件读取+strip、env 优先、空/缺失文件未配置、`redacted`/`repr` 无明文。

## 7. 测试

```bash
.venv/bin/python -m pytest tests/test_p5_llm_pipeline.py -q   # 15 passed
.venv/bin/python -m pytest -q                                 # 207 passed, 1 warning
```

基线 `7db775e` 实测 **192 passed**；新增 `tests/test_p5_llm_pipeline.py` 15 例，全仓 **207 passed**。

| 组 | 用例 | 断言要点 |
|---|---|---|
| key 文件 | `test_key_file_is_read_and_stripped`、`test_env_key_takes_precedence_over_key_file`、`test_missing_or_blank_key_file_is_not_configured` | strip、env 优先、空/缺失=未配置、redacted/repr 无明文 |
| 配置 | `test_extract_config_defaults_and_invalid_values` | 非法值回 0/3/450；合法值 1/2/600 生效 |
| 关闭/空输入 | `test_disabled_does_not_call_extractor_and_still_has_stats`、`test_zero_comments_reports_empty_input` | 0 调用、reason=disabled/empty_input、stats 键齐全、gate update 不覆盖 |
| 无 key | `test_no_key_reports_not_configured_without_network` | helper 与整轮均 `not_configured`、calls=0、规则榜照旧 |
| 失败降级 | `test_extractor_failure_degrades_to_rule_board` | `error` + 异常类型、空候选、cycle 正常收尾 |
| 合并/去重 | `test_existing_candidate_gets_metadata_without_recount`、`test_new_candidate_counts_from_evidence_and_dedupes` | 已有 count/df 不变、source=`n-gram+llm_extract`；新候选 count/df 由 evidence 计算；重复/坏项 skipped |
| metadata | `test_metadata_defaults_and_family_attach` | 缺字段默认；family 聚合 known/meaning |
| known | `test_known_does_not_use_comment_backed_bypass` | known：blocked=1、admitted=0；对照非 known：admitted=1、blocked=0 |
| 同轮 | `test_extraction_and_review_run_in_same_cycle` | extractor/reviewer 各 1 次；`llm_extract_*` 与 `llm_approved` 共存；`meme_signal.components_json` 有 metadata |
| shadow 脚本 | `test_shadow_run_without_key_exits_2_and_does_not_create_db`、`test_shadow_run_refuses_production_db` | 无 key exit 2、不建库/不写 JSON；生产库路径 exit 2 |

**fake 集成结果**（确定性注入，不代表 LLM 质量）：关闭/空输入/无 key 均 0 调用；失败注入后
规则榜照常；同轮注入 extractor + reviewer 时抽取候选进入 `list=new` 审核池并 approve；
known 候选被拦新梗旁路，非 known 对照成功经评论旁路入 observing。

额外做了一次 **3 轮 fake 注入连跑**（离线 `BiliClient` + 注入 extractor/reviewer，非 LLM、
非真机、不联网、`/tmp/p5_fake3.db`）：每轮 `llm_extract_reason=ok`、新增候选 1、审核
approve 1、`llm_gate_applied=true`、Top1「新梗复活」86.7 分 `list=new`、cycle `done`；
3 轮 reviewer 共 3 次调用（每轮 1 次）。该结果只验证「抽取→合并→审核→落库」跨轮不崩、
审计表按轮写入，**不能作为真实抽取质量或成本证据**。

## 8. 真机 shadow（未执行，明确阻塞）

**阻塞原因**：本会话环境无 `~/.hotmeme_llm_key`，也未设置 `HOTMEME_LLM_API_KEY`；
`LlmConfig.from_env().configured == False`。按任务要求不联网、不伪造，因此**没有真实 3 轮数据、
没有真实调用数/成本/耗时、没有真实 extract/review/Top10 对比**。

已就绪的脚本 `tools/shadow_run.py`：

```bash
# 1) 用户把 DeepSeek key 写入文件（不要贴聊天）
printf '%s\n' "$YOUR_KEY" > ~/.hotmeme_llm_key && chmod 600 ~/.hotmeme_llm_key

# 2) 3 轮 shadow（脚本内部强制 HOTMEME_LLM_MODE=shadow、HOTMEME_LLM_EXTRACT=1、
#    HOTMEME_OFFLINE=0、HOTMEME_DISABLE_SCHEDULER=1、HOTMEME_MAX_RPS=2）
HOTMEME_LLM_API_KEY_FILE=~/.hotmeme_llm_key \
  .venv/bin/python tools/shadow_run.py --rounds 3 --db /tmp/shadow.db
```

- 默认 DB `/tmp/shadow.db`（可 `--db`），脚本**拒绝** `data/` 下生产库（exit 2）；
- 无 key 时打印 `not_configured` 并 **exit 2**，不建库、不发请求（已实测）；
- 每轮 stdout 输出一行 JSON：cycle stats、`extract`（calls/input/candidates/merged/known/
  skipped/reason/details）、`review`（approved/rejected/verdicts/approved_items）、`top10`、
  `cost`；汇总写 `/tmp/shadow_report.json`（可 `--json`）；
- 成本：抽取 token 来自 `run_extraction` 审计，审核 token 来自注入 `ChatClient.stats` 增量，
  按 ¥1/¥2 每百万 token 估算并明确标注「量级估算，不是报价」。成本基线参考：抽取 3 批
  ≈ ¥0.033/轮（P4a 估算），审核最坏 ≤18 调用 ≈ ¥0.037/轮；3 轮约 ≤¥0.21（未实测）。

拿 key 后需要补的验收：3 轮真实 extract/review/Top10 对比、实际调用数/耗时/token、幻觉率
（`surface` 校验丢弃比例）、approve 率与人工抽查，再决定 `demote` / `require_approve`。

## 9. 验收清单

- [x] 接入点：`discover` 之前调用 `run_extraction` + `evidence_ids` 回映射
- [x] metadata：candidate/family/item 三级，`source="llm_extract"`/`n-gram+llm_extract`，落库
- [x] 合并去重：`normalize_surface`，已有不重复计数，新增按证据计算
- [x] known：标记 + 拦新梗旁路（种子通道保留），有对照测试
- [x] 降级：关闭/无评论/无 key/失败/异常，stats `llm_extract_reason` 等
- [x] 配置：3 个 env、默认值、非法值回默认
- [x] Key 文件：读取/strip/优先/不泄露
- [x] `run_cycle` extractor 注入（默认 env）
- [x] 测试：新增 15，全仓 207 passed
- [x] `tools/shadow_run.py`：3 轮、/tmp DB、每轮 JSON、无 key exit 2、拒绝生产库
- [ ] 真机 shadow 3 轮：**阻塞（无 key）**
- [x] 未 commit / 未装依赖 / 未改冻结文件
