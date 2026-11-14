# P3 · LLM 审核接入采集流水线（list=new 发布门）· 集成与验收报告

> 会话：llm-integrate（`49966ee3-4149-4b07-a316-6d3dcd58360e`），委派方 lead。
> 项目根：`/Users/wanjunqiang/Desktop/hot_meme_search`，HEAD `73a74b3`（P1/P2 已提交）。
> 范围：`app/config.py`、`app/pipeline.py`、`app/store.py`、`app/api.py`、`tests/test_p3_llm_gate.py`、`docs/02-capture-and-scoring.md`、本文件。
> **未修改** `app/llm.py`、`app/review.py`（P2 冻结）、`app/judge.py`、`app/score.py`、`app/extract.py`、`static/*`、`data/*`、`docs/01`、`README`；未 commit、未装依赖、未写生产库、未起服务、未碰 8000。
> 真机库：仅 `/tmp/p3.db`（`HOTMEME_MAX_RPS=2.0`、`HOTMEME_DISABLE_SCHEDULER=1`、`HOTMEME_OFFLINE=0`）。

## 0. 结论（TL;DR）

| 项 | 结果 |
|---|---|
| 集成点 | `run_cycle` 在 score/status/rank/display 之后、`usable` 落库之前调用 `run_llm_gate`；只审 `list=new` top50 |
| 发布门 | `shadow` 只记；`demote` 撤 reject/merge；`require_approve` 只发 approve；`off` 零调用/零变化 |
| 隔离 | 审核只过滤 `list=new`；`all`/`classic` 的成员、名次、status 不变（rank>0 项保留规则 status，靠 `llm_review` 过滤） |
| 降级 | 无 key / 超预算 / timeout / budget / 鉴权 / 连续错误 / 任意异常 → 规则榜 + `llm_fallback_reason`；绝不阻塞采集 |
| 审计 | 新表 `llm_review`（幂等迁移；shadow 也写）；`top_items`/`list_items` 每项返回 `llm_review` |
| 测试 | **全仓 135 passed**（P1/P2 基线 117 + 本会话新增 18，`tests/test_p3_llm_gate.py`） |
| 真机 | `/tmp/p3.db`：冷轮 shadow 84.5s / 168 请求 / 276 评论（28 页 reply/main 非 JSON，采集 degraded）；warm 轮 demote 15.7s / 31 请求，LLM gate 正常（gate_applied=true） |
| 无 key | 环境无 `HOTMEME_LLM_API_KEY`，`build_reviewer()` 返回 `None`；真机用 fake reviewer 证明接线，未发起任何真实 LLM 调用 |
| 质量结论 | **无真实 key，质量效果未验证**；真机使用确定性 fake 启发式，不能作为 LLM 审核质量证据，待 P4 真机 shadow 验证 |

## 1. 集成点与数据流

```
run_cycle
  fetch/collect/discover/merge → compute_usage → judge → score_families   （未动）
  status 计算（含梗性门控/评论门控/comment_backed）                        （未动）
  ranked / rank / display                                               （未动，规则榜先定）
  >>> run_llm_gate(conn, cycle_id, items, by_key, docs, video_meta, usage, reviewer)
        ├─ 候选：list=new 且 status∈(hot,observing)，按 score 降序 top N（默认 50）
        │        含 rank=0 的 comment_backed observing
        ├─ 映射 EvidencePacket 字段（components.comment / components.raw / raw_signals）
        ├─ reviewer 默认 build_reviewer()：off 或无 key → None；否则 ChatClient
        ├─ 总耗时闸门 LLM_BUDGET_SECONDS（线程 join，超时丢弃结果）
        ├─ review_candidates(max_rounds/target/max_calls/limit)
        ├─ 审计写 llm_review（shadow 也写）
        └─ apply_review_gate：只改榜单可见性，不改 score/components
  usable 落库（rank=0 被撤下项 status=rejected，不落库）                    （未动其余逻辑）
  insert_signals / finish_cycle                                          （status 可因审核 0 通过置 degraded）
```

- `run_cycle(conn=None, client=None, reviewer=_DEFAULT_REVIEWER)`：测试注入 fake；默认从 env 构建；`mode=off` 或没有 key 时返回 `None`，连客户端都不构造。
- 候选只来自当前 `items`，不新增 family、不扩候选池、不碰 `candidate` 表。
- `merge` 只写 `merge_into`，不做物理合并。

## 2. 配置（env + config）

