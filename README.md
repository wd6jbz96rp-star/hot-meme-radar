# 热梗雷达

用 B站公开语料发现当前网络热梗，输出可解释榜单和看板。当前只剩两条链路：

```
meme_info worker（独立进程 / 会话内）
  └─ collect → discover → heat → optional LLM verify
       └─ 原子写 /tmp/meme_info_latest.json
FastAPI :8000（只读）
  └─ /api/meme-info → meme_info_adapter → 新梗榜 / 模块 shadow
```

旧的 `app/pipeline.py` 采集/排名引擎、SQLite 历史榜、`/api/top`、`/api/meme/{key}/history`、经典/全部 tab **已全部删除**。历史库归档在 `archive/hotmeme-20261114.db`（不参与运行）。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 1. 跑一轮模块报告（无 key 时自动降级规则版）
HOTMEME_LLM_API_KEY_FILE=~/.hotmeme_llm_key \
  .venv/bin/python -m tools.meme_info_shadow \
  --out /tmp/meme_info_latest.json --limit 20 --verify --verify-gate

# 2. 启动只读服务
.venv/bin/python run.py
# http://127.0.0.1:8000

# 3. 另开终端刷新报告（600s 间隔；实际约 860s）
MEME_INFO_WORKER_INTERVAL=600 bash tools/meme_info_worker.sh
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOTMEME_HOST` / `HOTMEME_PORT` | `127.0.0.1` / `8000` | 服务地址 |
| `HOTMEME_MEME_INFO_REPORT` | `/tmp/meme_info_latest.json` | 模块报告路径 |
| `HOTMEME_MEME_INFO_MAX_AGE` | `3600` | 报告超过该秒数标 stale（仍可读） |
| `HOTMEME_OFFLINE` | `0` | health 展示用 |

模块自己的参数见 [`meme_info/README.md`](meme_info/README.md)（`MEME_INFO_LLM_*` / `HOTMEME_LLM_*`）。

## 页面

- **新梗榜**：`GET /api/meme-info?only=publishable&limit=8`，只发 publishable；
- **模块 shadow**：`GET /api/meme-info?only=all`，看全量/观察项；
- 没有经典/全部 tab，也没有旧榜数据。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meme-info?limit=10&only=all\|publishable` | 新梗榜/模块 shadow |
| GET | `/api/health` | 报告状态/年龄/条数 |
| GET | `/api/stream` | 兼容 SSE：空初始快照 + ping；页面走轮询 |
| POST | `/api/refresh` | **410 已退役**：旧采集 pipeline 不再存在 |

## 目录

```
app/
  api.py               FastAPI 只读服务
  config.py            服务/报告路径
  meme_info_adapter.py 只读报告 → 精简响应
  static/index.html    新梗榜 + 模块 shadow
meme_info/             独立热梗发现/打分库
tools/                 CLI / shadow runner / worker
data/                  模块知识库（种子/别名/黑名单/词表）
archive/               旧 SQLite 归档（不参与运行）
docs/                  设计与 PM 记录
tests/                 test_api / test_meme_info_adapter / test_meme_info
run.py                 启动入口
```

## 测试

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

当前 **87 passed**：模块 49 + adapter 37 + API 1。

## 已知限制

- worker 是**会话内 shell job**，不是 launchd/systemd；停则报告 1h 后 stale。
- 实际周期约 **860s**（采集约 260s + sleep 600s），不是严格 10 分钟。
- 只覆盖 B站、文本梗；图像/表情包/BGM 不在范围。
- 模块银标/AI 补标有循环性，P@10=1.0 不是独立人工准确率。
- LLM gate 仍可能误判泛化词/标题类。
- 旧 SQLite 仅归档，不再提供历史曲线/经典榜。

## 历史文档

`docs/01-meme-definition.md`、`docs/02-capture-and-scoring.md` 描述旧规则引擎，保留作历史参考；当前实现以 [`meme_info/README.md`](meme_info/README.md) 为准。部署记录见 [`docs/pm/LIVE-DEPLOY.md`](docs/pm/LIVE-DEPLOY.md)。
