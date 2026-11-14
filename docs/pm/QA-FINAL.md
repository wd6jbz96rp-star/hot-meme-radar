# QA-FINAL · 发布前最终验收（Wave 2D，只读）

- 会话：qa-final（`9de9090c-ffc5-4cac-b403-6479c6b6edad`），委派方 lead
- 时间：2026-11-14 14:38–15:05（Asia/Shanghai）
- 基线：git `4f1272f`（质量 `f4629e5` + 看板 `0bb3d3a` + 工程 `4f1272f`）；`git status` 仅本文件新增
- 约束遵守：只写本文件；未改 `app/`、`tests/`、`data/`、`README`、`docs/01-02`、`static`；不联网；不跑真机采集；未写 `data/hotmeme.db`（只读用 `file:data/hotmeme.db?mode=ro`，本轮实际只读 `/tmp/wave2b.db`）；未 kill/重启 8000；未装依赖；未 commit。
- 8000 端口旧进程（PID 34015，14:24 启动）**全程未触碰**，本轮结束仍监听。

**统一测试 env（所有 TestClient / 临时库命令均为该形态）**

```bash
HOTMEME_OFFLINE=1 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/qa-final-*.db \
  .venv/bin/python ...
# 8011 服务：HOTMEME_PORT=8011 HOTMEME_HOST=127.0.0.1 HOTMEME_DB=/tmp/qa-final-serve.db \
#           HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_OFFLINE=1 .venv/bin/python run.py
```

## 0. 发布判定（TL;DR）

| 项 | 结论 |
|---|---|
| 判定 | **有条件发布**（代码级可发布；发布动作需重启 8000 旧进程） |
| P0（必须修） | **0** |
| P1（应修） | **3** |
| P2（可延后） | **8** |
| 最关键 3 条 | ① 8000 上跑的是 14:24 旧进程，`is_new`/`scheduler`/单飞/2rps 全部未生效，**不重启等于没发布**；② Top10 10/10 种子命中（`seed_hit=0.35`），新梗曝光为 0，产品「实时热梗」价值打折（WP4b）；③ 采集单飞只在单进程内成立，`run.py` 无多 worker 守护，运维一旦加 `--workers` 即回到双跑 |

发布前置动作（非代码）：重启 8000 进程 → `curl localhost:8000/api/top | jq '.top[0].is_new'` 应返回布尔值、`/api/health` 的 `scheduler` 应为 `true`。

## 1. 测试（任务 1）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `HOTMEME_OFFLINE=1 HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_DB=/tmp/qa-final-pytest.db .venv/bin/python -m pytest -q` | **50 passed, 1 warning in 2.29s**（期望 50 ✓；warning 仅 Starlette TestClient 的 httpx 弃用提示） |
| 2 | `.venv/bin/python -m compileall -q app tests run.py` | **exit 0**（无输出） |
| 3 | 提取 index.html `<script>`（1 块 / 17757 字符 / HTML 569 行）→ `node --check /tmp/qa-final-board.js` | **exit 0**（Node v24.22.0） |

## 2. API 契约（任务 2，TestClient + `/tmp/qa-final-api.db`，离线跑一轮后测）

脚本 `/tmp/qa-final-api.py`，输出 `/tmp/qa-final-api-out.json`。离线一轮：`status=done, top=2`（fixture 只有 2 条可用，属已知离线局限）。