| env | 默认 | 代码 | 行为 |
|---|---|---|---|
| `HOTMEME_LLM_MODE` | `off` | `config.LLM_MODE` | `off/shadow/demote/require_approve`；非法值按 off |
| `HOTMEME_LLM_MAX_ROUNDS` | 3 | `config.LLM_MAX_ROUNDS` | 透传 P2 有界轮数 |
| `HOTMEME_LLM_MAX_CALLS` | 18 | `config.LLM_MAX_CALLS` | 单轮审核调用预算（P2 `budget` 触顶 → 规则榜） |
| `HOTMEME_LLM_TARGET` | 10 | `config.LLM_TARGET` | approve 目标；不足少发 |
| `HOTMEME_LLM_REVIEW_LIMIT` | 50 | `config.LLM_REVIEW_LIMIT` | 送审 top N（钳位 1–50） |
| `HOTMEME_LLM_BUDGET_SECONDS` | 60 | `config.LLM_BUDGET_SECONDS` | 审核阶段总耗时闸门 |

`off` 下不读 key、不构造客户端、不写审计、stats 全 0，规则榜与原行为一致。

## 3. 模式矩阵（行为 / 审计 / 降级）

| 模式 | `list=new` 行为 | `llm_review` 审计 | 降级 |
|---|---|---|---|
| `off`（默认） | 规则榜原样，零调用 | 不写 | 不适用 |
| `shadow` | 规则榜原样（只观察，不改 status/rank/成员） | 写（mode=shadow） | 与 demote 相同 |
| `demote` | `reject`/`merge` 撤下；`approve`/`unclear`/未审保留 | 写 | 无 key/超时/预算/鉴权/连续错误/异常 → 规则榜 |
| `require_approve` | 仅 `approve` 发布；`unclear`/`reject`/`merge`/未审全部撤下 | 写 | 同上；0 通过 → 沿用上一轮或空榜 degraded |

补充：

- **rank=0 被撤下**：直接 `status=rejected`，不落 `meme_signal`；
- **rank>0 被撤下**（可能同时在 `all`）：保留规则 `status/rank`，`list_items(list=new)` 按 `llm_review.verdict` 过滤；因此 `all` 和 `classic` 完全不变；
- **demote 未审**：保留（只做剔除）；**require_approve 未审**：撤下（不注入未审项）；
- **0 通过**：`require_approve` 从 DB 找上一轮 `llm_approved>0` 且未降级（`llm_gate_applied`）的 cycle（不限模式），新梗榜改读该轮并只保留 `approve` 条目（`cycle.stats.llm_new_list_cycle`）；没有则返回空榜，cycle `status=degraded`，`llm_fallback_reason="no_approved: 审核未通过（0 条 approve）"`；
- `classic` 不做任何 LLM 过滤；`all` 仍按 `rank` 返回。

## 4. 审计与观测

`llm_review`（`CREATE TABLE IF NOT EXISTS`，重复 `init_db` 幂等；早期缺 `merge_into` 的草稿库补列）：

```
cycle_id, family_key, verdict, meme_prob, novelty, origin, reason,
evidence_hash, mode, model, prompt_version, merge_into, created_at
PRIMARY KEY(cycle_id, family_key)
```

- 写入用 `ON CONFLICT ... UPDATE`，同轮重跑不重复累积；shadow 同样写。
- `top_items` / `list_items` LEFT JOIN 该表，每个条目新增 `llm_review`（verdict/reason/meme_prob/novelty/origin/merge_into/mode/model；未审核为 `null`）。
- `cycle.stats` 新增：`llm_mode`、`llm_calls`、`llm_rounds`、`llm_approved`、`llm_rejected`（reject+unclear+merge）、`llm_reviewed`、`llm_merged`、`llm_pool`、`llm_shortfall`、`llm_gate_applied`、`llm_fallback`、`llm_fallback_reason`；沿用历史时另有 `llm_new_list_cycle`。
- `/api/top` 新增响应字段 `llm`（mode/calls/rounds/approved/rejected/reviewed/shortfall/gate_applied/fallback/fallback_reason/new_list_cycle）；原有字段不变。

## 5. 降级路径（绝不阻塞采集）

| 触发 | `llm_fallback_reason` | 结果 |
|---|---|---|
| 非 off 模式但无 key / 构造失败 | `not_configured` | 不构造客户端，`llm_gate_applied=false`，规则榜，calls=0 |
| 审核阶段超过 `LLM_BUDGET_SECONDS` | `timeout_budget` | 丢弃本轮审核结果，`llm_gate_applied=false`，规则榜 |
| reviewer 抛任意异常 | `<ExceptionType>: <msg>` | 规则榜，`llm_gate_applied=false`，calls=0，不写审计 |
| P2 `degraded_reason=budget / llm_auth / llm_not_configured / llm_error_streak / candidate_leak` | 原样 | 已审部分写审计但不应用，`llm_gate_applied=false`，规则榜 |
| P2 `pool_exhausted` / `rounds_exhausted` | 不降级 | 正常 shortfall，已审结果照常应用（不足 10 条少发） |
| `require_approve` 0 通过且有历史 | `no_approved_previous_cycle` | 新梗榜沿用上一轮已审核结果 |
| `require_approve` 0 通过且无历史 | `no_approved: 审核未通过（0 条 approve）` | 新梗榜空，cycle degraded |
| 审计写失败 | `audit_error: <Type>` | 规则榜（不中断采集） |

