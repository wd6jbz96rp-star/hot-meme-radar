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

## 16. v2 P3 结果与当前状态（16:56）

### 16.1 P3 完成

- `run_cycle(..., reviewer=...)` 在落库前调用 `run_llm_gate`；只审 `list=new` top50，classic/all 不动。
- 模式：`off`（默认）/`shadow`/`demote`/`require_approve`；无 key/超时/预算/异常 → 规则榜 + `fallback_reason`。
- `llm_review` 审计表（含 merge_into）；API 返回 `llm_review` 与 `llm` 摘要；stats 14 项。
- 全仓 **135 passed**；真机 `/tmp/p3.db`：shadow 19 调用/10 approved/9 rejected；warm demote 撤掉 9 条纯 ASCII/数字，all Top8 种子不变。
- 无真实 key：`build_reviewer()` 返回 None，零真实调用；质量待真机 shadow。

### 16.2 已知风险

| 风险 | 证据 | 处理 |
|---|---|---|
| 评论接口非 JSON | P3 冷轮 28 页 `reply/main` 返回非 JSON → degraded | P4 加退避/重试/降级；区分 412/风控/空响应 |
| 新梗候选质量 | P1 comment_backed 70 条几乎全是 ascii/数字；LLM 可过滤但无好候选可审 | P4 LLM 从近期评论抽取候选（不只 n-gram） |
| 评论量 | 408/轮，未达 600 | 45–48 个评论视频或放宽窗口；需预算决策 |
| 无 key | env 未配置 | 等用户提供；否则只能 fake 验证接线 |
| 线上旧进程 | 8000 PID 39002，cycle#17 仍是 90 评论/31 请求 | 完成后重启；否则看不到 P1–P3 |

### 16.3 P4 建议（待确认）

1. **LLM 候选抽取**：每轮取 300–500 条近期高赞评论，让 LLM 抽「像梗的短语 + 出处/含义 + 证据」，补 n-gram 召回；成本 1–3 调用/轮。
2. **评论接口健壮性**：非 JSON/412 重试与退避，失败页记 `comment_page_fail`，不整轮 degraded。
3. **评论量**：评论视频 30→45（请求 180–200，冷轮 ~100s），或 90 天证据 + 7 天增速。
4. **前端**：新梗榜/经典榜 tab，展示 LLM 审核结论与证据；无 key 时显示「规则版」。
5. **真机 shadow**：拿到 key 后跑 3 轮，人工核对 LLM verdict 与 Top10；再切 `demote`/`require_approve`。

## 17. v2 P4：DeepSeek + 45 视频 + 抽取/健壮性/前端（16:58）

### 17.1 用户决策

| # | 决策 |
|---|---|
| D8 | LLM 用 **DeepSeek**（OpenAI 兼容；默认 base `https://api.deepseek.com/v1`，model `deepseek-chat`） |
| D9 | 审核推进顺序：**shadow 3 轮 → demote → require_approve** |
| D10 | 评论视频 **30 → 45**（mode2 3 页 + mode3 1 页；请求约 210–230，冷轮 ~110s，warm 缓存后 ~31 请求） |
| D11 | 重启 8000 待 P4 完成后执行（当前 PID 39002 仍是旧代码） |

### 17.2 P4 分工

