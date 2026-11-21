# Phase D 交付报告：family 状态与比例

> 日期：2026-11-22  
> 状态：**完成**（未接 worker、未切线上；工作区改动未 commit/push）  
> 口径修正（2026-11-22 PM 复核）：
> ① `video_count` 改为 15 分钟窗口独立 bvid，30 天证据独立视频降级为 `evidence_video_count`（不参与 count_total/比例/池2门槛）；
> ② `audit_adjustment` 统一为**一次性事件**（只记录「当前已应用的最新审核分」，Phase D 分数更新不再每轮加该项）；
> ③ `cred_base.usage_strength` 改用 **30 天证据计数**（`evidence_comment_count/evidence_danmaku_count/evidence_video_count`）；15 分钟窗口计数只用于比例与 P2 `usage_count`；
> ④ **冷启动例外**：`degraded` 且上一轮没有任何 active family 时不再一刀切冻结，允许按既有初始分规则建 pool1（`cold_start=true`，`status` 仍 degraded，pool2 门槛不放水）。

## 1. 交付文件

| 文件 | 类型 | 作用 |
|---|---|---|
| `meme_info/pools.py` | 新增 | family 观察抽取、cred/score、池迁移、Postgres 读写、纯内存编排 |
| `tools/pool_round.py` | 新增 | CLI：`--window-json / --no-db / --json / --round / --output` |
| `tests/test_pools.py` | 新增 | 39 单元 + 3 Postgres 集成（事务回滚） |
| `meme_info/models.py` | 只增 dataclass | `FamilyObservation` / `Pool1Row` / `Pool2Row` / `FamilyRoundStat` / `PoolTransitionRow` / `FamilyAliasRow` / `PoolRoundResult` |
| `meme_info/discover.py` | **bug 修复 2 行** | `_finalize_candidate` 元话语候选全无 `pubdate` 时 `min()/max()` 空序列抛 `ValueError`（Phase D 的 `WindowTarget` 只有 title/zone 会触发）；改为 `default=0`。不改归并逻辑 |

未改 `window.py` / `video_route.py` / `query.py` / `collect.py` / `heat.py` / `app/` / `static/` / `deploy/` / 迁移 SQL / 设计文档 / 现有测试。

## 2. 算法口径

### 2.1 观察抽取（`build_observations`）

- 15 分钟窗口 `sample.comments/danmaku` → 比例计数：评论按 `id`、弹幕按 `dmid`（缺失回退 `id`）去重；
- **`video_count` 只取 15 分钟窗口命中文档的独立 bvid**；30 天证据 `sample.evidence_comments/danmaku` → 发现/证据，并按 family alias 统计 `evidence_comment_count`（去重评论 id）/`evidence_danmaku_count`（去重 dmid）/`evidence_video_count`（独立 bvid），**不进 count_total/比例/P2**；
- `comment_targets + danmaku_targets` 的 title/zone → `Video` → `extract_meta_mentions`（30 天过滤用 `video_metric.pubdate`，DB 模式自动补）；
- 复用 `discover.extract_candidates` + `merge_families`；上一轮 pool1 的 family 用「归一化别名 → family_key」做**粘性身份对齐**，canonical 抖动时归回旧 key（`_group_families`）；
- 每条 doc 只命中一个 family 一次；同一 family 多别名同文不重复计；
- `evidence_json = {"items": 前5条使用证据, "badge_2x2", "badge_docs", "badge_videos", "evidence_comment_count", "evidence_danmaku_count", "evidence_video_count", "cred_base", "updated_round"}`，2×2 只作徽标（排除该梗自己的元话语视频的 bvid）；
- 上一轮 pool1 `normal/provisional` 但本轮未出现 → 补 `count=0` 观察（窗口计数 0 触发缺席惩罚）；补 0 行仍用上一轮 family 别名回扫 30 天证据，`evidence_*_count`/usage_strength 照常计算，避免窗口缺席把证据可信度清零；`out` 且未出现不补（不占活跃位，重新出现时带 `count_prev` 参与比例）。

### 2.2 计数与特征

```text
# ---- 15 分钟窗口（比例 / P2 usage）----
count_total = comment_count + danmaku_count + video_count   # meta/证据计数不参与比例
usage_count = comment_count + danmaku_count                 # 池2「有使用」门槛（口径统一：窗口评论+弹幕）
video_count = 15 分钟窗口命中文档的独立 bvid 数

# ---- 30 天证据（cred_base 的 usage_strength / 证据展示）----
evidence_comment_count = 30 天证据去重评论 id 命中数
evidence_danmaku_count = 30 天证据去重弹幕 dmid 命中数
evidence_video_count   = 30 天证据独立 bvid 数

cred_base = 100 * clamp(0.30*meta_strength + 0.30*usage_strength
                      + 0.15*spread + 0.10*freshness + 0.15*prior, 0, 1)
# usage_strength 用 30 天证据计数（PM 2026-11-22 复核；window 样本量不压低 cred）
usage_strength = 0.45*sat(evidence_comment_count/40)
               + 0.25*sat(evidence_danmaku_count/200)
               + 0.30*sat(evidence_video_count/12)
spread = 0.50*sat(分区/3) + 0.30*sat(独立视频/10) + 0.20*sat(来源通道/2)
freshness = clamp(1 - 最新证据 age_days/30, 0, 1)
prior = 种子/人工确认 0.9；搜索+热门双命中 +0.1（Phase D 输入无视频来源通道，暂不生效）
meta_strength = dedicated 1.0 / compilation 0.6 / quote 0.3（取最大）
```

