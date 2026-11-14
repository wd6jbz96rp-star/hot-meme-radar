# QA-VERIFY · 验收回归报告（WP0）

- 审计会话：qa-verify（session `7b9140f6-3c1c-4d83-92af-dc238b53f652`）
- 时间：2026-11-14 14:12–14:20（Asia/Shanghai）
- 基线：git `84a4b28`（与 BOARD 一致）；`git status` 仅 `?? docs/pm/`
- 环境：`.venv/bin/python` 3.15.1 / fastapi 0.162.7 / starlette 1.10.0
- 约束遵守：只写本文件；未启动 uvicorn、未访问 B站、未跑真机采集、生产库仅 `mode=ro` 只读、临时 DB 全部删除、未安装依赖、未 commit。

---

## 1. PASS / FAIL 总表

| # | 任务项 | 结果 | 证据 |
|---|---|---|---|
| 1a | 测试全绿 | **PASS** | `.venv/bin/python -m pytest -q` → `23 passed, 1 warning in 1.07s`；warning 仅 Starlette TestClient 弃用提示 |
| 1b | 语法编译 | **PASS** | `.venv/bin/python -m compileall -q app` → exit 0 |
| 1c | lint 可用性 | **跳过（合规）** | `ruff` / `pyflakes` 均未安装（`No module named ruff/pyflakes`，`.venv/bin` 无相关可执行文件）；按要求不安装 |
| 2 | 离线流水线（两轮） | **PASS（有观察项）** | `HOTMEME_OFFLINE=1 HOTMEME_DB=/tmp/qa-verify.db`，`pipeline.run_cycle()` ×2：status 均 `done`，stats 13 键齐全，`requests=0`（不触网），耗时 0.6s/0.5s；重复跑维度表不膨胀（video 40、meme 245、alias 252），信号按 cycle 追加（cycle1=2 行、cycle2=2 行），`integrity_check=ok`、`foreign_key_check=[]`、rank 无重复；二轮分数因 EMA 从 58.4→55.7（符合 `S=0.6·新+0.4·旧`，见 docs/02:70） |
| 3 | API 契约 | **FAIL** | `/api/top` 正常、limit 边界、health、history、SSE 首事件、refresh 400/403 均通过；但 **top 项缺契约字段 `is_new`**（P0-1），且 health 的 `scheduler` 字段在 OFFLINE 下失真（P2-2）。详见 §2 |
| 4 | 文档一致性 | **FAIL** | 环境变量表漏 4 项、API 表漏错误码、docs/02 种子数/限速值与代码不符、docs/01 别名权重不符、docs/02 两条榜单约束未实现。详见 §4 |
| 5 | 生产库数据完整性 | **PASS（1 项 P2 瑕疵）** | `PRAGMA integrity_check=ok`、`foreign_key_check=[]`（但 schema 无 FK 声明，见 P2-4）；2 个 cycle 均 done、无 running；每 cycle rank 唯一（10/10）；score 21.25–93.16（0–100 内）；top 与 `meme_signal rank>0` 完全一致；`components` 10 键齐全且 **decay 24/24 存在（未复现 BOARD “decay 可能缺失”）**；`text_sample` 90 行无孤立外键；candidate/meme 无空值重复；alias 跨 meme 重复 1 例（P2-3） |
| 6 | 代码级风险复核 | **FAIL** | `STATE["running"]` 检查/置位分离**已确定性复现**（2 次 refresh 均 started，`run_cycle` 最大并发=2）；普通异常正确落 `failed`；`KeyboardInterrupt` 会留下 `running` cycle 且无启动清理；SSE 断连清理正常；curl 兜底绕过限速且不计入请求数。详见 P1-1/P1-3/P1-4 |
| 7 | 本报告 | **PASS** | 本文件 |

---

## 2. API 契约实测（TestClient + 临时 DB）

命令形态：`HOTMEME_OFFLINE=1 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/qa-api.db .venv/bin/python`，先离线跑一轮再 `TestClient(api.app)`（config 在 import 读环境变量，故全部用子进程设 env）。

