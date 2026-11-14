# 项目看板 · hot_meme_search

PM：lead（session-2f72c964-af87-4c0d-a948-8228bb4b6463）
基线：git `84a4b28`（2026-11-14 14:12 快照）
当前阶段：v1.0 已跑通 → v1.1 加固与增强，先审计后改造

## 1. 现状快照

| 项 | 状态 | 证据 |
|---|---|---|
| 单元/离线测试 | ✅ 23 passed | `.venv/bin/python -m pytest -q` |
| 真机采集 | ✅ 已跑 2 轮 | cycle#2：377 视频 / 90 评论 / 16444 弹幕 / 1053 梗族 / 86 请求 / 43.0s / 0 错误 |
| 服务 | ⏸ 当前未监听 :8000 | 最后 DB 写入 14:11，进程已退出 |
| 数据 | `data/hotmeme.db` 3.1MB | 保留最近两轮，含 Top10 与历史信号 |
| 文档 | README + docs/01-02 + data/README | 14:07 完成 |
| 版本控制 | ✅ 刚建 git 基线 | 无远端；用于 diff / 回滚 |

风险：项目此前无 git；多会话并行前已补基线，但**并行只允许文件级互斥**，不做自动合并。

## 2. 角色与文件所有权

| 会话 | 职责 | 可写路径 | 状态 |
|---|---|---|---|
| 主线（另一 DSH 会话） | 业务功能实现，当前代码所有者 | `app/*.py`、`app/static/index.html`、`docs/01-02`、`README.md`、`data/*` | 运行中/待确认 |
| lead（PM） | 计划、接口、任务拆解、集成验收 | `docs/pm/**` | 进行中 |
| qa-verify | 验收回归（只读） | `docs/pm/QA-VERIFY.md` | 本波 |
| qa-quality | 榜单质量审计（只读） | `docs/pm/QA-QUALITY.md` | 本波 |
| async-design | 异步改造设计（只读） | `docs/pm/ASYNC-DESIGN.md` | 本波 |

**冻结规则（本波有效）**

1. 除主线外，任何会话**不得修改** `app/*.py`、`app/static/index.html`、`docs/01-02`、`README.md`、`data/*`、`tests/*`。
2. 不重启服务、不跑真机采集、不写 `data/hotmeme.db`、不装依赖、不 `git commit`。
3. 需要跑流水线时只用 `HOTMEME_OFFLINE=1` + 临时 DB（`HOTMEME_DB=/tmp/...`）。
4. 只读访问生产库用 `sqlite3` URI `file:data/hotmeme.db?mode=ro`。
5. 结论必须带证据（命令、文件:行号、DB 查询结果），区分「已验证」与「推测」。

## 3. 工作包（v1.1）

| WP | 内容 | 依赖 | 验收标准 | 负责 |
|---|---|---|---|---|
| WP0 | 验收回归 + 契约核对 | 无（只读） | PASS/FAIL 表 + P0/P1/P2 缺陷清单 | qa-verify |
| WP1 | 榜单质量审计 | 无（只读） | 逐条 Top10 判定 + 量化指标 + 规则改法 | qa-quality |
| WP2 | 异步/采集性能设计 | 无（只读） | 分阶段耗时预算 + 方案对比 + 文件级实施清单 | async-design |
| WP3 | 采集并发实现 | 主线冻结 `http.py/bili.py/pipeline.py` | 轮耗时 ≤ 35s、0 新增错误、测试全绿 | 待派（实现会话） |
| WP4 | 榜单质量调优 | 主线冻结 `judge.py/score.py/data` | 真梗率 ≥ 70%、常青/新闻误入 ≤ 1 条、回归测试 | 待派 |
| WP5 | 看板增强 | 主线冻结 `static/index.html` | 趋势图 + 空/错/降级态 + 移动端；无 CDN 依赖 | 待派 |

## 4. 接口契约（不得破坏）