任何异常都只影响审核阶段；采集/落库/其它榜单照常。回滚 = `HOTMEME_LLM_MODE=off`，无数据迁移。

## 6. 测试

```bash
.venv/bin/python -m pytest -q     # 135 passed, 1 warning
```

新增 `tests/test_p3_llm_gate.py` 18 个用例：

1. `off` 零调用、无审计、榜单不变；
2. 无 key 时 `build_reviewer()` 返回 None；
3. `shadow` 不改榜但落审计，条目带 `llm_review`；
4. `demote` 撤 reject，`all`/`classic` 不变（rank>0 保 status）；
5. `demote` 对 rank=0 项直接置 `status=rejected`；
6. `require_approve` 只留 approve，条目带 verdict/reason；
7. `require_approve` 未审项也撤下；
8. `require_approve` 0 通过且无历史 → 空榜 + degraded + `no_approved`；
9. `require_approve` 0 通过 → 沿用上一轮已审核结果；
10. 沿用上一轮时只带 approve（不限上一轮模式）；
11. 异常降级规则榜；
12. 审核超预算降级（线程闸门）；
13. 无 key 默认 reviewer 降级且 0 调用；
14. `require_approve` 无 key 降级时新梗榜回规则榜（gate_applied=false）；
15. 真实 `review_candidates` + 假 `chat_json` 接线（audit/model/prompt_version/证据字段）；
16. P1 证据 → `EvidencePacket` 字段映射；缺字段不崩（unclear 兜底）；
17. `llm_review` 幂等迁移 + 同轮覆盖写；
18. API `/api/top?list=new` 返回审核后条目，`all`/`classic` 不变，响应含 `llm` 摘要。

P1/P2 基线 117 个全部保持通过。

## 7. 真机/离线一轮（`/tmp/p3.db`，2 rps，无 key，fake reviewer）

环境：`HOTMEME_DB=/tmp/p3.db HOTMEME_MAX_RPS=2.0 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_OFFLINE=0`；`HOTMEME_LLM_API_KEY` 未设置。fake reviewer 是确定性启发式（纯 ASCII/数字或过短 reject，其余 approve），**仅验证接线，不代表 LLM 判断质量**。

### 7.1 第 1 轮（冷，shadow）

| 项 | 值 |
|---|---|
| cycle / status | #1 **degraded**（采集错误，非 LLM；`llm_fallback=false`） |
| 耗时 / 请求 | 84.5s / **168** |
| 评论 / ≤7 天 | **276** / 166（`comment_requests=92`，28 页 `reply/main` 返回非 JSON，疑似风控） |
| videos / candidates / families | 377 / 555 / 423 |
| usable / comment_backed | 44 / 36 |
| LLM stats | mode=shadow, calls=**19**, rounds=1, approved=**10**, rejected=**9**, fallback=**false**, model=`fake-heuristic` |
| 审计 | approve 10 / reject 9 |
| 榜单 | `new` 40 条（含 rank=0 观察项），`all` 8 条种子不变 |

说明：第 1 轮 120 评论页中 92 页成功、28 页返回 HTML/非 JSON（与 P1 记录的风控现象一致），因此评论 276 条、cycle degraded；这是采集侧问题，不是审核门导致。审核 19 条全部正常写审计，shadow 未改榜。

### 7.2 第 2 轮（warm，demote，当前代码复跑）

| 项 | 值 |
|---|---|
| cycle / status | #3 **done** |
| 耗时 / 请求 | 15.7s / **31**（评论 92 页全缓存，0 评论请求） |
| 评论 / ≤7 天 | 276 / 166 |
| usable / comment_backed | 35 / 36 |
| LLM stats | mode=demote, calls=**19**, rounds=1, approved=**10**, rejected=**9**, reviewed=**19**, gate_applied=**true**, fallback=**false** |
| 审计 | approve 10 / reject 9 |
| 生效 | `new` 中 9 条 reject 被撤下（`ai`/`on`/`co`/`10`/`202`/`ge`/`to`/`00`/`23` 等），10 条 approve 保留；`all` Top8 种子及名次完全不变 |