| 用例 | 期望 | 实测 | 判定 |
|---|---|---|---|
| `/api/top` top 项含 `is_new` | bool | 14 键含 `is_new`，`all(isinstance(x,bool))=True`，值 `[True, True]`（首轮全真，见 P2-6） | **PASS** |
| top 项含 `components` | dict | 10 键：`breadth, concentration, decay, engagement, exposure, meme, previous, raw, reasons, velocity` | **PASS** |
| `limit` 边界 `10/1/0/-1/51/999` | 钳位 1..50 | `[200,2] [200,1] [200,1] [200,1] [200,2] [200,2]` —— 0 与负数静默变 1、999 变可用条数，均 200 不报错（P2-2） | **PASS（契约未文档化）** |
| `/api/health` OFFLINE 下 `scheduler` | false | `{scheduler: False, offline: True, ready: True, running: False}` | **PASS** |
| `/api/meme/{key}/history` | 200 + `{family_key,history[]}` | 200，1 个点；不存在的 key 也 200 + 空数组（P2-5） | **PASS** |
| `/api/stream` 首事件 | `data: {snapshot}` | `content-type: text/event-stream`，首行 `data: `，负载键 `{cycle, health, status, top}` | **PASS** |
| `/api/refresh` OFFLINE | 400 | `400 {"detail":"离线模式不可刷新"}` | **PASS** |
| `/api/refresh` 并发（桩 1.2s 慢采集，OFFLINE=0 + DISABLE_SCHEDULER=1） | 一次 200、其余 409 | 顺序两次 await：`started=True` / `409 已有采集任务在跑`；`asyncio.gather` 真并发：`started / 409`，**`run_cycle` 实际调用 1 次**；真实 HTTP 双击（TestClient）：`200 / 409`，`run_cycle` **1 次** | **PASS** |
| 残留 `running` | 0 | `SELECT COUNT(*) FROM cycle WHERE status='running'` = 0 | **PASS** |

## 3. 工程修复独立复核（任务 3，`git show 4f1272f`，10 文件 +745/−62）

| 项 | 判定 | 证据 |
|---|---|---|
| (a) `MAX_RPS` 默认 2.0 + start-to-start 有测试 | **真修好** | `config.py:17` 读 env 缺省 `2.0`；`http.py:55-75` 锁内槽位预约、锁外 sleep，`_next = slot + min_interval` 即 start-to-start；`tests/test_wave2b.py:38` 用 `FakeClock` 断言精确时刻（100.0 → 100.5 → 101.3）与 sleep 次数（1 次），不是「只测没崩」的表面测试 |
| (b) cid `COALESCE` + 回写 | **真修好** | `store.py:229` `cid=COALESCE(excluded.cid, cid)`；`cid_map()`(:236) 批量读、`update_video_cid()`(:249) 回写；`pipeline.py:100-107` 命中即跳过 pagelist。数据侧：`/tmp/wave2b.db` warm 轮 `pagelist_requests=0`（冷轮 25），`梗搜索` 区 `with_cid` 0→25 |
| (c) 评论缓存 TTL + `comment_cached` | **真修好** | `store.py:318/336` TTL 取 `COMMENT_CACHE_SECONDS=1800`，`<=0` 关闭，`_fresh` 判过期；`pipeline.py:136-149` 命中仍写 `text_sample`、`stats.comments` 口径不变。数据：warm 轮 `comment_cached=30 / comment_requests=0 / comments=90`；3 条测试（命中/过期/关闭）均在 |
| (d) `_start_collection` 同步占位消除双跑、scheduler 与 refresh 共用 | **真修好** | `api.py:49-56`：`_collection_active()` 检查 → `STATE["running"]=True` → `create_task`，**检查与置位之间无 `await`**；`api.py:154-159` refresh 先判 409 再调入口（双检）；scheduler(`:61`) 与 refresh 同一入口；`grep -rn run_cycle app` 仅 `api.py:39` 一处（唯一执行点）。实测并发双击 `run_cycle` 调用 1 次、第二次 409（修复前 qa-verify 复现为 2 次） |
| (e) `BaseException` 收尾 + 启动 `aborted` 回收 | **真修好** | `pipeline.py:266` `cycle_id: int|None = None` + `start_cycle` 移入 `try`（:268）；`:436` `except BaseException` 先 `finish_cycle('failed')` 再 `raise`。独立复现：桩 `fetch_videos` 抛 `KeyboardInterrupt` → 异常向上传播、cycle 落 `failed`（`finished_at` 非空）、`running` 残留 0、`source_error=(cycle 1,'pipeline','ctrl-c')`。`store.abort_running_cycles`(:175) 手工造 running 后返回 1→`aborted`、二次调用返回 0（幂等）；lifespan(:71) 启动即调用 |
| (f) curl 兜底先限速再计数 | **真修好** | `http.py:207-208` `self.limiter.wait()` → `self._count_request(url)` → `subprocess.run`（顺序在 `which` 预检之后）；`_count_request`(:116) 加 `threading.Lock`；测试 `test_curl_get_waits_before_request_and_counts` 断言事件序 `["wait","run"]` 且计数 1、端点归 `comments`。冷轮 `endpoint_counts.comments=59` vs `comment_requests=30`（差 29 = 29 次 412 的 curl 兜底）证明兜底已计入 |

