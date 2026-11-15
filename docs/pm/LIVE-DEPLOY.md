# Live 部署：只读新梗榜（2026-11-16）

## 1. 当前形态

- `127.0.0.1:8000`：FastAPI 只读服务，无 scheduler、无采集、无 SQLite。
- 页面只有 **新梗榜** / **模块 shadow** 两个 tab；经典/全部、`/api/top`、`/api/meme/{key}/history` 已删除。
- `tools/meme_info_worker.sh` 每 600s 跑一轮模块查询，原子写 `/tmp/meme_info_latest.json`。

## 2. 启动

```bash
cd /Users/wanjunqiang/Desktop/hot_meme_search
HOTMEME_HOST=127.0.0.1 HOTMEME_PORT=8000 \
HOTMEME_MEME_INFO_REPORT=/tmp/meme_info_latest.json \
  .venv/bin/python run.py

MEME_INFO_WORKER_INTERVAL=600 \
  bash tools/meme_info_worker.sh
```

## 3. 验证

```bash
curl -s 127.0.0.1:8000/api/health
curl -s '127.0.0.1:8000/api/meme-info?limit=2&only=publishable'
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8000/api/top   # 期望 404
curl -s -X POST -o /dev/null -w '%{http_code}\n' 127.0.0.1:8000/api/refresh  # 期望 410
```

## 4. 回滚/归档

- 旧 SQLite 已归档到 `archive/hotmeme-20261114.db`（gitignored，不参与运行）。
- 页面回滚：`git checkout -- app/static/index.html` 可恢复旧前端，但后端已无旧数据接口。
- worker 停止后报告 1h 变 stale；UI 会显示 age/stale，不会静默伪造。

## 5. 已知限制

- worker 是会话内 shell job，不是 launchd/systemd 常驻。
- 实际周期约 860s（采集约 260s + sleep 600s），不是严格 10 分钟。
- 新梗榜只读模块报告；报告缺失/损坏时页面显示 missing/invalid，不 fallback 旧榜。

## 6. Phase 历史

- Phase 0：`/api/refresh` 410，旧 pipeline 标 deprecated。
- Phase 1：`/api/top`/history 只读化。
- Phase 2：删除旧 pipeline/规则/LLM 模块、旧测试、eval、shadow_run。
- Phase 3：删除 `/api/top`、`/api/meme/{key}/history`、`app/store.py`、SQLite 运行时；归档 DB。
- 当前测试：87 passed。