| 用例 | 结果 |
|---|---|
| `GET /api/top` | 200；字段 `{cycle,error,health,running,status,top}` 齐全；`cycle=1`、`running=false`、`error=""` |
| top 项字段 | `canonical, components, delta, family_key, first_seen, meme_id, previous_rank, rank, sample, score, status, type, updated_at` —— **无 `is_new`**（契约 BOARD:51 要求） |
| `components` 键 | `breadth, concentration, decay, engagement, exposure, meme, previous, raw, reasons, velocity`（含 decay；`raw` 为 dict，见 P2-1） |
| `limit=10/0/-1/999/1` | 均 200，返回条数 2/1/1/2/1；`api.py:85` 用 `max(1,min(limit,50))` 静默钳位，0 与负数不报错 |
| `GET /api/health` | 200；`scheduler/offline/ready/running/error/health`；空库时 `ready=false` |
| `GET /api/meme/{key}/history` | 合法 key 200，`{family_key,history[{score,rank,at}]}`；不存在 key 也 **200 + 空数组**（非 404） |
| `GET /api/stream` | 200、`text/event-stream`；首行 `data: {...}`，负载键 `{cycle,health,status,top}`；关闭连接后 `SUBSCRIBERS` 归零 |
| `POST /api/refresh`（OFFLINE=1） | 无 token → **400** `{"detail":"离线模式不可刷新"}` |
| `POST /api/refresh`（HOTMEME_TOKEN=secret123, OFFLINE=1） | 无/错 token → **403**；`?token=` 与 `x-token` 正确 → **400**（鉴权先于离线判定，`api.py:118-124`） |
| `POST /api/refresh`（running=True） | **409** `已有采集任务在跑`（单次调用）；但并发窗口可双跑，见 P1-1 |
| 空库 `GET /api/top` | 200 `{status:"empty",cycle:null,top:[]}` |

---

## 3. 离线流水线复现

```
rm -f /tmp/qa-verify.db*
HOTMEME_OFFLINE=1 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/qa-verify.db \
  .venv/bin/python -c 'from app import pipeline; [print(pipeline.run_cycle()["status"]) for _ in range(2)]'
```

实测（脚本输出摘要）：

```
run1: status=done cycle=1 videos=40 comments=81 danmaku=14000 danmaku_cached=3
      candidates=252 families=245 usable=2 requests=0 elapsed=0.6 errors=[]
      top=[(1,'破防',58.4),(2,'嘉豪',9.9)]
run2: status=done cycle=2 videos=40 comments=81 danmaku=14000 danmaku_cached=35
      candidates=252 families=245 usable=2 requests=0 elapsed=0.5 errors=[]
      top=[(1,'破防',55.7),(2,'嘉豪',9.9)]
DB: cycle1/2=done；video 40、video_metric 80、meme 245、alias 252、candidate 252
    candidate_cycle 504、meme_signal 4（每轮 2）、source_error 0；rank 无重复
```

结论：**不触网**（`requests=0`，`OFFLINE=1` 走 fixture）；**可重复**（候选/家族/文档数一致，维度表 upsert 不膨胀）；**stats 完整**（videos/zones/comments/danmaku/danmaku_cached/comment_requests/candidates/families/usable/top/requests/elapsed/errors）；第二轮 signal 为新增 cycle、分数受 EMA 影响属预期累计行为。临时 DB 已删除。

观察项（非 FAIL）：离线只有 2 条可用信号，rank2 仅 9.94 分仍占榜（seed 命中，`pipeline.py:335`）；与 docs/02:76「热度 ≥55 才稳定上榜」的「稳定」口径需文档明确（见 P2-6）。

---

## 4. 文档一致性逐项核对

