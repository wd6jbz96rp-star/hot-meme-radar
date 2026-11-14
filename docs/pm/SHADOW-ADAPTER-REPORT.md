# Shadow adapter 交付报告（meme_info → app，只读）

- 会话：shadow-adapter（2026-11-15 00:00–00:12）
- 基线：HEAD `bc640d3`（工作区另有 lead 未提交的 `docs/pm/BOARD.md` 编辑，本会话未触碰）
- 目标：把 `meme_info.HotMemeReport` 以**只读**方式接进 app 供内部 shadow 对比；**不切 `/api/top`、不写 DB、不动调度器、不重启 8000**
- 结论：**达成**。采集端 + 读端 + `/api/meme-info` + 24 个新测试全部通过（全仓 **297 passed**）；真机 shadow 跑通（137s，178 请求）；`/api/top` 行为不变、无 DB 写入。

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `tools/meme_info_shadow.py` | 新增 | 采集端：跑一轮模块查询 → 原子写 JSON → stdout 一行摘要 |
| `app/meme_info_adapter.py` | 新增 | 读端：校验/精简/缓存，状态机 `ok|stale|missing|invalid` |
| `tests/test_meme_info_adapter.py` | 新增 | 24 个测试（读端 + API + 边界） |
| `app/config.py` | 小改（+8 行） | `MEME_INFO_REPORT_PATH`、`MEME_INFO_MAX_AGE_SECONDS` |
| `app/api.py` | 小改（+16/-1 行） | `GET /api/meme-info`；`/api/top` 未动 |
| `docs/pm/SHADOW-ADAPTER-REPORT.md` | 新增 | 本报告 |

未改动（已核对）：`meme_info/**`、`app/pipeline.py`、`app/store.py`、`app/static/**`、`data/**`、`docs/01-02`、`README*`。未 commit、未装依赖。

## 2. 数据流

```
tools/meme_info_shadow.py
  └─ meme_info.find_hot_memes(BiliSource, QueryConfig)   # 模块内：采集→发现→打分→可选 LLM 审核
       └─ HotMemeReport.to_dict() → tmp 文件 → os.replace → /tmp/meme_info_latest.json（2.4MB）
app/meme_info_adapter.load_report()                      # 只读 JSON，不 import meme_info
  └─ 校验 schema/memes/必要字段 → 精简字段 → 返回 6–10KB
GET /api/meme-info?limit=10&only=all|publishable         # asyncio.to_thread，不阻塞事件循环
```

- 采集端与读端**无进程内耦合**：app 永远不 import `meme_info`，模块永远不 import `app`；
- 生产库、调度器、`/api/top` 都不经过这条链路。

## 3. 采集端 `tools/meme_info_shadow.py`

