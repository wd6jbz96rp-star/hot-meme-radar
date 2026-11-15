# Live 部署记录：新梗榜模块版（2026-11-15 01:23）

## 1. 当前形态

- `127.0.0.1:8000`：uvicorn 已启动，**`HOTMEME_DISABLE_SCHEDULER=1`**（旧 pipeline 暂停，不再写生产库、不再抢 B站 2rps）。
- 新梗榜：模块 `publishable`（`GET /api/meme-info?only=publishable&limit=8`），旧 `/api/top?list=new` 从 UI 下线。
- publishable 口径（2026-11-15 01:30 调整）：`hot/rising` 且（2×2=true **或** LLM `is_meme/is_current=true`）；2×2 仍作为证据徽标。当前报告 15 条 publishable，展示前 8 条；`灭建国/扎啤/老实赚钱` 已恢复可见。
- 经典/全部：仍走 `/api/top`，当前停在 cycle#24（11-14 17:43），**不会自动更新**；这是暂停旧引擎的预期代价。
- `/tmp/meme_info_worker.log`：worker 每 600s 跑一轮 `tools/meme_info_shadow.py`，原子更新 `/tmp/meme_info_latest.json`。

## 2. 启动命令

```bash
cd /Users/wanjunqiang/Desktop/hot_meme_search
HOTMEME_HOST=127.0.0.1 HOTMEME_PORT=8000 \
HOTMEME_DB="$PWD/data/hotmeme.db" \
HOTMEME_MEME_INFO_REPORT=/tmp/meme_info_latest.json \
  .venv/bin/python run.py

MEME_INFO_WORKER_INTERVAL=600 \
  bash tools/meme_info_worker.sh
```

## 3. 验证（启动后）

```bash
curl -s 127.0.0.1:8000/api/health
curl -s '127.0.0.1:8000/api/meme-info?limit=3&only=publishable'
curl -s '127.0.0.1:8000/api/top?limit=2'
```

- 启动时 `scheduler=false`、`/api/top` cycle#24、`/api/meme-info status=ok`。
- 生产库 `sha256=8b533acb…`、mtime 00:52，启动 `init_db` 后**未变化**。
- worker 首轮预计 70–210s；完成后 `generated_at` 应接近当前时间、`age_seconds` < 600。

## 4. 回滚

1. `kill` 8000 与 worker（或删掉 worker 循环）。
2. `git checkout -- app/static/index.html` 可恢复旧 UI；`/api/top` API 一直未动。
3. 不要恢复旧 `list=new` 作为新梗榜——它在 warm 库仍是 rank=0 噪声，已确认结构性不可用。
4. 若要恢复旧 pipeline 采集：去掉 `HOTMEME_DISABLE_SCHEDULER=1`；但这会重新写生产库、与新引擎抢 B站 请求，不建议在 shadow 验收前开启。

## 5. 已知限制

- 新梗榜按 `publishable` 展示前 8 条；2×2=false 但 LLM 通过的用户确认真梗也会进入（如 `灭建国/扎啤`）。`publishable_count` 为全量（当前 15），不是展示条数。
- 模块报告刷新依赖 worker；worker 停则 1h 后 stale（UI 会提示）。
- 经典/全部在 scheduler 暂停期间是静态快照。
- 生产库事故已止损；当前字节等于 00:11 副本，无业务行变化（`docs/pm/INCIDENT-20261115.md`）。

## 6. 后续

- 观察 1–2 小时 report age/新梗榜条数；
- 若后续想更严格，可把 `publishable` 改回 2×2 硬门槛；当前选择是为了不误杀用户确认的弱证据真梗。
- 将 worker 改成 launchd/systemd 常驻，或并入架构里的独立 worker。


## 7. Phase 0（2026-11-15 00:40）

- `/api/refresh` 已改为 **410 Gone**，不再触发旧 `pipeline.run_cycle`；避免手动刷新写生产库/抢 B站/复活种子榜。
- `app/pipeline.py` 已标 `DEPRECATED`；scheduler 保持关闭。
- 新梗榜仍由 `tools/meme_info_worker.sh` 刷新；回滚旧采集需要显式改代码，不再是一个 HTTP 请求。


## 8. Phase 1–3 完成（2026-11-16）

- **Phase 1**：`api.py` 已移除旧 pipeline 依赖；`/api/top` 只读 `store` 的历史快照。
- **Phase 2**：删除 `app/pipeline.py`、`bili.py`、`http.py`、`zones.py`、`normalize.py`、`extract.py`、`judge.py`、`score.py`、`llm.py`、`review.py`、`llm_extract.py`；删除对应旧测试、`tools/shadow_run.py`、`eval/`。
- **Phase 3**：`store.py` 重写为只读历史仓库（231 行），只服务 `/api/top`、`history`、`health`；SQLite 不再写新 cycle。
- 测试：87 passed；旧采集/排名代码不再存在，scheduler 概念移除。