| 文档位置 | 文档说法 | 代码实际 | 结论 |
|---|---|---|---|
| README:30-39 环境变量 | 9 个 `HOTMEME_*` | `app/*.py` 共 13 个：另含 `DATA_DIR`、`FIXTURES`、`TIMEOUT`、`UA`（`config.py:8,11,16,22`） | 偏差（漏 4 项）P2-8 |
| README:81-87 API 表 | 6 条路由 | 实际另有 `GET /`；未写 `limit` 钳位、`token` 鉴权、400/403/409 | 偏差 P2-8 |
| README:57-77 目录结构 | 列出的文件 | 逐项检查全部存在 | 一致 |
| README:89-98 测试 | pytest / 离线集成 / 黄金集 30+30 | `tests/golden/memes.json`：positives 30、negatives 30；`23 passed` | 一致 |
| README:19/26/50 | 首轮 1–2 分钟、10 分钟一轮、B站分区榜(8)+热门+20 组搜索 | 生产 cycle 66s/43s；`INTERVAL=600`；`ZONES`=8；`SEARCH_QUERIES`=20 | 一致 |
| docs/02:31 | 种子梗表 **132 条** | `data/seed_memes.json` 实际 **148** 条（别名 49） | 偏差 P2-6 |
| docs/02:95 | 限速 **2 rps** | `MAX_RPS` 默认 **3**（config.py:17），README 也写 3 | 偏差 P2-6 |
| docs/02:76-79 | 连续两轮 ≥55、**至少 2 个视频**才进稳定榜、老梗回锅 >14 天 | `rounds=1`（pipeline.py:328）；`MIN_VIDEOS_FOR_RANK` **0 处引用**；回锅逻辑无实现（grep 无 14 天分支） | 偏差 P1-2 / P2-5 |
| docs/01:66 | 命中种子词典 / **别名表 +0.35** | 种子 +0.35，别名命中仅 **+0.20**（judge.py:87-91） | 偏差 P2-7 |
| docs/01:68-86 | 后缀/广度/弹幕/梗源/负向权重 | 与 judge.py 逐条一致（含 0.45 阈值在 pipeline 用 OBSERVE_MIN） | 一致 |
| docs/01:60 | 归并：Jaccard≥0.8 / 包含长度差≤2 / 编辑距离≤1，alias 最高优先 | `_similar` + `merge_families` forced 分支一致 | 一致（但产生 P2-3 歧义） |
| docs/02:33 / zones.py | 分区权重 1.4/1.2/1.1/1.1/1.0/0.9/0.9/0.7 | 完全一致 | 一致 |
| docs/02:38-44 | 模式 3–20 字、n-gram 2–6、MIN_COUNT 3、PMI 3.0、熵 1.0、DF2、前 1500 | `extract.py:111-138,282-292,327` + config 一致 | 一致（排序另乘 quoted 因子，未写入文档，P2 级小差异） |

---

## 5. 生产库只读审计（`file:data/hotmeme.db?mode=ro`）

```
PRAGMA integrity_check        -> ok
PRAGMA foreign_key_check      -> []        （注意：SCHEMA 无任何 FK 声明）
cycle                          -> 2 行：id1 14:08:10→14:09:16 done；id2 14:10:18→14:11:01 done；无 running
计数                           -> video 380 / video_metric 754 / text_sample 90 / danmaku_cache 50
                                  candidate 1502 / candidate_cycle 3000 / meme 1055 / alias 1503
                                  meme_signal 24 / source_error 0
每 cycle rank                  -> cycle1 10 行 rank1-10 唯一；cycle2 14 行 rank1-10 唯一 + 4 行 rank0
score 范围                     -> 21.28–92.96（c1）/ 21.25–93.16（c2），均在 [0,100]
top vs signals                 -> stats.top 与 meme_signal(rank>0) 的名称/分数/名次逐条相等
components                     -> 24/24 均含 exposure,engagement,breadth,velocity,meme,decay,
                                  concentration,previous,raw,reasons；decay 全为 1.0（首轮 age≈0）
text_sample 外键               -> bvid 不在 video 的 0 行；text/cycle_id 空值 0；kind 仅 comment
candidate                      -> norm/surface 空值 0；norm 重复 0；未知 norm 的 candidate_cycle 0
meme                           -> family_key/canonical 空值 0；family_key 重复 0；canonical 重复 0
meme_alias                     -> 空 surface 0；孤儿 meme_id 0；**跨 meme 同 surface：哈基米 → meme 188(哈基) 与 1053(哈基米)**
meme_signal                    -> meme_id/cycle_id 孤儿 0；rank ∈ [0,10]
video_metric                   -> cycle/video 孤儿 0
```

结论：数据完整性整体通过；`decay` 缺失的说法**未复现**。唯一数据瑕疵是 alias 歧义（P2-3），无 schema 级 FK 是设计缺陷（P2-4）。