- `GET /api/top?limit=10` → `{status, cycle, top[], health, running, error}`；`top` 项含 `rank, family_key, canonical, score, delta, type, first_seen, sample, components{exposure,engagement,breadth,velocity,meme,decay,reasons}, is_new`。
- `GET /api/meme/{family_key}/history?limit=60` → `{family_key, history[]}`。
- `GET /api/health`、`GET /api/stream`（SSE，`data: {snapshot}`）、`POST /api/refresh`（`HOTMEME_TOKEN` 鉴权，409 表示已在跑）。
- 配置键见 `app/config.py`，环境变量前缀 `HOTMEME_`。
- SQLite 表结构见 `app/store.py` 的 `SCHEMA`。

## 5. 已知问题（待审计确认）

- `api.py`：`STATE["running"]` 的检查与置位分离，`_scheduler` 与 `/api/refresh` 可能并发跑两轮。
- `COMMENT_CACHE_SECONDS` 定义但未被引用（评论无缓存），请求数可再降。
- 真机 Top10 含「吃瓜/麻了」等常青词、「抽象/整活」等泛词，与 docs/01 的新鲜度口径有偏差。
- `components.decay` 在 API 中可能缺失（看板已兜底显示 `-`）。
- 无 README 之外的部署/运维说明；无 CI。

## 6. 决策日志

| 日期 | 决策 | 理由 |
|---|---|---|
| 11-14 | 并行第一波只做**只读审计 + 设计**，不动业务代码 | 主线仍在写 `app/*.py`，避免冲突 |
| 11-14 | 异步改造范围限定「采集段受限并发 + 分端点限速桶」，CPU 与 SQLite 落库保持串行 | 全局限速下 naive async 无收益；SQLite 单写者 |
| 11-14 | 先加评论缓存（已定义未用）再谈并发 | 砍请求比提并发更安全、收益更确定 |
| 11-14 | 使用 git 基线 + 文件级所有权 | 无 VCS 下多会话并行不可回滚 |

## 7. 审计结论（14:20，三路只读会话）

报告：`docs/pm/QA-VERIFY.md`、`docs/pm/QA-QUALITY.md`、`docs/pm/ASYNC-DESIGN.md`

### 7.1 阻断级

| 编号 | 结论 | 证据 |
|---|---|---|
| P0-1 | `pipeline.py:324` 把 0–1 的 `components.meme` 与 `OBSERVE_MIN=45` 比较，非种子梗族 1040/1055 被直接 rejected；可用条目只剩 14 条种子命中，cycle#2 Top10 实为「种子表 ∩ 热度」 | QA-QUALITY 报告；`pipeline.py:324` |
| P0-2 | 非种子正向证据上限低于 0.45（docs/01 的入池线），且种子被豁免 lexicon/evergreen 负向规则；阈值与规则不自洽 | QA-QUALITY §根因 |
| P0-3 | `is_new` 只在进程内存在，未落库；`/api/top` 缺该字段，「新晋」只能按 `first_seen<36h` 猜，冷启动当天 10/10 全标 | QA-VERIFY P0-1 |

### 7.2 工程风险（P1）

- `/api/refresh` 的 `STATE["running"]` 检查与置位分离，连续两次调用可双开采集（QA-VERIFY 已复现）。
- `RateLimiter` 实际未生效：往返 452ms > 间隔 333ms，`MAX_RPS=3` 是死配置；当前吞吐约 2.2 rps，网络变快会无声超频（ASYNC-DESIGN）。
- `_curl_get` 在请求**之后**才限速且不计数；`KeyboardInterrupt` 会留下 `status=running` 的 cycle。
- `upsert_videos` 用 `cid=excluded.cid` 反向覆盖 NULL，搜索视频每轮重补 25 次 pagelist；`COMMENT_CACHE_SECONDS` 未使用，评论每轮 30 请求。
- `MIN_VIDEOS_FOR_RANK` 0 引用；`CONFIRM_ROUNDS` 被 `rounds=1` 硬编码（语义等效 2 轮，但配置失真）。
- 质量基线：放宽口径真梗率 60%、严格新梗口径 20%、黄金集召回 16.7%。