参数（与需求一致）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out` | `/tmp/meme_info_latest.json` | 报告路径；拒绝 `.db/.sqlite` 与 `data/` 目录 |
| `--cache-dir` | `/tmp/meme_info_cache`（env `MEME_INFO_CACHE_DIR`） | HTTP + LLM 文件缓存 |
| `--limit` | 20 | 报告保留条数（1–100） |
| `--comment-videos` / `--danmaku-videos` | 45 / 10 | 采集通道规模 |
| `--max-requests` | 330 | 请求预算 |
| `--expand-limit` | 20 | 定向搜索扩量 |
| `--verify` / `--verify-gate` | off | **只有显式传才调 LLM**；gate 隐含 verify |
| `--now` | 当前 | ISO 时间或 Unix 秒，用于回放 |

关键行为：

- **key 安全**：`LlmConfig.from_env()` 支持 `MEME_INFO_LLM_API_KEY` → `HOTMEME_LLM_API_KEY` → `~/.hotmeme_llm_key`；工具只输出 `configured/model`，不打印 key，不落盘；
- **无 key 降级**：stderr 打 `not_configured` 并继续出规则版，exit 0，不伪造审核结果；
- **原子写**：同目录 `tempfile.mkstemp` → `write+flush+fsync` → `os.replace`，读端不会读到半个 JSON；
- **拒写生产库**：后缀 `.db/.sqlite/.sqlite3(/wal/shm/journal)` 与 `data/hotmeme.db` 同目录一律 `SystemExit`；
- **不 import app**（测试用子进程断言 `sys.modules` 无 `app*`）；不写 SQLite、不起 FastAPI；
- **stdout 一行摘要 JSON**：`ok/out/bytes/elapsed_seconds/generated_at/memes/observing/top10[]/stats/verification/verify`。

## 4. 读端 `app/meme_info_adapter.py`

### 4.1 校验与状态机

读取顺序与状态：

| 状态 | 触发条件 | 返回 |
|---|---|---|
| `missing` | 文件不存在 | 空 memes + error，**不抛异常** |
| `invalid` | 非 JSON / 非对象 / `schema_version` 不以 `meme-info/` 开头 / `memes` 非列表 / 单条缺 `family_key`、`canonical`、数字 `score`、`status` | 空 memes + 具体 error 文案 |
| `stale` | `generated_at`（ISO，无时区按本地）年龄 > `MEME_INFO_MAX_AGE_SECONDS` | **仍返回数据**，只做标记 |
| `ok` | 其余 | 完整精简数据 |

- `generated_at` 坏掉时按文件 mtime 估年龄，且不透传坏时间；
- 状态机不抛异常：坏文件在 API 层仍是 **200 + status=invalid**，不 500。

### 4.2 精简字段（不透传 2.4MB 原始报告）

每条 meme 只给：

`rank, family_key, canonical, score(2 位小数), status, type, meaning(≤200 字), evidence_rate_2x2, publishable, verified, meta_count, usage_count, evidence`

- `verified`：`is_meme/is_current/heat/confidence/reason(≤200 字，可选 error)`；**去掉 `evidence_ids/model/cached`**；
- `evidence`：**只取前 2 条**，每条形如 `{quote(≤120 字), ctime, like, bvid}`；去掉 `comment_id/video_title/kind/zone`；
- `meta_count/usage_count`：优先 `len(meta)`/`len(usage)`，缺省回退 `signals.meta_videos/usage_docs`；**不返回 meta/usage 数组本身**；
- `stats`：白名单 17 个标量（videos/comments/danmaku/families/current_families/current_2x2/observing_families/discovery_candidates/requests/elapsed/budget_exhausted…）；不返回 `config/discovery_audit/endpoint_counts`；
- `verification`：`attempted/ok/rejected/errors + llm.{calls,prompt_tokens,completion_tokens,total_tokens} + model/configured`。

实测体积：原始 **2,445,882 B** → `limit=10` 响应 **6,332 B**（≈386× 压缩）；`limit=20` 10,361 B。

### 4.3 发布门标记（不硬过滤）

```python
publishable = status in ("hot", "rising") and signals.evidence_rate_2x2 is True
```

- `only=all`（默认）返回全部本轮 memes；`only=publishable` 只筛该标记；
- `publishable_count` 始终给出全量标记数；不修改模块结论、不 top-up。

### 4.4 缓存

- 模块级缓存 key = 文件绝对路径，signature = `(st_mtime_ns, st_size)`；命中不重读；
- 文件被原子替换后 signature 变化自动失效；`clear_cache()` 供测试/热更新；
- `threading.Lock` 保护缓存 dict；读文件在锁内（单次 ~ms，API 每次请求最多一次读）。

## 5. API 契约 `GET /api/meme-info`

| 参数 | 类型/默认 | 约束 |
|---|---|---|
| `limit` | int，10 | 越界/非整数 → 夹到 `[1, 50]`（`limit=0→1`，`999→50`） |
| `only` | `all` | `all/publishable`；非法值 → **400** |

200 响应（示例字段）：

```json
{
  "status": "ok|stale|missing|invalid",
  "generated_at": "2026-11-15T00:01:25+08:00",
  "age_seconds": 153,
  "max_age_seconds": 3600,
  "report_path": "/tmp/meme_info_latest.json",
  "count": 10, "total_memes": 20, "publishable_count": 7,
  "memes": [{"rank":1,"family_key":"吃了不烧心","canonical":"吃了不烧心","score":79.97,
             "status":"hot","type":"词","meaning":"","evidence_rate_2x2":true,
             "publishable":true,"verified":{"is_meme":true,"is_current":true,"heat":72.0,
             "confidence":0.82,"reason":"…"},"meta_count":6,"usage_count":56,
             "evidence":[{"quote":"不烧心","ctime":1792753728,"like":7,"bvid":"BV1GGHj6REow"}]}],
  "stats": {"videos":944,"comments":1025,"danmaku":2000,"current_families":42,"current_2x2":20,"requests_total":178},
  "verification": {"attempted":20,"ok":13,"rejected":8,"errors":0,"llm":{"calls":0},"model":"deepseek-chat","configured":true},
  "error": ""
}
```

- 读取走 `asyncio.to_thread(meme_info_adapter.load_report, None, limit, only)`，不阻塞事件循环；
- **`/api/top` 一行未改**；`/api/meme-info` 与 `/api/top` 无共享状态（不写 `STATE`、不碰 DB）。

## 6. 配置

```python
# app/config.py
MEME_INFO_REPORT_PATH = Path(os.getenv("HOTMEME_MEME_INFO_REPORT", "/tmp/meme_info_latest.json"))
MEME_INFO_MAX_AGE_SECONDS = _env_int("HOTMEME_MEME_INFO_MAX_AGE", 3600)
```

## 7. 测试

- 新增 `tests/test_meme_info_adapter.py`：**24 passed**（全部离线，不联网）；
- 全仓：**297 passed**（原 273 + 新 24），`pytest -q` ≈ 7s。

覆盖：

- 状态机：`missing` / `invalid`（坏 JSON、坏 schema、memes 非列表、缺 family_key/canonical/score/status、非对象）/ `stale`（2h 前）/ 新鲜边界（60s 前 = ok）/ 坏 `generated_at` 回退 mtime；
- 契约：精简字段、非法字段不外泄、`evidence` 只 2 条且截断、`verified` 截断、`meta_count/usage_count`、响应体积上限；
- 筛选：`limit=0/2/999/非整数`、`only=all|publishable|bogus`、`publishable` 标记三态、`publishable_count`；
- 缓存：二次读取不重读文件（monkeypatch 计数）、文件更新后重新加载；
- API（TestClient，`HOTMEME_DB=/tmp/...`、`HOTMEME_DISABLE_SCHEDULER=1`、`HOTMEME_OFFLINE=1` 由 conftest 提供）：ok/limit/only/missing/invalid/stale/非法 only=400；
- `/api/top`：响应键集合不变、无 `meme` 字段；调 `/api/top` + `/api/meme-info` 前后 **DB 文件 `(mtime_ns, size)` 完全一致**；
- 边界：子进程断言 `app.meme_info_adapter` 不 import `meme_info`、`tools.meme_info_shadow` 不 import `app`；采集端拒写 `.db`/`data/`；原子写无 `.tmp` 残留。

## 8. 真机 shadow 结果（2026-11-15 00:01:25 生成）

命令（/tmp 报告 + warm 缓存；未起 8000、未写 `data/hotmeme.db`）：

```bash
python -m tools.meme_info_shadow \
  --out /tmp/meme_info_latest.json \
  --cache-dir /tmp/mi_cache \
  --limit 20 --comment-videos 45 --danmaku-videos 10 --max-requests 330 --expand-limit 20 \
  --verify --verify-gate