---

## 6. 缺陷清单

### P0（1）

**P0-1 `/api/top` top 项缺少契约字段 `is_new`**
- 位置：`app/store.py:390-424`（`top_items` 未输出）、`app/api.py:81-89`（仅透传 `top_items`）；契约见 `docs/pm/BOARD.md:51`。
- 证据：TestClient 返回 top 项键集合无 `is_new`；全仓 `grep -rn is_new app/` 仅 `pipeline.py:306,322,330`（进程内变量，不落库，重启即丢）。
- 复现：`HOTMEME_OFFLINE=1 HOTMEME_DB=/tmp/qa-api.db .venv/bin/python - <<'PY' ... TestClient ... print(top[0].keys()) PY`（或 `curl -s localhost:8000/api/top | jq '.top[0]'`）。
- 影响：BOARD §4 接口契约不成立；WP5 无法做「新晋」角标；SSE 负载同样缺该字段。
- 建议归属：**WP5**（看板/契约），由主线在 `store.top_items` 补 `is_new`（按 `meme.first_seen` 是否落在本 cycle 之前，或落库时写入 signal）。

### P1（4）

**P1-1 `STATE["running"]` 检查与置位分离，`/api/refresh` 与 `_scheduler` 可并发跑两轮**
- 位置：`app/api.py:121-122` 检查 → `app/api.py:127-138` 新任务内才 `STATE["running"]=True`；`app/api.py:27-37` 调度器同样是 check-then-set。
- 复现（确定性，不触网）：`HOTMEME_OFFLINE=0 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/qa-race.db` 下把 `pipeline.run_cycle` 换成 0.6s 的桩，同一事件循环内连续 `await api.api_refresh(req)` 两次（两次调用之间无 await）。
- 实测输出：`two sequential-await refresh calls returned: {'started': True} {'started': True}`；`max concurrent run_cycle: 2 started: 2`；单次 running=True 时能正确 409。
- 影响：重复采集（B站请求翻倍、CPU/SQLite 双写、限速压力）、两个 cycle 同时 running；刷新按钮连点即可触发。
- 建议归属：**WP3**（异步改造时用 asyncio.Lock / 原子 CAS 统一调度器与手动刷新）。

**P1-2 「至少出现在 2 个视频中才进稳定榜」未实现（配置项死代码）**
- 位置：`app/config.py:35 MIN_VIDEOS_FOR_RANK` 全仓 0 引用；`app/pipeline.py:324-338` 只用 score/seed_hit 决定 hot/observing；文档 `docs/02-capture-and-scoring.md:79`。
- 证据：`grep -rn "MIN_VIDEOS_FOR_RANK" app tests` → 仅 config 定义；`CONFIRM_ROUNDS`（config.py:33）同样 0 引用，`pipeline.py:328` 硬编码 `rounds=1`。
- 影响：单视频候选可进 Top10，榜单易被单视频刷屏（与 docs/01「可复制性/跨视频」口径冲突）；现有 2 轮真机 Top10 恰好都有 breadth 证据，故未观察到违例。
- 建议归属：**WP4**（榜单质量调优）：在 `pipeline` 生成 display 前加 `df_video >= MIN_VIDEOS_FOR_RANK` 过滤，并补回归测试。

**P1-3 curl 兜底路径绕过限速器且不计入请求数**
- 位置：`app/http.py:140-145`（403/412 → `_curl_get`）、`app/http.py:151-166`（`subprocess.run` 之后才 `limiter.wait()`；未 `self.requests += 1`）。
- 复现：桩掉 `opener.open` 抛 `HTTPError(412)`、桩掉 `subprocess.run` 记录时刻，`HttpClient(max_rps=3).get(url, retries=0)`。
- 实测：`delay between HTTPError(412) and curl subprocess start: 0.0006s (min_interval 0.333s)`；`request_count = 1`（实际发出 2 个 HTTP 请求）。
- 影响：恰在风控（403/412）时对 B站 连发两请求，可能加剧 -352/412；健康统计少计请求；`docs/02:95` 的限速承诺在兜底路径不成立。
- 建议归属：**WP3**（`http.py`）：curl 前 `limiter.wait()`，并计入 `requests`/统一计数。