### 7.3 文档偏差（P2）

种子 148 vs docs 写 132；限速 2 vs 配置 3；别名 +0.35 vs 代码 +0.20；README 环境变量缺 `HOTMEME_DATA_DIR/FIXTURES/TIMEOUT/UA`；`哈基米` 一条别名跨 2 个梗族。

## 8. 决策（14:22）

| # | 决策 |
|---|---|
| D1 | 主线会话 14:12 后无写入、服务已停，视为**已交接**；`app/` 所有权收归 PM 调度 |
| D2 | v1.1 顺序：**质量闸门（P0）→ 工程修复（P1）→ 看板 → 复验**；质量不修，性能与看板都是放大错误 |
| D3 | 质量修复只动 `pipeline/judge/score/store/config/data/tests`；`http/bili/api/static` 留给下一波，避免同文件竞争 |
| D4 | 合规线定 **2 rps**：修好限速器后默认 `MAX_RPS=2.0`，并发默认关闭；分端点桶不用于扩容（84/86 请求同域名） |
| D5 | 评论缓存 30 分钟上线；搜索缓存（C2）**不做**（会冻结 42% 候选池，钝化新面孔） |
| D6 | `is_new` 由 API 正式暴露，看板优先用该字段，`first_seen` 仅作兜底 |
| D7 | 真机验证一律写临时库（`HOTMEME_DB=/tmp/...`），不污染 `data/hotmeme.db`；每波最多 1 次真机 |

## 9. 执行记录

### 9.1 Wave 2A（质量闸门，app-core-quality）

- 已完成：`MEME_MIN` 引入、量纲 bug 修复、`passes_meme_gate`、`is_new` 落库 + `/api/top` 暴露、`MIN_VIDEOS_FOR_RANK` 接线、31 tests 通过。
- PM 复核**退回 4 项**：
  1. `tests/test_golden.py` 被削弱（删掉 `positive_mean>=0.35` / `all(score>=0.3)`），已知梗 抽象 0.25、凡尔赛 0.25 被掩盖；
  2. 种子对 `lexicon/proper_noun` 的豁免被一并去掉，导致种子判定分过低——应恢复，仅保留 `evergreen` 降权；
  3. `hype_streak(rounds=CONFIRM_ROUNDS=2)` 实际要求连续 3 轮（当前轮未落库），应 `rounds=CONFIRM_ROUNDS-1`；
  4. docs/01 权重被改成与代码一致但无证据表——要求补「权重变更 × 证据 × 副作用」，无依据的回退。
- 追加任务：`MEME_MIN=0.35` 受控试验（临时库、1 次真机、人工标注非种子新增 20 条，≥60% 像梗才采纳）。
- 第二轮结果：黄金集原断言恢复并通过（min=0.30 / mean=0.3517 / neg_max=0.0，34 passed）；种子恢复 lexicon/proper_noun 豁免，evergreen −0.05；`CONFIRM_ROUNDS` 偏移修正 + 测试；`MEME_MIN=0.35` 试验 → 非种子新增 36 条但 Top10 有 8 条明显垃圾（「盘点/原版/正在刷弹幕/25年/哔哩哔哩」），判为梗仅 20%，**维持 0.45**，瓶颈定性为「证据抽取」而非阈值。
- 结论：**质量闸门修复通过 PM 验收**；Top10 仍为种子主导（7/10 常青+泛化），列入 WP4b 待办（新梗曝光/证据抽取），不在 v1.1 收尾范围。
- 提交：`f4629e5`（质量）、`0bb3d3a`（看板）。

### 9.2 Wave 2C（看板，frontend）