```

| 指标 | 值 |
|---|---|
| exit / 耗时 | 0 / **137.1s**（collection 80.9s + 打分 0.07s + 审核） |
| 请求 | **178**（预算 330，未触顶；cache 命中多） |
| 语料 | 944 视频 / 1,025 评论 / 2,000 弹幕 / 4,709 docs |
| 结果 | `memes=20`、`observing=20`、`current_families=42`、`current_2x2=20` |
| 报告文件 | `/tmp/meme_info_latest.json`，2,445,882 B，`schema_version=meme-info/v1` |
| LLM 审核 | key `configured=true`（`deepseek-chat`）；`attempted=20, ok=13, rejected=8, errors=0`；**`llm.calls=0`**（20 条 verdict 全部命中既有 prompt_version+证据缓存，本轮无新 LLM 请求、无新增费用） |
| gate | 已开启（报告里 8 条按 LLM 结论剔除/降级；规则榜条目仍在 `candidates`） |

输出摘要（采集端 stdout 一行）：

```json
{"ok":true,"memes":20,"elapsed_seconds":137.1,
 "top10":["吃了不烧心","吴家村","闪身步","司凤音乐","中国人能飞","灭建国","不小心惹了上帝","扎啤","MJ宇宙","老牧师"]}
```

## 9. 模块 Top10 vs 当前 app cycle Top10

app 基线：`data/hotmeme.db` 最新完成轮 **cycle 24（2026-11-14 17:43:33–17:43:52）**，即比本次 shadow **早约 6.5 小时**；`/api/top`（`list=all`）实测 Top10 仍是种子/老梗。对比（用 DB 副本 + TestClient 实测，见 `/tmp/shadow_api_probe.json`）：

| # | meme_info（00:01） | 分 | 状态 | 2×2 | 可发布 | | app `/api/top`（17:43，cycle 24） | 分 |
|---:|---|---:|---|---|---|---|---|---:|
| 1 | 吃了不烧心 | 80.0 | hot | ✅ | ✅ | | 抽象 | 73.0 |
| 2 | 吴家村 | 76.0 | hot | ✅ | ✅ | | 咕咕嘎嘎 | 72.3 |
| 3 | 闪身步 | 74.2 | hot | ✅ | ✅ | | yyds | 64.5 |
| 4 | 司凤音乐 | 69.9 | hot | ✅ | ✅ | | 蚌埠住了 | 64.0 |
| 5 | 中国人能飞 | 66.0 | hot | ✅ | ✅ | | 哈基米 | 63.2 |
| 6 | 灭建国 | 65.4 | hot | ✅ | ✅ | | 嘉豪 | 60.6 |
| 7 | 不小心惹了上帝 | 61.7 | hot | ❌ | ❌ | | 整活 | 59.5 |
| 8 | 扎啤 | 59.7 | hot | ❌ | ❌ | | 那咋了 | 53.7 |
| 9 | MJ宇宙 | 46.9 | observing | ❌ | ❌ | | 麻了 | 51.5 |
| 10 | 老牧师 | 45.2 | rising | ✅ | ✅ | | 吃瓜 | 50.1 |

- **Top10 overlap = 0/10**；模块 Top10 **10 条全部是 app 当前 Top10 的新面孔**，且全部不是 app 旧榜里的种子老梗；
- 模块 Top10 中 **7 条命中用户 16 条盲标锚点**（吃了不烧心/吴家村/司凤音乐/中国人能飞/灭建国/扎啤/MJ宇宙）；另 3 条（闪身步/不小心惹了上帝/老牧师）不在 16 锚点集，需人工复核（其中「不小心惹了上帝」「扎啤」2×2=false，已被 publishable 标记拦住）；
- 用户锚点里的老梗 `PiuPiuPiu/狗尾草表情包/雪豹眉眉/没希望，佩不上` 仍在模块 Top20（11–15 名），未丢；
- 复核标记的 4 条假阳性：`最怕BLG夺冠之人`、`非凡之爱`、`明码标价` 不在 Top20；`腋毛攻击` 仍在 Top20（#14，observing，2×2=false，publishable=false）——发布门层面不会下发；
- app 当前库的 `list=new` 实测为空（cycle 24 属旧 schema 轮次，`is_new` 仅 2 条、且无 P1+ 字段），这正是模块可补的位；**本会话未切、未改 `/api/top`**。

## 10. `/api/meme-info` 实测 JSON 摘要（真机报告 + DB 副本 + TestClient）

见 `/tmp/shadow_api_probe.json`。关键值：

```
status=ok, age_seconds=153, count=10, total_memes=20, publishable_count=7
publishable=[吃了不烧心,吴家村,闪身步,司凤音乐,中国人能飞,灭建国,老牧师]
stats={videos:944, comments:1025, danmaku:2000, current_families:42,
       current_2x2:20, requests_total:178, budget_exhausted:false}