**P1-4 硬中断会留下 `status=running` 的 cycle，且无启动清理**
- 位置：`app/pipeline.py:230` `start_cycle` 在 `try` 之外；`app/pipeline.py:410` 只捕获 `Exception`（`KeyboardInterrupt`/`SystemExit` 不进入 failed 分支）；`app/store.py:157-160` `latest_cycle` 只认 done/degraded，running 行永不暴露、永不回收。
- 复现：`HOTMEME_DB=/tmp/qa-fail.db`，桩 `pipeline.fetch_videos` 分别抛 `RuntimeError` 与 `KeyboardInterrupt`。
- 实测：普通异常 → `case1 cycles: [{id:1,status:'failed'}]`；`KeyboardInterrupt` → `[{id:1,failed},{id:2,status:'running',finished_at:None}]`，`snapshot` 返回 empty（无 done 可回退），`store.health` 仍显示上一 done，stuck 行不可见。
- 影响：Ctrl-C / 部署重启 / OOM 后 DB 累积 running 行；监控盲区；对新库会表现为「无榜单」。
- 建议归属：**WP3**（pipeline 启动时把孤儿 running 标记为 interrupted，或用 `BaseException`/上下文管理确保收尾）。

### P2（9）

| ID | 缺陷 | 位置 / 证据 | 建议归属 |
|---|---|---|---|
| P2-1 | `components.raw` 原始分被覆盖为 `raw_signals` 字典：score.py 写入 `raw` 为 float（`app/score.py:96-104`，raw 在第 104 行），pipeline 用 `"raw": item.get("raw_signals", {})` 覆盖（`app/pipeline.py:384-388`）；生产库 24/24 `raw` 均为 dict | 实测 `raw_type=dict`；UI 未使用 raw，暂未破前端 | WP5 或主线 |
| P2-2 | `/api/health` 的 `scheduler` 在 OFFLINE=1 时为 `true`，但 lifespan 因 OFFLINE 根本没启动调度器 | `app/api.py:106` 返回 `not config.DISABLE_SCHEDULER`，而 `app/api.py:52` 条件是 `not DISABLE and not OFFLINE`；实测空库 `scheduler=True, offline=True` | WP5 |
| P2-3 | `meme_alias` 同 surface 跨 meme 重复：`哈基米` 同时挂到 meme 188(`哈基`) 与 1053(`哈基米`)；表无 `UNIQUE(surface)`，归并 forced 分支可产生同义族 | `app/store.py:100-104,326-331`；生产库查询结果 | WP4（归并规则）+ 主线（约束） |
| P2-4 | SCHEMA 无任何 `REFERENCES/FOREIGN KEY`，`PRAGMA foreign_key_check=[]` 只是「未声明」而非通过校验；`foreign_keys=ON` 形同虚设 | `app/store.py:28-123`；`grep -n "REFERENCES\\|FOREIGN KEY" app/store.py` → NONE | 主线（schema 加固） |
| P2-5 | 4 个配置项死代码：`MIN_VIDEOS_FOR_RANK`、`CONFIRM_ROUNDS`、`COMMENT_CACHE_SECONDS`、`MAX_SURFACE_LEN` 无引用；回锅（>14 天）逻辑未实现 | `app/config.py:33,35,77,62`；grep 均为 0 引用 | WP3/WP4 决策后删除或接线 |
| P2-6 | 文档与代码数值不一致：种子表 132 vs 148；docs/02 限速 2 rps vs config/README 3；docs/02「老梗回锅」无实现；docs/02「稳定上榜」与离线 9.94 分占 rank2 的口径需澄清 | `docs/02:31,76-79,95`；`config.py:17`；离线实测 | PM（文档） |
| P2-7 | docs/01 称别名表命中 +0.35，代码别名命中 +0.20 | `docs/01:66` vs `app/judge.py:87-91` | PM（文档） |
| P2-8 | README 环境变量表漏 `HOTMEME_DATA_DIR/FIXTURES/TIMEOUT/UA`；API 表漏 `GET /`、token、400/403/409、`limit` 钳位（0/-1 静默变 1） | `README.md:28-39,81-87`；`app/api.py:85,118-124` | PM（文档） |
| P2-9 | `/api/meme/{key}/history` 对不存在 key 返回 200 空数组，客户端无法区分「无历史」与「key 不存在」 | `app/api.py:92-99` 实测 | 契约讨论（可选 404） |