结论：6/6 **真修好**，未发现「表面修好」或回归。附注：`_start_collection(reason)` 的 `reason` 参数未使用（死参，P2-8），`_run_collection` 的 `STATE["running"]=True` 与 `_start_collection` 重复置位（无害冗余）。

## 4. 数据证据（任务 4，`/tmp/wave2b.db` 存在；`sqlite3.connect("file:...?mode=ro", uri=True)`）

| 检查 | 实测 | 判定 |
|---|---|---|
| cycle | #1 done 14:34:17→14:35:52；#2 done 14:35:52→14:36:10；`PRAGMA integrity_check=ok` | PASS |
| warm 轮 `requests<=40` | **31** | PASS |
| warm 轮 `elapsed<=30` | **18.0s** | PASS |
| warm 轮 `pagelist_requests=0` | **0**（冷轮 25） | PASS |
| warm 轮 `comment_cached>0` | **30**（冷轮 0） | PASS |
| `endpoint_counts.comments` vs `comment_requests` | 冷轮 59 vs 30（差 29 = 412→curl 兜底）；warm 轮 endpoint 无 comments 键 vs 0（差 0，全缓存命中） | PASS（口径差异已在 docs/02 §7.1 写明） |
| `running` 残留 | `COUNT(*)=0` | PASS |
| `meme_signal.is_new` 有值 | 无 NULL；cycle1 12/12 为 1（首次观测）、cycle2 0/12 为 1（回归族） | PASS（语义=「首次进入本系统观测历史」，与 docs/02:81 一致） |
| 冷轮 | 165 请求 / 95.1s（`comment_requests=30`，`danmaku_cached=0`） | 已知风险，见 §6.1 |

种子主导量化（cycle2 `components_json.reasons`）：Top10 **10/10** 均含 `seed_hit=0.35`、`alias_hit=0`；`meme` 0.57–0.72，「我的刀盾」仅 35.55 分仍占 rank10（seed 豁免 `passes_meme_gate`）。

## 5. 文档一致性（任务 5）