- 完成：历史 sparkline、`is_new` 优先、状态/空态/降级、SSE 断线降级轮询、键盘可访问、移动端、reduced-motion。
- 验证：`node --check` 通过；8011 临时服务 200；Chrome headless 实测 10 卡片 / 10 条曲线 / console errors=0；375px 无横向溢出。截图 `/tmp/wp2c-desktop.png`、`/tmp/wp2c-mobile.png`。
- 副作用已披露：临时启动触发 `init_db` 幂等迁移，给生产库 `meme_signal` 加了 `is_new` 列（行数不变，未跑采集）。
- 状态：**代码待 PM 提交，功能验收通过**。

### 9.3 事故 I-1（无法完全归因）

- 14:14:29–14:15:11 生产库出现 cycle#3（86 请求 / 42.7s），当时唯一活跃的只读会话是 qa-verify；其报告声明 TestClient 均用临时库 + OFFLINE。无法排除 TestClient/lifespan 误触调度器。
- 影响：生产库多一轮派生数据，无业务损害；`is_new` 列迁移与之无关（由 8011 服务启动触发）。
- 处置：数据保留（可再生成）；规则升级——**任何 TestClient/服务启动必须显式 `HOTMEME_DB=/tmp/*.db` + `HOTMEME_DISABLE_SCHEDULER=1`（除刻意真机验证），并在报告中贴出启动 env**。
- 后续：Wave 2B 的 api 单飞锁 + 全局唯一采集入口，从代码上消除双跑。

## 10. 交接与升级

- 主线收尾时请写 `docs/pm/HANDOFF.md` 或在看板追加一行：完成了什么、还剩什么、哪些文件可释放。
- 主线释放 `app/*.py` 后，PM 按 WP3→WP4→WP5 顺序派实现会话（同一时间只允许一个会话写 `app/`）。
- 审计文档只给建议，不直接改代码；由 PM 汇总成实施任务单。

## 11. 发布状态（15:05，Wave 2D 完成）

| 项 | 结论 |
|---|---|
| 代码基线 | `4f1272f`（质量 `f4629e5` + 看板 `0bb3d3a` + 工程 `4f1272f`）+ 本文件/QA-FINAL |
| 测试 | **50 passed**；compileall / node --check 通过 |
| 性能 | warm 轮 **31 请求 / 18.0s**；冷轮 165 请求 / 95.1s（comments 412→curl 兜底 29 次） |
| 最终判定 | **有条件发布：P0=0，P1=3，P2=8**（`docs/pm/QA-FINAL.md`） |
| 发布动作 | **必须重启 8000 上的旧进程（PID 34015，14:24 启动）**，否则 `is_new`/`scheduler`/单飞/2rps 均未加载 |
| 重启自检 | `/api/top` 的 `is_new` 为 bool；`/api/health.scheduler=true`；首轮约 95s，次轮约 18s；`/api/health.health.errors=[]` |

P1 遗留：① 旧进程未重启（发布动作）；② Top10 10/10 种子命中、新梗曝光 0（WP4b）；③ 单飞仅单进程，禁止 `--workers>1`。

P2 摘要：`limit` 钳位未文档化、history 不存在 key 返回 200 空数组、`components.raw` 类型被覆盖、`MAX_SURFACE_LEN` 死配置、冷启动 `is_new` 全真需 UI 说明、无 curl 环境评论降级、`_start_collection(reason)` 死参。

## 12. 下一阶段建议（WP4b，未排期）

1. **证据抽取**：真正的瓶颈——真实非种子稳定组合分 0.39 < 0.45；提升引号/后缀/独立成词的召回（如引号模板、弹幕整句、评论区「这是什么梗」上下文）。
2. **新梗曝光**：API/看板增加「新晋候选」视图（`is_new` + 高增速但未进 Top10），避免新梗被种子热度淹没。
3. **冷启动**：评论 412→curl 兜底改为直接 curl 或用可用 TLS 指纹，压缩 95s；或首轮限制评论目标数。
4. **多进程/部署**：单 worker 显式声明，或加进程级文件锁。
5. **异步**：在 2 rps 合规线下无收益，暂缓；若未来放宽到 3 rps 再评估有界并发（ASYNC-DESIGN 方案 A）。