---

## 7. 代码级风险细节（任务 6）

| 检查点 | 结论 | 证据 |
|---|---|---|
| refresh 竞态 | **可复现**（P1-1） | 连续两次 `await api.api_refresh` 均 started；桩内最大并发=2 |
| 调度器异常后状态复位 | 正常 | 桩 raise RuntimeError：`running=False, error="boom"`（api.py:34-37 finally） |
| 异常路径留下 running cycle | 普通异常不会（failed）；`KeyboardInterrupt`/进程被杀会（P1-4） | `/tmp/qa-fail.db` 实测 |
| SSE 断连清理 | 正常 | `await gen.aclose()` 后 `SUBSCRIBERS=0`；TestClient 关闭流后也为 0 |
| SSE 慢消费 | 队列满(16)时 `put_nowait` 静默丢弃（api.py:21-22），20 次广播后 qsize=16，无报错；无丢事件计数 | 可接受降级，建议埋点 |
| SSE 心跳 | 20s 无事件发 `: ping`（api.py:153-156），无首屏超时 | 未压测 |
| 限速器 | 正常路径生效：`min_interval=0.333s`，固定间隔；curl 兜底路径失效（P1-3） | `http.py:53-64,140-166` 实测 |
| curl 兜底其他风险 | 不带 `-L`、不解析压缩/编码、超时 `timeout+5`；失败直接抛 412 错误不重试 | `http.py:151-166` 代码审阅（未真机验证） |
| `/api/top` 空库回退 | 每次请求都会 `to_thread(pipeline.snapshot)`（api.py:78-80），无缓存；轻负载可接受 | 代码审阅 |

---

## 8. 未覆盖项与风险

1. **未触及外网/真机**：B站接口、wbi 签名、真实弹幕压缩、412 兜底真实行为均未验证（禁网约束）；`data/hotmeme.db` 为 14:11 静默快照，运行态并发写未观察。
2. **未启动 uvicorn**：8000 端口、lifespan 真实启停、SSE 在真实 ASGI server 下的断连行为未验证（TestClient 已覆盖逻辑层）。
3. **跨进程并发未测**：单 uvicorn worker 假设；多 worker 下 `STATE` 与 `SUBSCRIBERS` 进程内变量会各自为政（架构风险，WP2/WP3 需考虑）。
4. **无 lint**：ruff/pyflakes 不可用且未安装；`compileall` 仅保证语法。
5. **离线 fixture 覆盖面**：只有 40 视频/252 候选，Top10 仅 2 条；不能代表真机 377 视频/1053 家族下的极端分布（如单视频刷屏、低分 seed 占榜）。
6. **榜单质量**（真梗率、常青/新闻误入、同源刷屏）属 qa-quality 的 WP1，本报告只做契约/数据/工程面，不重复判定。
7. **行号漂移**：基线 `84a4b28` 后主线仍在改 `app/*.py`，本报告 file:line 以 14:12–14:20 快照为准。
8. **`components.decay` 全为 1.0**：生产库两轮都是同一次运行写入，`first_seen≈now`，无法在现有数据下验证衰减确实生效；需跨轮/历史数据回归（建议 WP4 补用例）。

---

## 9. 给 PM 的处置建议

- **发布前必修**：P0-1（契约 `is_new`）。
- **本波 WP3 一并做**：P1-1（原子锁）、P1-3（curl 限速/计数）、P1-4（孤儿 running 清理）。
- **本波 WP4 做**：P1-2（MIN_VIDEOS 过滤）、P2-3（alias 唯一/归并策略）。
- **文档同步**：P2-6/7/8 一次性改 README、docs/01-02（由主线或 PM 执行）。
- 复跑验收时建议补：真机一轮后只读跑本报告 §5 的 SQL 套件 + §2 的 TestClient 脚本，确认 P0/P1 修复且未引入回归。