结论：假的审核结论可以只改变 `list=new`；`all` 的 8 条种子（抽象/哈基米/整活/咕咕嘎嘎/吃瓜/嘉豪/离谱/那咋了）与排名不变；被剔除的纯 ASCII/数字噪音不再出现在新梗榜。**由于 fake 启发式把「也不/还没/看完」等常用短语也 approve，新梗榜质量仍不可接受；这正是需要 P4 用真实 LLM 验证的部分。**

产物：`/tmp/p3.db`、`/tmp/p3_run.json`（冷轮+第 2 轮）、`/tmp/p3_run3.json`（当前代码 warm 复跑）、`/tmp/p3_run.py`、`/tmp/p3_run3.py`（均在 /tmp，不写生产库）。

## 8. 未配置 LLM 状态（如实）

- 本机环境未设置 `HOTMEME_LLM_API_KEY`；真机两轮均注入 fake reviewer，**没有发出任何真实 LLM 请求**（无 key 时 `build_reviewer()` 返回 None，默认路径会在 stats 写 `not_configured`）。
- 因此本报告只能证明：
  1. 配置/候选构造/调用/审计/发布门/降级链路已接通；
  2. 模式矩阵与 `classic/all` 隔离由 133 个离线测试覆盖；
  3. 真实模型的 approve 率、误杀率、P95 延迟、成本**均未验证**。
- 上线顺序仍按 P2 设计：先 `shadow` 跑 3–5 轮，看 approve 率与延迟，再开 `demote`，最后评估 `require_approve`。

## 9. 已知风险与 P4 建议

| # | 风险 | 说明 / 建议 |
|---|---|---|
| 1 | 真实质量未知 | 无 key；fake approve 了常用短语。P4 必须用真实 key 做 shadow，人工抽查 approve/reject 各 20 条 |
| 2 | 18 调用预算 vs target=10 | 若 approve 率低，P2 会在第 2 轮触顶 `budget` → 本轮规则榜（符合“预算降级”，但会丢失部分审核结果）；上线前按 shadow 的 approve 率校准 `MAX_CALLS`/`TARGET` |
| 3 | 延迟 | 真实串行 18 次 × P95 4s ≈ 72s，可能触 60s 总预算 → `timeout_budget` 规则榜；建议 P4 实测后调预算或并发 |
| 4 | 候选质量 | 本轮 `list=new` 仍以高频子串为主；demote 只保证“被 reject 的不上”，不保证“剩下的都是梗”；需要 require_approve 或更严的规则前置 |
| 5 | 风控 | 冷轮 28 页 reply/main 非 JSON；真机连续跑轮需冷却（P1 已记录），不要靠降页数掩盖 |
| 6 | 审计增长 | `llm_review` 每轮 ≤50 行，暂不清理；P4 可纳入 `prune()` |
| 7 | 上一轮沿用 | 取最近一个 `llm_approved>0` 的 cycle 并只带 approve 条目；从未有过 approve 时首轮 0 通过会空榜 degraded，属预期 |

## 10. 变更文件

| 文件 | 改动 |
|---|---|
| `app/config.py` | `LLM_MODE/LLM_MAX_ROUNDS/MAX_CALLS/TARGET/REVIEW_LIMIT/BUDGET_SECONDS`，env 安全解析 + 非法 mode 归 off |
| `app/pipeline.py` | `run_llm_gate` / `review_pool` / `review_candidate` / `apply_review_gate` / `build_reviewer` / 预算线程闸门；`run_cycle(reviewer=...)`；stats 初始化与 gate 调用 |
| `app/store.py` | `llm_review` 表 + 幂等迁移；`insert_llm_reviews` / `llm_review_map` / `cycle_stats` / `previous_approved_cycle`；`top_items`/`list_items` LEFT JOIN 并返回 `llm_review`；`list=new` 门过滤与上一轮沿用 |
| `app/api.py` | `/api/top` 回显 `llm` 摘要（原有字段/行为不变） |
| `tests/test_p3_llm_gate.py` | 新增 18 个用例（见 §6） |
| `docs/02-capture-and-scoring.md` | §5/§5.1 发布门、模式、降级、审计、stats；§8 扩展点更新 |
| `docs/pm/P3-INTEGRATION-REPORT.md` | 本文件 |

未改 P2 冻结文件；未 commit；未装依赖；`static/*`、`data/*`、`docs/01`、`README` 未动。

> 注：`docs/pm/BOARD.md` 在本会话开始前已有未提交改动（§15 P3 计划），属于委派方/其它会话产物；本会话未编辑该文件。