| 项 | 结论 |
|---|---|
| README 环境变量表 vs config | **一致**。代码 `HOTMEME_*` 14 个（DATA_DIR/DB/FIXTURES/OFFLINE/DISABLE_SCHEDULER/INTERVAL/TIMEOUT/MAX_RPS/HOST/PORT/TOKEN/UA/COMMENT_CACHE_SECONDS/CA），README 表 14 个，集合完全相同（QA-VERIFY P2-8 已修） |
| README `MAX_RPS=2.0` vs config | **一致**（`2.0`） |
| docs/01 阈值 | **一致**：`MEME_MIN=0.45`（docs/01:87）、`MIN_SCORE=55`/`OBSERVE_MIN=45` 量纲说明、种子免入池阈值（`passes_meme_gate(seed_hit)`） |
| docs/01 权重 | **一致**：种子 `+0.35`、别名 `+0.20`（docs/01:66 vs `judge.py:78/81`）、新闻 −0.40/营销 −0.30/通用词 −0.30/UP 主 −0.30/恒青 −0.05/纯数字 −0.30/超短英文 −0.20 全部对上 |
| docs/01 种子豁免 | **1 处不实（P2-1）**：docs/01:87 称种子豁免「长度」，代码 `judge.py:150` `if len(norm) < 2 or len(norm) > 12: score -= 0.15` 无 `not seed`，种子同样被扣。种子均为 2–12 字，实际不触发，无功能性影响 |
| docs/02 限速 | **一致**（§7 两处 2 rps） |
| docs/02 缓存/指标 | **一致**：§7.1 弹幕 24h、评论 1800s、cid `COALESCE`、warm `pagelist_requests=0`、`requests/endpoint_counts/comment_requests/comment_cached` 口径与代码实测量完全对应 |
| docs/02 种子数 | **一致**：148（文件内 `memes` 数组，`data/seed_memes.json`） |
| docs/02 稳定榜 | **一致**：`CONFIRM_ROUNDS=2`→`hype_streak rounds=max(1,CONFIRM_ROUNDS-1)`（`pipeline.py:242`）；`MIN_VIDEOS_FOR_RANK` 已接线（`pipeline.py:251`） |
| `MAX_RPS` 2.0 全仓一致 | **一致**（`config.py:17`、`http.py:63`、README:37、docs/02 §7；仅历史报告/看板 `docs/pm/*` 保留旧值叙述，属快照不算偏差） |

## 6. 看板回归（任务 6）

| 检查 | 结果 |
|---|---|
| `node --check`（提取单 `<script>`） | **exit 0** |
| 字段引用仍存在 | `is_new`（`index.html:180-181`，优先读 + 真值回退）、`components`（:408）、`/api/meme/{key}/history?limit=60`（:279）、`status` 状态机均仍引用，无改名/无死引用 |
| 8011 临时服务（`HOTMEME_DB=/tmp/qa-final-serve.db`，`/tmp/wave2b.db` 副本） | `GET /` → **200**；`GET /api/top?limit=10` → **200**，`status=done, cycle=2, top=10`，键含 `is_new`/`components`，`is_new` 10/10 为 `false`（与库一致） |
| 收尾 | 8011 已停止（`lsof` 计数 0）；8000 旧进程未被触碰（仍 PID 34015 监听）。副作用仅 `/tmp/qa-final-serve.db` |

## 7. 问题清单

### P0（必须修）— 0 项

无。契约 `is_new`/`components` 已成立，六项工程修复全部真修好，测试 50 passed，看板无断裂引用。

### P1（应修）— 3 项

| ID | 问题 | 证据 | 影响 / 建议 |
|---|---|---|---|
| P1-1 | **8000 上运行的是 14:24 旧进程**（旧代码），本轮所有修复未生效 | `lsof -iTCP:8000` PID 34015 监听；HEAD `4f1272f` 改动未加载 | 发布动作必须重启；重启后自检 `/api/top` 的 `is_new` 为 bool、`/api/health.scheduler=true`（非 OFFLINE） |
| P1-2 | **Top10 10/10 种子命中，新梗曝光 0** | `/tmp/wave2b.db` cycle2 reasons 全含 `seed_hit=0.35`；「我的刀盾」35.55 分占 rank10（seed 豁免入池线） | 产品「实时热梗」价值打折；已由 PM 列入 WP4b（证据抽取/新梗曝光），不阻断 v1.1 |
| P1-3 | **单飞只在单进程内成立**，无部署守护 | `api.py` 的 `RUN_LOCK`/`_COLLECT_TASK`/`STATE` 均为进程内对象；`run.py` 无 worker 校验；多 uvicorn worker 下各自为政 | 运维一旦加 `--workers>1` 立刻回到双跑（B站请求翻倍、SQLite 双写）。建议 README/启动日志显式声明「单 worker」，或加进程级锁 |

### P2（可延后）— 8 项

