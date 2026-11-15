# 热梗雷达 · 模块化版

用 B站公开语料发现「当前网络热梗」，输出可解释的排行榜和看板。当前架构是**两进程 + 一份 JSON 报告**：

- `meme_info` 引擎（独立库 / worker）：采集 → 元话语 + 使用双通道发现 → 热度打分 → 可选 LLM 审核；
- `FastAPI :8000`（只读服务）：新梗榜读模块报告，经典/全部读历史 SQLite 快照；
- 旧的 `app/pipeline.py` 采集排名引擎已于 2026-11-15 退役并删除。

当前架构图：`.archify/architecture-hotmeme-20261115-232136/hotmeme-current-architecture.html`（本地生成产物）。

## 快速开始

```bash
# 1. 安装
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 2. 先跑一轮模块报告（无 key 时自动降级为规则版）
HOTMEME_LLM_API_KEY_FILE=~/.hotmeme_llm_key \
  .venv/bin/python -m tools.meme_info_shadow \
  --out /tmp/meme_info_latest.json --limit 20 --verify --verify-gate

# 3. 启动只读服务
.venv/bin/python run.py
# 打开 http://127.0.0.1:8000

# 4. 另开终端跑刷新 worker（每 600s 一轮；实际约 860s，见“已知限制”）
MEME_INFO_WORKER_INTERVAL=600 bash tools/meme_info_worker.sh
```

新梗榜读 `/tmp/meme_info_latest.json`；经典/全部读 `data/hotmeme.db` 的历史 cycle（当前停在 cycle#24）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOTMEME_HOST` / `HOTMEME_PORT` | `127.0.0.1` / `8000` | 服务地址 |
| `HOTMEME_DB` | `data/hotmeme.db` | 历史快照 SQLite（只读服务；不再写入新 cycle） |
| `HOTMEME_DATA_DIR` | `data` | 未设置 `HOTMEME_DB` 时的数据目录 |
| `HOTMEME_MEME_INFO_REPORT` | `/tmp/meme_info_latest.json` | 模块报告路径 |
| `HOTMEME_MEME_INFO_MAX_AGE` | `3600` | 报告超过该秒数标 stale（仍可读） |
| `HOTMEME_OFFLINE` | `0` | 只影响 health 展示；当前无采集入口 |

模块自己的环境变量见 `meme_info/README.md`（`MEME_INFO_LLM_*` / `HOTMEME_LLM_*`）。

## 架构

```
meme_info worker（独立进程/会话内）
  └─ collect → discover → heat → optional verify
       └─ 原子写 /tmp/meme_info_latest.json
app FastAPI :8000（只读）
  ├─ GET /api/meme-info → meme_info_adapter 读 JSON → 新梗榜 / 模块 shadow
  └─ GET /api/top      → SQLite cycle#24 历史快照 → 经典 / 全部
```

- **新梗榜**：`GET /api/meme-info?only=publishable&limit=8`；旧 `/api/top?list=new` 已从 UI 下线。
- **经典/全部**：`GET /api/top?list=classic|all`，是历史快照，不自动更新。
- **模块 shadow**：`GET /api/meme-info?only=all`，看全量/观察项。

## 目录结构

```
app/
  api.py               FastAPI 只读服务（新梗榜 + 历史快照）
  config.py            服务/报告/历史库路径
  meme_info_adapter.py 只读报告 → 精简 API 响应（缓存/状态机）
  store.py             历史 SQLite 只读查询（top/history/health）
  static/index.html    看板（新梗 / 经典 / 全部 / 模块 shadow）
meme_info/             独立热梗发现与打分库（见 meme_info/README.md）
tools/
  meme_info.py         CLI 入口（python -m tools.meme_info）
  meme_info_shadow.py  跑一轮模块 → 原子写报告
  meme_info_worker.sh  600s 刷新 worker
data/                  种子/别名/黑名单/词表
docs/                  设计规范、PM 记录、部署文档
tests/                 test_api / test_meme_info_adapter / test_meme_info
run.py                 启动入口
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meme-info?limit=10&only=all\|publishable` | 新梗榜/模块 shadow；只读 JSON 报告 |
| GET | `/api/top?limit=10&list=classic\|all` | 历史快照榜（cycle#24） |
| GET | `/api/meme/{family_key}/history` | 单个梗的历史分数/名次 |
| GET | `/api/health` | 服务/历史库状态；`scheduler` 恒 false |
| GET | `/api/stream` | 兼容 SSE：只推初始快照 + ping，不再有采集广播 |
| POST | `/api/refresh` | **410 已退役**：旧采集 pipeline 不再触发 |

## 测试

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

当前 **87 passed**：

- `tests/test_meme_info/`：模块的采集解析、发现、归并、打分、审核、评测；
- `tests/test_meme_info_adapter.py`：报告状态机/精简/API 契约/只读边界；
- `tests/test_api.py`：只读服务基础契约。

旧 `tests/test_judge|score|extract|pipeline|wave2b|p3...` 随旧 pipeline 一并删除。

## 已知限制

- worker 目前是**会话内 shell job**，不是 launchd/systemd 常驻；停则报告 1h 后 stale。
- 实际周期约 **860s**（采集约 260s + sleep 600s），不是严格 10 分钟。
- 模块只覆盖 B站；图像/表情包/BGM 梗不在范围。
- 经典/全部是历史快照（cycle#24），不会自动更新。
- 模块的银标/AI 补标有循环性，P@10=1.0 不是独立人工准确率。
- LLM gate 仍可能误判泛化词/标题类；当前用用户负例 + 知识库 stopword 回归。

## 历史文档

`docs/01-meme-definition.md`、`docs/02-capture-and-scoring.md` 描述的是**旧规则引擎**的设计，保留作历史参考；当前实现以 [`meme_info/README.md`](meme_info/README.md) 为准。部署/回滚见 [`docs/pm/LIVE-DEPLOY.md`](docs/pm/LIVE-DEPLOY.md)。