| 会话 | 所有权 | 任务 | 依赖 |
|---|---|---|---|
| comment-robust | app/bili.py、pipeline.py、config.py、store.py、tests/*、docs/02 | 45 视频、非 JSON/412 退避重试、失败统计、评论 ≥600、warm 0 请求 | 无 |
| llm-extract | 新建 app/llm_extract.py、tests/test_llm_extract.py、docs/pm/P4-LLM-EXTRACT.md | 从 300–500 条近期高赞评论抽候选（surface/meaning/origin/evidence/confidence），假客户端测试 | 无（真实调用等 key） |
| frontend-v2 | app/static/index.html、docs/pm/P4-WEB-REPORT.md | new/classic tab、LLM 审核 badge/证据、空榜/降级态、移动端 | 可用 /tmp/p3.db |

### 17.3 验收口径

- 评论 ≥600/轮；失败页 ≤5%；冷轮 ≤130s、warm ≤35s；0 未捕获错误。
- LLM 抽取：只从证据评论出候选，禁止编造；输出可合并进 `discover` 前的候选池。
- 前端：无 key/新榜为空时明确显示「规则版/审核中」，不假装有数据。
- 不写生产库、不碰 8000、不 commit。

## 18. v2 P4 结果与 P5（17:06）

### 18.1 结果

| 项 | 结果 |
|---|---|
| 评论 | **631/轮（30 天）、293（≤7 天）**；45 视频、180 评论请求 |
| 冷轮 | 223 请求 / **111.4s** / 0 失败页 / 0 错误 |
| warm 轮 | **31 请求 / 17.6s** / 180 评论页全缓存 |
| 健壮性 | 412/HTML/空/非 JSON/-352 分类重试；失败率 >20% 才 degraded；单页失败不整轮降级 |
| LLM 抽取 | `app/llm_extract.py` 44 tests；450 评论/3 批 ≈ ¥0.033/轮；无 key 返回空、零调用 |
| 前端 | 新/经典/全部三 tab、LLM badge、证据、空态/降级态；headless 实测、console 0 error |
| 测试 | **192 passed** |

### 18.2 P5 集成（下一步）

1. 在 `discover(docs)` 之前调用 `llm_extract.extract_candidates`；把 LLM 候选按 `normalize_surface` 与 n-gram/种子合并，补 `meaning/origin/evidence`；不重复计分、不编造。
2. 配置 `HOTMEME_LLM_EXTRACT=1`（默认 0）；无 key/失败静默跳过，stats 记 `llm_extract_*`。
3. `LlmConfig` 支持 `HOTMEME_LLM_API_KEY_FILE`（key 文件，chmod 600），方便不把 key 写进聊天/仓库。
4. 新脚本 `tools/shadow_run.py`：3 轮 shadow + 抽取，写 `/tmp/shadow.db`，输出每轮 candidate/approved/rejected 对比；不启服务、不写生产库。
5. 拿到 key 后跑 3 轮 shadow，人工核对；再切 `demote`，最后 `require_approve`。

### 18.3 Key 协议

- 用户把 DeepSeek key 写入 `~/.hotmeme_llm_key`（`chmod 600`），或设置 `HOTMEME_LLM_API_KEY`；**不要贴聊天**。
- 我方只以 `HOTMEME_LLM_API_KEY_FILE` 读取，不打印、不入库、不提交。

## 19. v2 P5 结果与真实 LLM 阻塞（17:22）

### 19.1 P5 完成

- `run_cycle` 在 `discover` 前调用 LLM 抽取；候选按 norm 合并、补 metadata、known 标记；stats `llm_extract_*`。
- `HOTMEME_LLM_API_KEY_FILE` 支持；key 不打印、不入库。
- `tools/shadow_run.py`：3 轮 shadow + 抽取，`/tmp/shadow.db`，拒绝生产库；无 key exit 2。
- 全仓 **207 passed**；fake 集成 3 轮链路通过（approve 1、gate applied）。
- **真实 shadow 未执行**：无 `~/.hotmeme_llm_key` / `HOTMEME_LLM_API_KEY`。

### 19.2 当前状态（重要）

- **new 榜在 fake 审核下是垃圾**：截图 `docs/pm/assets/02-tab-new.png` 里「也不/烂梗/还没/都没」被 `fake-heuristic` 标通过。这证明接线通了，但**不是可发布质量**。
- `HOTMEME_LLM_MODE` 默认 **off**，因此线上不会展示这些 fake 结果；new 榜需要真实 key 才能启用。
- 8000 仍是旧进程（PID 39002，cycle#17），P1–P5 未加载。

### 19.3 下一步（P6，等 key）

1. 用户把 DeepSeek key 写入 `~/.hotmeme_llm_key`（chmod 600），或设 `HOTMEME_LLM_API_KEY`。
2. 跑 `tools/shadow_run.py --rounds 3 --db /tmp/shadow.db`；检查 extract 候选、review verdict、Top10、调用/成本。
3. 人工核对 3 轮：记住「new 榜必须至少 1 条非种子且 LLM approve」；达不到就继续修抽取 prompt/证据，不要切发布。
4. 通过后 `HOTMEME_LLM_MODE=demote` 灰度，再 `require_approve`；最后重启 8000 并更新 README。

## 20. 真实 DeepSeek shadow 结果（18:10）——未达发布线

### 20.1 已跑通

- 3 轮真实 shadow（`/tmp/shadow_p7.json`，status=ok，exit 0），DeepSeek 调用正常；成本合计 **¥0.222/3 轮**。
- 审核池优先级修复：抽取候选从「0 条被审」变为 R1 2 条 / R3 4 条被审；0 fallback。
- 全仓 **214 passed**。

### 20.2 未达标

| 指标 | 结果 |
|---|---|
| approve | R1 **1 条（神人 0.62）**，R2/R3 **0** |
| 稳定性 | 「考古」曾 approve 0.72，后又 reject 0.15；不稳定 |
| Top10 | 3 轮 Top1 仍是 **抽象**；new 榜仍以哈基米/咕咕嘎嘎/嘉豪等种子为主 |
| 抽取 | R1 14 条、R2 **0 条（schema 失败）**、R3 20 条；3 轮 1 轮断供 |
| 证据 | 20 条抽取候选里仅 10 条被判 `new`，其中仅 4 条过梗性/评论门；多数只有 1 评论/1 视频 |
| 覆盖 | new 榜 99–326 条，审核只覆盖 24 条；demote 会让未审垃圾顶上 |

### 20.3 结论

- **不能切 demote / require_approve，不能重启 8000 发布。** 当前 LLM 审核只能证明「能拒绝垃圾」，还不能稳定证明「能选中真新梗」。
- 瓶颈不在「审核循环」代码，而在**证据深度与新旧校准**：抽取自 30 天评论，new 榜要求 7 天活跃；候选多数只有单评论/单视频；规则闸门每轮仍拒 459–471 条。
- 「神人」被 approve 说明 rubric 对泛化词仍偏松；「考古」跨轮翻转说明证据不足时判断不稳。

### 20.4 P7 建议（待定）

1. 修 `llm_extract` 的 schema/重试（R2 断供）；允许部分字段缺失。
2. 证据门槛前置：抽取候选必须 ≥2 条评论、≥2 个视频才送审；提取采样向 ≤7 天倾斜。
3. 审核 rubric 收紧：泛化词/无固定模板/无出处直接 reject；加「已知老梗」检查（种子 origin + 经典词表 + LLM 软判断）。
4. 发布策略：new 榜只发 approved（require_approve），不足 3 条显示「候选不足/审核中」，不 top-up。
5. 数据量：45 视频 → 60，或 mode=2 5 页；评论 631 → 1,000+，冷轮 ~150s。
6. 评测：用 20–30 条人工标注的「近期真梗/老梗/非梗」做 precision 检查，再决定是否继续。

## 21. 工程师 meme_info 交付复核（22:45，含用户盲标）

- 交付：`7a585af feat(meme-info)`，`meme_info/` 独立库 + `tools/meme_info.py` + 49 tests；全仓 263 passed；不 import app、不写库、不起服务。
- 用户 16 条盲标（`meme_info/labels/blind_20261114_user.json`）：**11 真 / 4 假 / 1 不确定**；P@5 **1.00**、P@10 **0.80**、P@16 **0.688**；**Top8 全部为真**。
- 假阳性集中在 9/10/14/16：`最怕BLG夺冠之人`、`非凡之爱`、`腋毛攻击`、`明码标价`（均被 LLM gate 放行）。
- 独立重跑还发现：规则版 `音乐` #4（`is_generic` 未接入 `blocked_reason`）；单条 signals 缺 `evidence_rate_2x2`；银标 71 条与查询同通道（P@10 1.0 不可复现，独立银标 run 0.70）。
- 返工 v3（`3cbceb3`/`294f159`/`cc18ef5`）：59 module tests / 273 full；扎啤/吴家村保留，4 条假阳性已拦，Top8 仍在；62 条（16 用户锚点+46 AI 网页）P@10 1.0 / P@16 0.875，但**不是纯人工盲标**。
- 判定：**架构/工程通过；质量条件性通过，可做 shadow，不能自动发布**。用户 16 锚点 raw P@10=0.80（labeled 1.0）；报告写 1.0 是口径错误，需改。
- 复核报告：`docs/pm/MEME-INFO-REVIEW.md` §8。
- 下一步：shadow adapter（模块报告只读落新表/新 endpoint），用户复核 2–3 轮后再谈切换。

## 22. Shadow adapter（23:58 启动）

目标：把 `meme_info` 的 `HotMemeReport` 以**只读**方式接进 app，供内部对比；**不改 `/api/top`、不写 DB、不动调度器、不重启 8000**。

| 项 | 决策 |
|---|---|
| 数据流 | `tools/meme_info_shadow.py` 跑模块 → 原子写 `/tmp/meme_info_latest.json` → app 只读 |
| API | `GET /api/meme-info?limit=10&only=all|publishable`；返回 status/missing/stale/invalid + memes + verification |
| 发布门 | 只给 `publishable` 标记（status hot/rising + 2×2 证据），**不硬过滤**；shadow 阶段全量展示 |
| 文件 | 新增 `app/meme_info_adapter.py`、`tools/meme_info_shadow.py`、`tests/test_meme_info_adapter.py`；小改 `app/api.py`、`app/config.py` |
| 禁止 | 不改 `meme_info/`（工程师冻结）、不改 static、不写生产库、不起 8000 |
| 验收 | 59+ tests 全绿；TestClient 覆盖 missing/invalid/stale/ok/limit/only；一次真机 shadow 报告；不重启线上 |

下一步：shadow 对比 2–3 轮 → 人工看新面孔/假阳性 → 再决定是否替换 `list=new`。

### 22.1 Shadow adapter 交付（00:12）

- 新增 `tools/meme_info_shadow.py`（采集端，原子写 `/tmp/meme_info_latest.json`；拒写 DB/data）、`app/meme_info_adapter.py`（只读/精简/缓存）、`GET /api/meme-info`；`/api/top` 未动。
- 全仓 **297 passed**（+24）；原始报告 2.4MB → API 响应 6.3KB；状态机 ok/stale/missing/invalid 均 200，非法 only 400。
- 真机 shadow：137s / 178 请求 / 1025 评论 / 20 memes；`publishable_count=7`；Top10 命中用户 16 锚点 7 条；app cycle#24 是 6.5h 前旧数据，overlap 0/10 **不是质量结论**。
- 线上 8000 当时未运行；未重启、未写生产库。
- 下一步：给看板加「模块 shadow」只读 tab（显示 status/age/evidence/LLM），用同一时段跑 2–3 轮再评估是否切 `list=new`；切之前仍不自动发布。

## 23. Shadow tab（进行中，00:20）

- 目标：看板加「模块 shadow」只读 tab，展示 `/api/meme-info` 的 status/age/top/2×2/LLM；明确「仅对比，不参与发布」。
- 所有权：`app/static/index.html` + `docs/pm/SHADOW-TAB-REPORT.md`（+ 截图 `docs/pm/assets/shadow-tab.png`）；不改 app 代码。
- 验证：8011 + `/tmp/shadow-web.db` 副本 + `/tmp/meme_info_latest.json`；headless 截图、console 0 error、`/api/top` 不变。
- 禁止：重启 8000、写生产库、run 采集。

## 24. 同一时段 shadow 评估（00:25）

- 报告：`docs/pm/SHADOW-EVAL.md`；双侧冷缓存、首尾相接 16s，总跨度 7m00s。
- app 旧 pipeline：195s / 275 请求 / 377 视频 / 592 评论 / 1704 弹幕；Top10 **10/10 种子**（抽象/上头/整活/咕咕嘎嘎/离谱/吃瓜/嘉豪/yyds/绝绝子/那咋了），105 usable。
- 模块：207s / 228 请求 / 943 视频 / 826 评论 / 2000 弹幕；Top10 **10/10 新面孔**，7/10 命中用户 ✅，publishable 7/10。
- **overlap 0/10（Top20 也 0/20）**；app 全量 105 条与模块 Top20 0/20。模块 Top10 无 ❌（3 条未标），Top20 有 2 条漏网 ❌：`最怕BLG夺冠之人`(#12)、`腋毛攻击`(#17)。
- 单轮稳定性：模块 Top10 相对 00:01 上轮 7/10、Top20 15/20。
- **发现 app `list=new` 不可用**：rank=0 的 comment_backed 噪声词（ai 87.5/看完/b站/ge/这句…）按 score 占满 new 榜，3 条真 new 梗被挤出；切榜前必须修或由模块替代。
- 修了 `tools/meme_info_shadow.py` 直接运行被 `tools/meme_info.py` 遮蔽的 bug（sys.path 优先仓库根）；297 tests 仍全绿。
- 结论：**继续 shadow，暂不切 `list=new`。**

## 25. Gate 修复 + new 榜替代契约（00:45）

- 工程师 `f841d2e`：gate 漏网 `最怕BLG夺冠之人`/`腋毛攻击` 已修；40 审 18 rejected，`unverified_omitted=9`；Top10 无用户 ❌。
- `meme_info/REPLACE-NEW-LIST.md`：旧 `list=new` 的 rank=0 comment_backed 噪声不可用，决定由模块 `publishable` 替代；切榜门槛=3 轮 shadow、Top10 无 ❌、publishable 稳定、用户复核未标项、旧 new 下线。
- 补标 4 条为 **AI+网页检索**（蓝色大肥鱼 current、doge evergreen、心生爱慕 current、雪豹眉眉 unclear），不是用户盲标；用户 16 锚点 raw P@10 仍为 0.70（labeled 1.0）。
- 全仓 **300 passed**。
- 下一步：app + 模块同段 3 轮 warm-history，记录稳定性；再决定切榜。

## 26. Warm-history 3 轮与切榜门槛（00:58）

- 报告：`docs/pm/WARM-EVAL.md`；3 轮 app→模块，总 8m34s，≤2rps，无 8000/生产写（除事故脚本，见下）。
- 模块 Top10 用户 ❌：**0/0/0**；publishable Top10 **7/8/8**；Top20 Jaccard **0.905**；Top10 8 条 3 轮全在。
- app 旧 Top10 仍 10/10 种子；模块 Top10 与 app overlap **0/10**；旧 `list=new` 仍被 rank=0 噪声占满。
- 未切榜：`REPLACE-NEW-LIST.md` 四门槛仅第 1 条满足；`闪身步` 连续 3 轮 Top10+publishable 仍只有 AI 补标、无用户锚点；产品未确认「只发 publishable 7–8 条/轮」；旧 new 未下线；r2/r3 gate 全走 LLM 缓存，需 1 轮冷缓存复验。

### 26.1 事故（已止损）

- warm-eval 辅助脚本 `/private/tmp/warm_recover_app.py` 首版忘设 `HOTMEME_DB`，连生产库并调用 `init_db()`；`data/hotmeme.db` 从 `00b3e839…` 变为 `8b533acb…`。
- 取证：当前字节 == `/tmp/shadow-web.db`（00:11 副本）；integrity ok；cycle=24 / signals=341 / memes=1650，无业务行变化；只是 WAL checkpoint/布局差异。
- 处置：不再写生产库；后续脚本强制 `/tmp`；事故记录 `docs/pm/INCIDENT-20261115.md`。数据无损坏，不需要恢复。

### 26.2 切榜前还需

1. 用户确认 `闪身步`（AI 标 current，3 轮 publishable）与 `不小心惹了上帝` 等； 
2. 产品确认「只发 publishable 7–8 条/轮」；
3. 旧 `list=new` 从 UI/API 下线或标 shadow；
4. 1 轮 LLM 冷缓存 gate 复验；
5. 完成后 `/api/meme-info?only=publishable&limit=8` 作为新梗榜，保留 `/api/top` + JSON 回滚。

## 27. 用户确认闪身步非梗 + 冷缓存复验（01:08）

- 用户 2026-11-15 确认：`闪身步` ❌；已加入 `blind_20261114_user.json`（17 条）与 `REVIEW_STOPWORDS`，补回归测试；全仓 300 passed。
- 冷缓存 module 跑（`/tmp/cold-cache-1`，`/tmp/gate_cold.json`）：212.5s / 198 请求 / 40 审 / 20 rejected / gate=true；Top10 **无闪身步、无用户 ❌**；publishable 7/10；`灭建国`/`蓝色大肥鱼`/`不小心惹了上帝` 因 2×2=false 不在 publishable。
- 决定：按用户同意切新梗榜——UI `新梗榜` 改读 `/api/meme-info?only=publishable&limit=8`；旧 `/api/top?list=new` 从 UI 下线（API 暂留）；模块 shadow tab 保留全量。
- 注意：新榜只发 7 条左右；meta-only/2×2 未过项进「模块 shadow」观察，不 top-up。

## 28. 新梗榜切模块版（01:19）

- `app/static/index.html`：`新梗榜` 改为 `GET /api/meme-info?only=publishable&limit=8`；旧 `/api/top?list=new` 从 UI 下线（API 暂留）；经典/全部仍走 `/api/top`；模块 shadow tab 保留 `only=all`。
- 实测 8011（`/tmp/newtab-web.db` + `/tmp/gate_cold.json`）：新梗榜 8 卡（publishable_count=12/20）；**全会话 `/api/top?list=new` = 0 次**；console/HTTP 错误 0；sparkline/键盘/移动端正常；截图 `docs/pm/assets/new-tab-module.png`。
- 冷 gate 报告已复制到 `/tmp/meme_info_latest.json`，供默认路径启动。
- 已知 tradeoff：publishable 需要 2×2 证据，`灭建国`（用户 ✅）、`蓝色大肥鱼`、`不小心惹了上帝` 暂被排除；若产品认为不可接受，需放宽 publishable 规则（不建议为凑数放水）。
- 仍未部署：8000 未启动；模块报告刷新 worker 未建；先确认这两项再算 live。

## 29. Live 启动（01:23）

- 8000 已启动：`HOTMEME_DISABLE_SCHEDULER=1`，新梗榜读模块 publishable，经典/全部停在 cycle#24。
- worker `tools/meme_info_worker.sh` 已启动：600s 一轮，写 `/tmp/meme_info_latest.json`，日志 `/tmp/meme_info_worker.log`。
- 启动后 `/api/health` 200、`scheduler=false`、`/api/meme-info=ok`、`/api/top` 仍为旧数据；生产库 hash 未变。
- 部署/回滚：`docs/pm/LIVE-DEPLOY.md`。
- 待观察：worker 首轮完成后 report age<600、新榜 8 条稳定；1–2 小时后决定是否恢复旧采集或调整 publishable。

## 30. Live 调整 publishable 口径（01:30）

- 首次 live 发现 2×2 硬门槛只出 6 条，且误杀用户 ✅ `灭建国/扎啤`；按用户「7–8 条」验收调整为：
  `publishable = hot/rising 且（2×2 或 LLM is_meme/is_current）`；2×2 仍单独展示。
- 8000 已重启加载新规则：`/api/meme-info?only=publishable&limit=8` 返回 8 条（`publishable_count=15`），包含 `灭建国/扎啤/老实赚钱`；`/api/top` 仍 cycle#24。
- worker 首轮 261s 完成，报告 `generated_at=01:23:23`；下一轮 600s 后。
- 全仓 300 passed；`docs/pm/LIVE-DEPLOY.md` 已同步。


## 31. Phase 0：旧采集入口退役（00:40）

- `/api/refresh` 改为 410；不再调用 `pipeline.run_cycle`；旧 pipeline 标注 DEPRECATED。
- 299 tests passed；README API/env 表已同步。
- 8000 当前未运行；下次启动即生效。经典/全部仍为 cycle#24 历史快照。
- 下一步：Phase 1 让 `/api/top` 只依赖 store 只读 helper，Phase 2 归档旧 pipeline/LLM 代码。


## 32. 旧 pipeline 全量清除（2026-11-16 00:50）

- Phase 1：`api.py` 不再 import/调用旧 pipeline；`/api/top` 只读 `store`；SSE 只推初始快照 + ping。
- Phase 2：删除 11 个旧 app 模块、16 个旧测试、`tools/shadow_run.py`、`eval/`；保留 `meme_info`、adapter、store、static。
- Phase 3：`store.py` 从 900+ 行重写为 231 行只读仓库（latest/top/list/history/health），旧写库/缓存/迁移移除。
- 当前：87 tests；`/api/refresh` 410；经典/全部读 cycle#24；新梗榜仍走模块报告。

## 33. 删除经典/全部与历史 DB（2026-11-16 01:10）

- 用户确认：经典/全部是固定 cycle#24 旧种子榜，非当前热梗、无产品必要。
- 已归档 `data/hotmeme.db` → `archive/hotmeme-20261114.db`（gitignored），删除运行时 DB。
- 删除 `app/store.py`、`/api/top`、`/api/meme/{key}/history`、经典/全部 tab、sparkline 依赖。
- `app/api.py` 仅保留 `/`、`/api/meme-info`、`/api/health`、`/api/refresh` 410、`/api/stream` 空快照。
- `app/config.py` 去掉 DB/DATA 配置；README/LIVE-DEPLOY 重写；87 tests。

## 34. 前端 UI 重做（2026-11-16 01:25）

- 新子智能体 `frontend-design` 负责前端；重写 `app/static/index.html`：两 tab（新梗榜 / 模块 shadow）、状态板、证据/LLM/2×2 徽标、stale/missing/invalid/offline 状态、移动端/a11y。
- 删除旧榜死代码：`/api/top`、history、sparkline、SSE、经典/全部残留；内联 JS 1085→708 行，文件 66→58KB。
- 验证：8011 headless console/HTTP 0 错；1280/390 无横向溢出；XSS 插值测试通过；87 tests。
- 截图：`docs/pm/assets/ui-desktop.png`、`ui-mobile.png`、`ui-stale.png`；报告 `docs/pm/UI-DESIGN.md`。

## 35. Aceternity 风格 UI（2026-11-16 01:40）

- 按用户要求参照 ui.aceternity.com/components，原生 CSS/JS 复刻 Spotlight、Dotted Glow、Grid、Aurora、Beams、Bento、Card Hover/3D 倾斜；无 React/Tailwind/CDN。
- Hero + 6 瓦片 Bento 状态板；新梗榜玻璃卡、指针高光、轻倾斜；shadow 保持 OBSERVE/虚线/紫 accent。
- 未采用 Meteors/Canvas/WebGL/打字机等（看板性能与信息密度），原因写入 UI-DESIGN。
- 87 tests；8011 headless console/HTTP 0 错；1280/390 无溢出；截图 `ui-desktop/ui-mobile/ui-hover/ui-stale.png`。

## 36. Aceternity 源码级复刻（2026-11-16 20:40）

- frontend-design 读取 `ui.aceternity.com/api/components` 与 `registry/<name>.json` 的真实 `.tsx` 源码，按源码参数复刻 SpotLight New、Dotted Glow canvas、Grid、Bento、Card Spotlight、Glowing Effect、3D Card、Text Generate。
- 未引入 React/Tailwind/Motion；修复基础类与标题不可见；87 tests；1280/390、stale/missing/XSS 验证通过。
- 公网临时地址仍为 localhost.run 隧道；页面完整加载约 8s，隧道延迟偏高。

## 37. 云服务器上线（2026-11-16 20:55）

- 腾讯云 Ubuntu 24.04 `124.220.189.74` 部署完成：`/home/ubuntu/hotmeme`，venv + systemd app(8090)/worker，Nginx `/hotmeme/` 子路径反代。
- 公网：`http://124.220.189.74/hotmeme/`；现有 80/8080/8000 服务未动。
- 首轮 worker 223s 完成，报告 20 条/17 publishable；公网 API 200；HTTPS/域名未配。
- 部署文件 `deploy/`、文档 `docs/pm/CLOUD-DEPLOY.md`。


## 38. 暂停 LLM 语义审核（2026-11-16 21:10）

- 按用户要求暂停 `SemanticVerifier`：worker 增加 `MEME_INFO_VERIFY` 开关，默认 0；systemd 显式 `MEME_INFO_VERIFY=0`。
- 不再调用 DeepSeek；报告无 `verified`，publishable 只按 2×2；UI 显示「LLM 审核已暂停」，不伪造审核理由。
- 代码保留 `meme_info/verify.py`，后续优化方案给出后可 `MEME_INFO_VERIFY=1` 恢复。
- 87 tests。


## 39. Phase A：Postgres 状态层（2026-11-21）

- 云服务器安装 PostgreSQL 16.15，127.0.0.1:5432；建 hotmeme + 三角色（migrator/worker/admin）；`/etc/hotmeme/env` 600。
- 新增 `migrations/0001_state.sql`、`0002_pool_flags.sql`、`meme_info/state.py`、`tools/migrate_state.py`、`tests/test_state.py`；schema_version=2。
- 修正池字段：pool1.meta_only/explainer_only/audit_adjustment/status normal|provisional|out；pool2.explainer_only。
- 本地 104 passed/15 skipped；云上 test_state 32 passed；未接 worker/app、未切流量。
- 报告：`docs/pm/PHASE-A-REPORT.md`。


## 40. Phase B：B 线候选视频与选片（2026-11-21）

- 新增 `meme_info/video_route.py`、`tools/video_route.py`、`tests/test_video_route.py`；`bili.py` 增 online/newlist/related/view。
- 候选 300（240 常规 + 40 探索 + 20 相关预留）；45 评论（35+10）/10 弹幕（8+2）；单分区≤40%。
- 在线人数 `/x/player/online/total`；首见评论/弹幕用年龄归一化代理；写 round/video_metric。
- 本地 126 passed/16 skipped；云上 23 passed（事务回滚）；live smoke 4.2s。
- 未接 worker、未切流量；报告 `docs/pm/PHASE-B-REPORT.md`。


## 41. Phase C：15 分钟窗口计数（2026-11-21）

- 新增 `meme_info/window.py`、`tools/window_sample.py`、`tests/test_window.py`；bili 增 include_top/dmid。
- 评论 1000/600，弹幕 600/300；文本去重 per_video；异常窗口 clamp 并标 degraded；30 天证据分离。
- 本地 146 passed/18 skipped；云上 22 passed（回滚）；未接 worker/未切流量。
- 报告：`docs/pm/PHASE-C-REPORT.md`。


## 42. Phase D：family 状态、比例与池迁移（2026-11-22）

- 新增 `meme_info/pools.py`、`tools/pool_round.py`、`tests/test_pools.py`；修复 discover 空 pubdate min/max bug。
- 15 分钟窗口计数：comment/danmaku/window_video；30 天 evidence_video 独立字段，不进 count_total。
- cred_base 五特征；score 0.85/0.15 + ratio + audit_adjustment；0 计数惩罚；P1 40/32、P2 65/55；provisional/explainer-only。
- 本地 176 passed/20 skipped；云上 test_pools 32 passed（回滚，0 残留）；未接 worker/未切流量。
- 报告：`docs/pm/PHASE-D-REPORT.md`。


## 43. Phase E：报告 v2 与 adapter（2026-11-22）

- 新增 `meme_info/report.py`、`tools/build_report.py`、`tests/test_report.py`；adapter 支持 v2 pool1/pool2，v1 兼容。
- `memes=pool2`（heat/rank），`pool1` shadow；2×2 仅徽标；`cred_score` 从 evidence_json 重建。
- 本地 216 passed/24 skipped；云上 test_report 29 + test_pools 33（回滚）；未接前端/未切流量。
- 报告：`docs/pm/PHASE-E-REPORT.md`。


## 44. Phase E2：前端池化视图（2026-11-22）

- 发布池 pool2 / 候选池 pool1 两 tab；publishable=pool2，2×2 仅徽标；degraded 独立提示。
- 卡片显示 cred/heat/heat_rank/audit_state/explainer-only；空态/降级保留；Aceternity 视觉不变。
- 8011 v2 报告验证：桌面/移动无溢出，console/HTTP 0 错；未 commit 前端由 PM 提交。