## 13. v2 P0：评论区优先（进行中，16:12）

### 13.1 决策

| # | 决策 |
|---|---|
| V1 | **评论区是第一语料区**：评论 30 视频 ×（热度 mode=3 + 时间 mode=2），弹幕降到 10 视频、只做扩散旁证 |
| V2 | **分层窗口**：≤30 天取证据与语义；≤7 天算增速/新晋；≤90 天只用于经典/回锅 |
| V3 | 点赞按年龄归一（`log1p(like)` + age），不做「7 天且高赞」硬 AND（实测会接近 0 条） |
| V4 | 评论 `ctime` 必须落库（现 `raw_time` 恒 0 是 bug）；新增 `earliest_comment_at` / `age_days` |
| V5 | 老梗刷屏识别：弹幕重复高 + 近 7 天评论增速低 → 只进经典榜 |
| V6 | P0 不做 LLM；LLM 审核/重排在 P2，等证据包可用后再接 |

### 13.2 实测依据

- 当前 cycle#7：评论 90 条 vs 弹幕 16,444 条（弹幕 99.5%）。
- 接口探测（1 视频）：`/x/v2/reply` 仅 3 条；`/x/v2/reply/main?mode=3` 20 条；`mode=2` 19 条且按时间。
- 3 视频分布：热度评论集中在 30–90 天前（均赞 2021/3508），时间评论集中在 ≤7 天（均赞 0.1–0.5）；仅 2 天新视频出现「≤7 天 + 均赞 1506」。

### 13.3 分工

| 会话 | 所有权 | 任务 |
|---|---|---|
| comment-core | `app/bili.py`、`app/pipeline.py`、`app/store.py`、`app/config.py`、`tests/*`、`docs/02` | 切 `reply/main`、ctime 落库、窗口/配额/缓存、评论证据与 7 天增速、真机对比 |
| eval-harness | `eval/`（新建）、`docs/pm/EVAL-DESIGN.md`、`docs/pm/P0-BASELINE.md` | 标注规范、指标脚本、种子年代审计、当前 Top10 基线 |

### 13.4 P0 验收

- 评论 ≥800 条/轮，弹幕 ≤4,000 条/轮；请求 ≤110；0 新增错误。
- `text_sample.raw_time` 非 0；canonical 能带 `earliest_comment_at`、`comment_7d_count`、`comment_like_weighted`。
- pytest 全绿；真机只用 `/tmp/p0.db`，不写生产库、不重启 8000。
- 产出「评论区版 Top10 vs 当前 Top10」对比与 origin 年代分布。

## 14. v2 P1/P2：评论影响排序 + LLM 审核模块（16:25）

### 14.1 P0 结果（已提交 `ced1024`）

| 项 | 结果 |
|---|---|
| 测试 | 63 passed |
| 评论量 | 90 → **222–237 条/轮**（30 视频 × mode3 1 页 + mode2 1 页） |
| 关键 bug | `buvid3` 使 `reply/main` 只回 3 条；评论请求临时摘除 buvid3 后回 20 条 |
| 时间戳 | `text_sample.raw_time` 432/432 非 0；真实 ctime 可查 |
| Top10 | **仍 100% 种子**（P0 不改 judge/score，评论证据只做门控） |
| 差距 | 评论目标 800 未达：60 请求拉 958 条，76% 因 >30 天被丢；mode=2 单页仅 3–9 条但可用 `next=20/40` 翻页（实测 4 页 60 条不重复） |
| 基线 | eval：seed 10/10、常青 60%、平均 origin 3.87 年、严格新梗 0/10、种子表 classic 75.7% |

### 14.2 P1 决策