verification={attempted:20, ok:13, rejected:8, errors:0, llm:{calls:0}, model:deepseek-chat, configured:true}
```

测试方式：`cp data/hotmeme.db /tmp/hotmeme_shadow_copy.db`，env `HOTMEME_DB=/tmp/hotmeme_shadow_copy.db HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_OFFLINE=1`，进程内 `TestClient` 调 `GET /api/meme-info` 与 `GET /api/top`——**不改生产库、不起 8000**。（当时 127.0.0.1:8000 实测 connection refused，8000 未运行。）

## 11. 未做项 / 风险 / 后续建议

1. **未切换任何线上行为**：`/api/top`、调度器、`list=new` 均未动；看板未接 `/api/meme-info`（无 static 改动）；
2. **8000 未运行**（探测 connection refused），因此没有"线上进程内实测"；`/tmp/meme_info_latest.json` 已就位，下次启动/重启后 `GET /api/meme-info` 即可用；
3. **LLM 本轮 calls=0** 是缓存命中（不是断网）；如需验证新调用链，可清 `--cache-dir` 后重跑（约 ¥0.03–0.05/轮）。`attempted=20 / ok=13 / rejected=8` 中 ok+rejected=21 是模块 `_attach_verification` 的既有计数口径（被 gate 剔除项先计 ok 再计 rejected），非本 adapter 问题；
4. **单轮结果，未做同 `--now` 双跑稳定性**；建议下一步按 BOARD §22 连跑 2–3 轮，人工看新面孔与假阳性；
5. `/api/meme-info` 默认 `limit=10`；上线前建议加看板/内部页，并把 `only=publishable` 作为展示默认（当前默认 all，符合 shadow 全量对比要求）；
6. app 当前 DB 基线（cycle 24）比模块报告旧 ~6.5h，等 app 侧恢复采集后再做同轮对比更公平；本轮对比结论只说明"旧规则榜 vs 新模块榜"；
7. 生产库 `data/hotmeme.db` 主文件全程未变（mtime 仍为 11-14 17:43；读取使用 `mode=ro` URI；`-wal` 为 0 字节、无内容写入）；未 commit。

## 12. 复现

```bash
# 采集（真机，写 /tmp；可选 --verify）
.venv/bin/python -m tools.meme_info_shadow --out /tmp/meme_info_latest.json \
  --cache-dir /tmp/mi_cache --limit 20 --verify --verify-gate

# 测试（离线）
.venv/bin/python -m pytest tests/test_meme_info_adapter.py -q   # 24 passed
.venv/bin/python -m pytest -q                                   # 297 passed

# 读端（临时 DB，仅进程内 API；不写生产库）
.venv/bin/python -c "from app import meme_info_adapter; import json; \
print(json.dumps(meme_info_adapter.load_report(limit=10), ensure_ascii=False)[:500])"
```
