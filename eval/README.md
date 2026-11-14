# eval/ · v2 P0 评测工具

本目录只做**只读评测**：不联网、不启动服务、不写生产库、不装依赖（只用 Python 标准库）。
口径与标签规则见 [`docs/pm/EVAL-DESIGN.md`](../docs/pm/EVAL-DESIGN.md)；本轮基线见 [`docs/pm/P0-BASELINE.md`](../docs/pm/P0-BASELINE.md)。

## 文件

| 文件 | 用途 |
|---|---|
| `metrics.py` | 单轮指标：Top10、seed 命中率、origin 分布、常青/新闻/专名计数、同族重复、平均年龄、非种子占比、候选池、黄金集召回。输出 JSON + Markdown |
| `labeling-template.csv` | 50 条待标项：cycle#7 Top10 + 40 条候选（非种子优先）；`label` 列**留空** |
| `README.md` | 本文 |

## metrics.py

```bash
cd /Users/wanjunqiang/Desktop/hot_meme_search

# 固定基线轮次（推荐；数字可复现）
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7

# 只出 JSON / 只出 Markdown
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7 --format json
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7 --format md

# 最新已完成 cycle（默认；生产库每 10 分钟采集，数字会漂移）
.venv/bin/python eval/metrics.py --db data/hotmeme.db
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db PATH` | `$HOTMEME_DB` 或 `<repo>/data/hotmeme.db` | 库路径；按脚本位置推导，不写死生产绝对路径 |
| `--cycle N` | 最新 `status='done'` 的 cycle | 必须 pin 才能跨时间对比 |
| `--seed PATH` | `<repo>/data/seed_memes.json` | 种子表 |
| `--golden PATH` | `<repo>/tests/golden/memes.json` | 黄金集 |
| `--as-of YYYY-MM-DD` | 今天 | origin 年龄计算基准 |
| `--format both\|json\|md` | `both` | 输出格式 |

实现要点：

- 连接串固定 `file:<path>?mode=ro` + `uri=True`，并执行 `PRAGMA query_only=ON`；库不存在时报错退出，**不会创建空库**。
- 候选池数字分「单轮口径」（`cycle.stats_json`）与「当前表快照」（`meme`/`candidate` 总行数）两栏；后者会随采集增长，不要当单轮指标。
- 标签来自 `metrics.py` 内的 `LABELS` 快照（与 `docs/pm/P0-BASELINE.md` 一致），仅覆盖 cycle#7 Top10；未知条目按 `unclear` 计，避免臆造。
- 退出码：`0` 成功；非 0 表示库不存在 / cycle 不存在 / 只读打开失败（错误信息输出到 stderr）。

JSON 顶层字段：`cycle`、`top10[]`、`metrics`、`candidate_pool`、`golden`、`seed_audit`、`label_snapshot`。Markdown 由同一份 JSON 渲染，二者数字一致。

## labeling-template.csv

- **50 行**（不含表头）：10 条 cycle#7 Top10 + 40 条候选。
- 40 条抽样：`candidate` 中排除种子 canonical/alias、纯 ASCII/纯数字 surface、已进 Top10 的条目；按 `df_video` 降序取 30 条，再按 `df_zone` 降序补 10 条；固定排序，可复现。
- 字段：

| 字段 | 说明 |
|---|---|
| `id` | `meme:<meme.id>` 或 `cand:<norm>` |
| `canonical` | 展示词 |
| `type` | `meme.type`（候选表无 type 时为「未定」） |
| `source` | 来源表 + cycle/rank/抽样方式 |
| `first_seen` | **系统入库时间**，不是梗首现 |
| `origin_year` | 种子/`meme.origin` 的年份；无则「未知」 |
| `evidence` | sample、`components.reasons`、`meme`/`velocity`、`df_video`/`df_zone`/`total_count` |
| `label` | **留空**，由标注人填写 |
| `notes` | 证据瑕疵、origin 来源、是否已建 family |

- 可填标签：`new_meme`、`recent_meme`（扩展）、`rising_old`、`evergreen`、`news_event`、`proper_noun`、`marketing`、`unclear`；`evergreen` 可在 `notes` 标 `pure`/`generalized`。
- **不要**把 `docs/pm/P0-BASELINE.md` 的标签直接抄进模板：模板是待标项，不是答案。
- 判不准就填 `unclear` 并在 `notes` 写缺什么证据（如「仅标题出现」「origin 只有年份」）。

## 复现检查

```bash
# 1) 只读跑通，且与基线报告数字一致
.venv/bin/python eval/metrics.py --db data/hotmeme.db --cycle 7 --format md

# 2) 模板行数/字段
.venv/bin/python - <<'PY'
import csv
rows = list(csv.DictReader(open("eval/labeling-template.csv", encoding="utf-8")))
print(len(rows), list(rows[0]), all(r["label"] == "" for r in rows))
PY
```

## 边界

- 生产库仍在被采集进程写入；只读连接不会写库，但**表总数会漂移**，对比必须 pin `--cycle`。
- 观测史不足（2026-11-14 当天），`rising_old`/生命周期类指标当前不可测。
- 标签是假设而非 ground truth；本目录的结果只用于 v2 前后对比，不用于对外宣称准确率。