### 2.3 分数

```text
新 family（不参与第一轮比例）：
  有使用（count_total>0）        → clamp(cred_base, 0, 80)；<P1_in 存 out
  元话语-only（强元话语 + 专门梗视频/≥2 独立元话语/种子或人工）
                                  → provisional，30 + 5*meta_strength（30–35）
  种子/人工确认 + 强元话语        → max(40, cred_base)，normal，meta_only=true
  其它（弱元话语）                → 不建池行（跳过）

已有 family：
  ratio = (count_now+5)/(count_prev+5)；delta = clamp(5*log2(ratio), -10, +10)
  score = clamp(0.85*prev + 0.15*cred_base + delta, 0, 100)
  本轮 0：prev>0 → -5；prev=0 → -2/轮；zero_streak≥3 额外 -2
  provisional 前 2 轮零计数 → -0.5 且不按 P1_out 撤池；第 3 轮起正常惩罚
  audit_adjustment 只从 pool1 透传，**不再每轮加到 score**：
    Phase F 审核时按 score - old_adjustment + new_adjustment 应用并写回，
    审核效果由 score 自身延续；新审核替换旧 adjustment，不重复累加；不写 audit_event
  count_prev 在轮末更新为 count_now
```

### 2.4 池迁移

```text
P1_in=40 / P1_out=32；P2_in=65 / P2_out=55（滞回，32–40 / 55–65 保持原状态）
普通条目进池2：status=normal 且 score>=65 且 usage_count>0
explainer-only：强元话语 + 种子/人工确认 + 无当前使用，可无使用进池2，每轮最多 2 条
  - meta_only 且 provisional 不放行
  - 已在池2的 explainer 不因 P2_out 立刻淘汰，按「连续 3 轮无新元话语/无使用」降池
pool1 撤出（含 out）→ 同步删除 pool2；迁移写 pool_transition
degraded：冻结分数/比例/池成员，只更新证据/审计字段；新 family 不建池
冷启动例外（degraded 且上一轮无任何 active family）：
  - 不做冻结/零计数惩罚/降池保护（没有旧池可保护）
  - 新 family 按既有初始分规则建池（有 usage 才可能 normal；元话语-only provisional 30–35）
  - pool2 仍按正常门槛（score≥65 + usage>0；explainer-only 规则不变，不放水）
  - result.cold_start=true / stats.cold_start=true；report.status 仍为 degraded
```

### 2.5 Postgres

- 只经 `state.transaction()`；DB 模式事务内 `读上一轮 pool1/pool2 → 计算 → 写回`；
- 写 `pool1`（upsert，含 `updated_at=now()`）、`pool2`（upsert + 删除降池/撤出行）、`family_round_stat`（`UNIQUE(round_id,family_key)` upsert）、`family_alias`（`ON CONFLICT(norm_alias) DO NOTHING`，种子 source=seed）、`pool_transition`（同 round 重算时 delete+insert 幂等）；
- **不写** `audit_event`，**不改** `round`；
- `--no-db` / 无 `DATABASE_URL` 纯内存，同一 `PoolRoundResult` 结构；测试连真实库走事务回滚。

## 3. 验证

### 3.1 本地

- 全量：Phase D 交付时为 **178 passed, 20 skipped**（基线 146/18 + 本 Phase 32/2）；`audit_adjustment` 一次性事件修正后于 **2026-11-22 00:4x** 复跑：`tests/test_pools.py` 32 passed / 2 skipped，全量 **255 passed, 41 skipped**（当前工作区已含 Phase E 提交与 Phase F 并行中的新模块，最终全量由 Phase F 复核）
- `tests/test_pools.py`：**39 passed, 3 skipped**（skip 为无本地 Postgres；含「窗口低/30 天证据高 → cred 不被压低」、计数分离、补 0 行仍用 30 天证据、冷启动例外等测试）

### 3.2 云上 Postgres（124.220.189.74，schema_version=2）

在 `/tmp/phase_d_venv`（隔离 venv，不动线上服务）以 worker DSN 跑：

- `tests/test_pools.py`：**42 passed**（39 单元 + 3 集成，事务回滚；含冷启动 degraded 建池与非冷启动冻结两个 DB 集成）
- 覆盖：pool1/pool2/stat/alias/transition 落库、同轮重跑幂等、`round`/`audit_event` 不变；窗口计数与 30 天证据计数分离（含 evidence_json）；补 0 行仍按 30 天证据算 usage_strength；degraded 冷启动建池落库 vs 非冷启动冻结不降池；
- 跑完 8 张业务表 **行数全 0**、`schema_version=2`；`hotmeme-app`/`hotmeme-worker`/`nginx` 均 active，线上服务未改动；未打印任何连接串/密码。