| ID | 问题 | 证据 |
|---|---|---|
| P2-1 | docs/01 称种子豁免「长度」，代码仍扣 −0.15 | `docs/01:87` vs `judge.py:150` |
| P2-2 | `limit=0/-1` 静默钳位为 1，README API 表未写钳位与 400/403/409 | 实测 `[200,1]`；`api.py:117` |
| P2-3 | `components.raw` 被 pipeline 覆盖为 `raw_signals` dict（score 侧是 float） | `pipeline.py:413` `"raw": item.get("raw_signals", {})` |
| P2-4 | `MAX_SURFACE_LEN` 仍是死配置（全仓仅 `config.py:63`） | `grep -rn MAX_SURFACE_LEN app tests --include=*.py` → 1 处 |
| P2-5 | `/api/meme/{不存在}/history` 返回 200 + 空数组，客户端无法区分 | 实测 200/0 点 |
| P2-6 | 冷启动首轮 `is_new` 全真（12/12），看板首日 10/10 标「新晋」 | `/tmp/wave2b.db` cycle1；语义自身自洽（首次观测即新），但需 UI 文案或「系统首轮不算新」约定 |
| P2-7 | 评论 412 面 29/30，curl 兜底是热路径；无 curl 环境（如 slim 容器）评论整体降级 | `bili.py:373-375` catch 后 `_record_error` 返回 `[]` → 整轮 `degraded`，不崩但评论语料归零 |
| P2-8 | `_start_collection(reason)` 的 `reason` 未使用；`STATE["running"]=True` 在 `_start_collection` 与 `_run_collection` 重复置位 | `api.py:49-56`、`:37` |

## 8. 已知风险（发布披露）

1. **冷启动 95.1s**：2 rps 合规下冷轮 165 个 HTTP 请求（含 29 次 412→curl 兜底）；README 写「首次 1–2 分钟」仍成立，但首屏空白接近 1.5 分钟；warm 轮 18.0s。压缩需降请求数（限制评论目标）或另立并发方案，不在本轮。
2. **Top10 种子主导**：见 P1-2，10/10 种子命中；新梗只能靠 `NEWCOMER_PERCENTILE` 边缘渗透。
3. **单进程单飞**：见 P1-3。
4. **旧进程需重启生效**：见 P1-1，发布步骤不可省。
5. **质量基线**（承接 QA-QUALITY，未在本轮复测）：真梗率放宽口径 60%、严格 20%、黄金集召回 16.7%。
6. **看板 SSE 慢消费丢事件**（队列 16 满即丢，无计数）为既定降级；`health.scheduler` 语义是「调度任务存活」而非「本轮完成」。

## 9. 未覆盖 / 局限

- 未触网、未跑真机：B站接口行为、412 与 curl 兜底的真实成功率沿用 WP2B 记录（本次只读其数据）。
- 生产库 `data/hotmeme.db` 只做了存在性/端口层面检查，未执行写操作；本轮未新增生产 cycle。
- 多 worker / 跨进程并发、uvicorn 真实 ASGI 下的 SSE 断连、Safari/Firefox 看板未实测。
- `docs/pm/*` 历史报告（QA-VERIFY/ASYNC-DESIGN）中的旧数值（`MAX_RPS=3`、种子 132）为当时快照，本次未回改，不算现行文档偏差。
- 无 lint（ruff/pyflakes 未安装且不装）；`compileall` 仅保证语法。

## 10. 证据索引

```
/tmp/qa-final-api.py            API 契约脚本（TestClient + 桩慢采集）
/tmp/qa-final-api-out.json      契约实测输出
/tmp/qa-final-board.js          index.html 提取脚本（node --check 对象）
/tmp/qa-final-top.json          8011 /api/top 响应
/tmp/qa-final-serve.log         8011 服务日志
/tmp/qa-final-api.db            API 契约临时库
/tmp/qa-final-ki.db             KeyboardInterrupt/abort 复现库
/tmp/qa-final-pytest.db         pytest 目标库
/tmp/wave2b.db                  WP2B 真机两轮（只读）
```
