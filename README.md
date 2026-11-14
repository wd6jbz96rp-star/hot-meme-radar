# 网络热梗 · 实时 Top10

按**热度动态排名近期网络热梗**的最小实现：以 B站 分区榜、评论、弹幕为主要语料，用「新词发现 + 规则判定」筛出真梗，按曝光、互动、广度、增速、梗性加权打分，每 10 分钟产出一个可解释的 Top10，并提供网页实时看板。

## 它解决什么问题

热搜榜排的是**事件**，不是**梗**。真正的梗有五个特征：可复制、被模仿使用、依赖出处、有语义增量、有兴衰曲线。本项目的判定与统计规范见：

- [梗的定义与判定规范](docs/01-meme-definition.md)
- [捕获与统计](docs/02-capture-and-scoring.md)

## 快速开始

```bash
# 1. 安装依赖（建议虚拟环境）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 2. 启动（首次启动会先在后台跑一轮采集，约 1-2 分钟）
.venv/bin/python run.py

# 3. 打开看板
open http://127.0.0.1:8000
```

首轮采集完成前页面显示「采集中」。之后每 10 分钟自动刷新，页面通过 SSE 实时更新。

### 常用环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOTMEME_INTERVAL` | `600` | 采集间隔（秒） |
| `HOTMEME_PORT` / `HOTMEME_HOST` | `8000` / `127.0.0.1` | 服务地址 |
| `HOTMEME_DB` | `data/hotmeme.db` | SQLite 路径 |
| `HOTMEME_MAX_RPS` | `3` | 对 B站 的最大请求频率 |
| `HOTMEME_OFFLINE` | `0` | 置 1 用 `tests/fixtures` 回放，不触网 |
| `HOTMEME_DISABLE_SCHEDULER` | `0` | 置 1 只提供 API，不自动采集 |
| `HOTMEME_TOKEN` | 空 | 设置后 `POST /api/refresh` 需要 `x-token` |
| `HOTMEME_CA` | 自动 | 自定义 CA 证书路径 |

## 工作原理

```
B站分区榜(8) + 全站热门 + 20组「热梗/梗百科」搜索视频
        ↓
标题 / 简介 / 标签 / 热评 / 弹幕  →  归一化  →  n-gram 新词发现 + 模式抽取
        ↓                                        （凝固度 + 左右邻接熵 + 通用词表）
候选  →  梗性规则判定（种子词、跨视频广度、弹幕重复、梗源覆盖、负向过滤）
        ↓
梗族归并  →  五信号分位加权 × 时间衰减 × EMA  →  Top10  →  SQLite / API / SSE / 看板
```

- **为什么是 B站**：弹幕天然是「同一句话被大量人重复」的场所，重复率本身就是梗的证据；评论提供「这是什么梗」的语义上下文。
- **为什么加搜索通道**：热榜标题以新闻/番剧为主，「热梗合集」「梗百科」类视频的标题、标签与弹幕才是梗的高密度来源。
- **为什么纯规则**：零成本、可解释、离线可跑。规则先筛，后续可接 LLM 复核 Top 候选。

## 目录结构

```
app/
  config.py       配置与阈值
  http.py         HTTP（Cookie、限速、重试、压缩、412 时 curl 兜底、CA 回退）
  bili.py         B站适配：分区榜、热门、搜索、评论、弹幕、wbi 签名
  zones.py        分区与权重
  store.py        SQLite 表与查询
  normalize.py    归一化、切段、常用词/词表加载
  extract.py      n-gram 新词发现、模式抽取、梗族归并
  judge.py        梗性规则判定
  score.py        分位归一化、加权、衰减、EMA
  pipeline.py     采集 → 抽取 → 判定 → 打分 → 落库
  api.py          FastAPI、SSE、看板路由
  static/index.html
data/             种子梗表、别名表、黑名单、通用词表、词表
docs/             定义规范与统计说明
tests/            fixtures / 黄金集 / 单元与离线集成测试
run.py            启动入口
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/top?limit=10` | 榜单（热度、名次变化、类型、代表用法、分项得分、判定依据） |
| GET | `/api/meme/{family_key}/history` | 单个梗的分数/名次曲线 |
| GET | `/api/health` | 调度状态、最近一轮、各源错误 |
| GET | `/api/stream` | SSE 推送新一轮结果 |
| POST | `/api/refresh` | 手动触发一轮（可配 token） |

## 测试

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

- 单元测试覆盖归一化、新词发现、判定、打分、存储、API。
- 离线集成测试用 `tests/fixtures` 回放一轮完整流水线，不触网。
- 黄金集 `tests/golden/memes.json`（30 梗 + 30 非梗）约束判定精度。

## 已知限制

- 匿名评论接口每页只返回约 3 条，评论语料有限，主语料仍是弹幕。
- 谐音/错别字变体依赖种子表与人工别名，无法自动发现「同音不同字」。
- 图像梗、表情包梗、BGM 空耳不在范围内。
- 冷启动首轮没有历史增速，榜单以跨视频广度、梗源覆盖与词典先验为主。
- 榜单是「当前仍活跃的梗」，老梗若仍在高频被用会继续上榜；「近期」指统计窗口，不是梗的诞生年份。

## 数据与合规

- 只访问 B站 公开接口，默认 10 分钟一轮、限速请求，不登录、不下载音视频、不保存用户身份。
- 敏感词直接剔除；新闻、营销、专有名词强降权。
- 通用词表 `data/lexicon.txt` 抽取自 [jieba](https://github.com/fxsjy/jieba) 的 `dict.txt`（MIT License），仅保留词频 ≥ 100、长度 2–6 的词。

## 后续可做

- 接入微博/抖音/头条热榜（`bili.py` 的 adapter 结构已隔离）。
- 规则先筛 + LLM 复核 Top 20–50 候选，提升谐音梗与反讽梗的识别。
- 图像/表情包梗与 BGM 空耳检测。
- 生命周期阶段标注（萌芽/爆发/平台/衰退）与订阅告警。