### 3.3 真机 smoke（Phase C → Phase D，`--no-db`）

实时采样 3 个热门视频（8 分钟窗口样本）：评论 79 / 弹幕 85 / 30 天证据评论 78 / 弹幕 439，样本 degraded；
Phase D 发现 416 个 family，按门槛 **pool1=0、pool2=0**（弱证据默认不进池，符合「默认不接线上、宁缺毋滥」）；CLI `--no-db --json` 正常输出。

## 4. 已知限制 / PM 决策点

1. **evidence_json 形状**：用 dict 包装（`items` + 徽标字段）而非裸 list（DB 默认 `'[]'` 兼容读取）。Phase E adapter/UI 需按此读取。
2. **video_count 口径（已按 PM 复核修正）**：`video_count` = 15 分钟窗口独立 bvid；30 天证据独立视频只作 `evidence_video_count`（evidence_json 内），不参与 `count_total`/比例/P2 门槛。
3. **usage_count 口径（已统一）**：窗口评论+弹幕，不含窗口视频与 30 天证据；只有 30 天证据的 family 不算「有使用」，不能走普通 P2，只能等窗口出现或走 explainer-only。
4. **meta 30 天过滤**：依赖 `video_metric.pubdate`（DB 模式自动补）；`WindowTarget` 无 pubdate，`--no-db` 时元话语视为本轮。线上 DB 模式无此问题。
5. **spread 的「来源通道」**：设计未给精确定义，本实现取「评论/弹幕/视频/meta/种子」五类证据通道（≥2 类即满）。
6. **prior**：known 即种子 → 0.9；`known=0.75` 与 `meme_prob*0.5` 在 Phase D 输入无对应字段，暂未启用；搜索+热门双命中 +0.1 需视频来源通道，暂不生效。
7. **out 行策略**：有使用但初始分 <40 也写 pool1 行（status=out）以保留比例连续性；out 且未出现的 family 不补 0、不写 `family_round_stat`（避免大量休眠行），重新出现时按 out→P1 迁移。
8. **family_round_stat 范围**：只为本轮有观察（含补 0 的 active）的 family 写行；若 PM 要求「每轮 pool1 全量一行」需再加逻辑。
9. **explainer-only**：名额按「本轮新进 ≤2」；人工确认依赖 `audit_state='human_pass'`（Phase F 才产生），当前主要靠 knowledge 种子；LLM 不能单独放行。
10. **pool1 全量 upsert**：每轮把 pool1 全部行（含长期 out）重写一遍；量级小（千行）可接受，后续可优化为只写变化行。
11. **并发**：无跨进程锁，依赖 Phase G 单写者/调度串行。
12. **audit_adjustment 语义（PM 2026-11-22 复核，一次性事件）**：字段只记录「当前已应用的最新审核分」；Phase D 更新公式为 `score=clamp(0.85*prev+0.15*cred_base+delta,0,100)`，**不含** `+audit_adjustment`；Phase F 仍按 `score - old_adjustment + new_adjustment` 应用并写回，审核效果通过 score 延续，避免每轮重复放大。
    - 依赖检查（Phase D 未改其他文件）：`meme_info/audit.py` 的 `compute_audit_update`（`score - old + new`）行为与新口径一致，无需改逻辑；但其模块 docstring 第 30–33 行仍写「Phase D 每轮把 audit_adjustment 当常驻项再加一次」，Phase F 需同步该注释。
    - 展示侧（`report.py` / adapter / UI）只读展示 `audit_adjustment` 数字，语义不受影响；设计文档 `REARCHITECTURE.md` §5.9.1「分数计算只使用当前 audit_adjustment」的表述需 PM 后续同步（Phase D 不改设计文档）。
13. **usage_strength 口径（PM 2026-11-22 复核）**：`cred_base` 的 usage 特征改用 30 天证据计数（证据评论/弹幕/独立视频）；15 分钟窗口计数只用于 `count_total`/比例/P2 `usage_count`。两套计数在 `FamilyObservation` 中字段分离，窗口样本量低不会压低 cred_base。
14. **冷启动例外（PM 2026-11-22 复核）**：夜间/首轮 `degraded` 不应一刀切冻结。判定 `cold_start = degraded and 上一轮无 active（normal/provisional）family`；cold_start 时按正常初始分规则建池（不冻结、不零惩罚），`result.cold_start=true`/`stats.cold_start=true`，`report.status` 仍 degraded；pool2 门槛与 explainer 规则不变。非 cold_start 的 degraded 保持原冻结行为。
15. **discover.py 2 行 bug 修复**：属必要前置（Phase D 的 WindowTarget 无 pubdate）；已保持归并逻辑不变。

## 5. 下一步（Phase E）

- pool2 `heat_score/rank` 计算（当前 NULL）；
- report schema 增加 pool/cred/audit 字段，app adapter/UI 只读 pool2；
- shadow 读 pool1/pool2 全量对比。
