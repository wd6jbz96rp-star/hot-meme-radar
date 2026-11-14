# meme_info 工程师交付 · PM 复核

- 复核对象：commit `7a585af feat(meme-info): 独立热梗查询与热度打分模块`
- 复核时间：2026-11-14 22:25
- 方法：读代码/报告 + 跑测试 + **独立真机重跑**（21:xx 的 `/tmp/mi_check*.json`）
- 结论：**架构与工程边界通过；质量结论不通过，暂不组装进 app 生产链路。**

## 1. 通过项

| 项 | 结果 |
|---|---|
| 独立模块 | 不 import `app/`，不写 SQLite，不起 FastAPI；边界干净 |
| 测试 | `tests/test_meme_info` **49 passed**；全仓 **263 passed** |
| 文档/契约 | `Comment/Evidence/Candidate/Meme/HotMemeReport` 可 JSON 化；`schema_version`/`prompt_version` 有 |
| 数据通道 | 元话语（梗百科/指南/盘点）+ 使用（评论/弹幕）+ 搜索 expansion；方向正确 |
| LLM | 默认 shadow、失败降级、key 文件、prompt 只给证据；设计正确 |
| 架构价值 | 比旧 `app` 的「种子表 ∩ 热度」强，Top 不再被老种子占满 |

## 2. 不通过项（有证据）

### 2.1 独立重跑 P@10 = 0.7，不是报告里的 1.0

```bash
# 规则版（无 gate）
python -m tools.meme_info query --out /tmp/mi_check.json \
  --comment-videos 45 --danmaku-videos 10 --max-requests 330 \
  --cache-dir /tmp/mi_cache --expand-limit 20 --verify \
  --verify-limit 40 --now 2026-11-14T22:25:00+08:00 --quiet

# 带 gate
python -m tools.meme_info query --out /tmp/mi_check_gate.json ... --verify-gate ...
python -m tools.meme_info eval --pred /tmp/mi_check_gate.json \
  --labels meme_info/labels/current_20261114.json --k 10
```

- 带 gate：40 条送审，**14 条 rejected**；Top10 P@10 **0.70**、current P@10 **0.70**、label coverage 0.70，unmatched `美国/鬼畜/绷住`。
- 报告里的 P@10=1.0 只在 19:35 的快照成立；22:25 同命令族不成立。**数据漂移/门控/采样任一都会改变结果，不能对外宣称 1.0。**
- 规则版（无 gate）Top 里出现 `音乐`（#4，68.35）：普通词被当成梗；带 gate 后 LLM 正确剔除（`is_meme=False`，heat 5），说明 gate 有用，但默认榜不能直接用。

### 2.2 具体代码缺陷

1. **`is_generic` 写好了但没接上**：`meme_info/knowledge.py:149` 有 `is_generic()`（含 `lexicon` 判断），但 `blocked_reason()`（`:166-179`）只查 `common_words`，不调用 `is_generic`。`音乐` 在 `data/lexicon.txt` 里，因此漏过。修复：`blocked_reason` 对非种子调用 `is_generic`，并补测试 `音乐/扎啤/明码标价`。
2. **LLM gate 仍会放行「只有梗百科标题、没有独立使用」的条目**：独立重跑 gate 后 Top 仍有 `吴家村`（place name，仅 1 个梗视频 + 2 条 usage）、`明码标价`（普通短语）、`扎啤`（普通名词）。当前 prompt 对「梗百科视频存在」权重过高，应要求**至少 2 条 comment/danmaku 原文 + 2 个视频**，且拒绝纯 explainer-only。
3. **`evidence_rate_2x2` 不在单条 `signals` 里**：报告/README 说每项有该指标，但 `/tmp/mi_check.json` 的 `memes[].signals` 没有；只有 `eval` 输出里有。组装前要么补字段，要么改文档。
4. **银标循环性**：71 条标签来自同一搜索通道（梗百科/指南/盘点标题），报告已承认。独立重跑 0.70 说明它不能当 ground truth；需要用户盲标 50–100 条。
5. **meta-only 偏多**：报告自承 Top10 的 2×2 证据率 0.50、Top20 0.25；assembly 若直接发布会把 explainer-only 当强证据。

## 3. 判定

- **代码/架构：可以继续用**，作为独立候选生成 + 打分库。
- **质量：未达到可发布**；不能替换 `app` 的现榜，也不能直接接 UI。
- **下一步不是组装，而是「精度加固 + 盲标验证」**；通过后再做 shadow 组装。

## 4. 返工清单（给工程师会话）

| # | 任务 | 验收 |
|---|---|---|
| 1 | `blocked_reason` 接入 `is_generic`；补 proper noun/普通名词过滤 | `音乐/扎啤/明码标价/吴家村` 在规则版被过滤或有明确 reject reason |
| 2 | LLM gate 要求独立使用证据（≥2 comments/≥2 videos，且非纯 meta）；explainer-only 只能 observing | gate 后 Top10 不出现 place/普通名词；`吴家村/明码标价` 被 reject |
| 3 | 单条 signals 输出 `evidence_rate_2x2`；文档同步 | `/tmp/*.json` 每项可读该字段 |
| 4 | 同一 `--now` 连跑 2 次，报告 Top20 diff；固定 tie-break/排序 | diff 可解释，不出现随机翻转 |
| 5 | 用户盲标 50–100 条（不由梗百科标题生成），重跑 eval | 盲标 P@10 ≥ 0.8，且 evergreen/news 误入 ≤1/10 |
| 6 | 单独报告 `music/place/proper noun` 的过滤前后数量 | 有前后对比 |

## 5. 组装顺序（返工通过后）

1. `app` 新增 adapter：`HotMemeReport.memes` → 新表/现有 `meme_signal`；只读展示，不改旧榜。
2. shadow：每轮跑模块 + 旧 pipeline，双写；API `?source=meme_info` 供内部对比。
3. 发布门：只发 `status=hot/rising` 且 2×2 证据 ≥ 阈值；meta-only 进「观察」。
4. 用户确认盲标 P@10 达标后，再切 `/api/top?list=new` 的数据源。
5. 最后才拆采集/规则/LLM 服务（架构图上的 seams）。

## 6. 证据文件

- `/tmp/mi_check.json`（规则版）
- `/tmp/mi_check_gate.json`（gate 版）
- `meme_info/REPORT.md`（工程师自述）
- `meme_info/examples/report_20261114_snapshot.json`（19:35 快照）
- 测试：`tests/test_meme_info/` 49 个