- 评论 **mode=2 翻 3 页（next=0/20/40）**，mode=3 1 页；评论请求 120/轮，总请求预算 ~150。
- 评论证据从“只做门控”升级为“能进候选池”：`comment_backed`（近期评论 ≥3、覆盖 ≥2 视频、7 天有量）可绕过 `MEME_MIN` 进入 observing。
- 增加 `novelty`：`age_days` + `comment_7d_count` + `comment_like_weighted`；输出 `list=new|classic`。
- 不改 judge 的种子先验；只给非种子一条评论证据通道。

### 14.3 P2 决策

- 先做**独立 LLM 审核模块**（不直接改 pipeline）：`app/llm.py` + `app/review.py` + 假客户端测试。
- Provider 走 OpenAI 兼容接口，env：`HOTMEME_LLM_BASE_URL / API_KEY / MODEL`；无 key 时用 fake，不阻塞。
- 审核 rubric 四项：像不像梗 / 出处可追溯 / 语义增量 / 是否在扩散（只用证据，不用模型记忆）。
- 有界循环 ≤3 轮，从 top 50 递补；失败降级沿用上一版/观察版；输出结构化 JSON + 审计。
- 集成（P3）等 P1 的证据字段就绪后再做。

### 14.4 分工

| 会话 | 所有权 | 任务 |
|---|---|---|
| novelty-core | `app/bili.py`、`pipeline.py`、`store.py`、`config.py`、`judge.py`、`score.py`、`api.py`、`tests/*`、`docs/02` | 评论 3 页、comment_backed、novelty/list、真机对比 |
| llm-review | 新建 `app/llm.py`、`app/review.py`、`tests/test_llm_review.py`、`docs/pm/P2-LLM-DESIGN.md` | 客户端、rubric、状态机、假客户端测试 |

## 15. v2 P1/P2 结果与 P3（16:42）

### 15.1 P1 结果（`/tmp/p1.db` 两轮）

| 项 | 冷轮 | warm |
|---|---|---|
| 评论保留 / ≤7 天 | 408 / 206 | 408 / 206 |
| comment_backed / 非种子 usable | 70 / 70 | 69 / 77 |
| 请求 / 耗时 | 160 / 87.9s | 31 / 15.9s |
| new/classic | new 74 / classic 4 | 同 |
| Top10 | **未变，8/8 种子** | 同 |

- 修掉 P0 翻页 bug：`reply/main` 的 `next` 是 opaque 游标（1682），不是 20；改为上一页 `cursor.next` 串联并缓存，warm 轮 120 页 0 请求。
- comment_backed 70 条几乎全是高频子串/ASCII（ai/co/10/in/on…），只进 `observing` 与 `list=new`，不占主榜；质量过滤交给 LLM。
- novelty 权重：exposure 0.30→0.25，novelty 0.05；合计 1.0。
- 未达 600 评论：30 天窗口丢 71%，要 600 需 45–48 个评论视频（超预算），未硬凑。

### 15.2 P2 结果

- `app/llm.py` + `app/review.py` + 44 tests，全仓 **117 passed**；OpenAI 兼容、JSON 校验、注入防护、有界循环、预算/降级、审计 log。
- 无 key 可构造 fake；成本：单轮闸门 ≤18 调用 ≈ ¥0.037，10 分钟一轮满额约 ¥5.3/天（按 ¥1/¥2 M token 估算）。

### 15.3 P3 集成计划

| 项 | 决策 |
|---|---|
| 插入点 | `score_families` 之后、`display` 之前；只审 `list=new` 的 top50，不改分数 |
| 模式 | `off`（默认）/ `shadow`（只记）/ `demote`（剔除 reject/merge，推荐首开）/ `require_approve`（只发 approved） |
| 输入 | P1 的 comment/age/df 证据 + `EvidencePacket`；缺失自动 unclear |
| 输出 | `status`、`llm_review` 字段、`llm_review` 审计表、stats（calls/approved/rejected/fallback） |
| 降级 | 无 key/超时/预算/连续错误 → 保留规则榜，`fallback_reason` 入 stats |
| 发布 | 新梗榜只发 approved；不足 10 条显示「审核中/候选不足」，不空榜、不注入未审项 |
