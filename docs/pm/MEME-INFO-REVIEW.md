# meme_info 工程师交付 · PM 复核（v2）

- 复核对象：commit `7a585af feat(meme-info): 独立热梗查询与热度打分模块`
- 复核时间：2026-11-14 22:25–22:45
- 方法：读代码/报告 + 跑测试 + 独立真机重跑 + **用户 16 条盲标**
- 结论：**条件性通过。Top8 全对、P@10=0.80；但 P@16=0.688，仍有 4 条明显假阳性，暂不自动发布，先修 gate。**

> 修订说明：初版复核只看了我自己的银标重跑（P@10=0.70），对 `扎啤/吴家村` 判断过重。用户盲标证明这两条是真梗；本版以用户盲标为准。

## 1. 通过项

| 项 | 结果 |
|---|---|
| 独立模块 | 不 import `app/`，不写 SQLite，不起 FastAPI；边界干净 |
| 测试 | `tests/test_meme_info` **49 passed**；全仓 **263 passed** |
| 文档/契约 | `Comment/Evidence/Candidate/Meme/HotMemeReport` 可 JSON 化；`schema_version`/`prompt_version` 有 |
| 数据通道 | 元话语 + 使用（评论/弹幕）+ search expansion；方向正确 |
| LLM | 默认 shadow、失败降级、key 文件、prompt 只给证据；设计正确 |
| 用户盲标 | **Top8 全部 ✅**；P@5=1.0、P@10=0.80；比旧 app 的「种子表 ∩ 热度」明显强 |

## 2. 用户 16 条盲标（2026-11-14，gate 版 Top16）

标签文件：`meme_info/labels/blind_20261114_user.json`

| 排名 | 梗 | 用户标签 | 判定 |
|---:|---|---|---|
| 1 | 吃了不烧心 | ✅ | 真 |
| 2 | 司凤音乐 | ✅ | 真 |
| 3 | 扎啤 | ✅ | 真 |
| 4 | 老实赚钱很枯燥吧 | ✅ | 真 |
| 5 | 灭建国 | ✅ | 真 |
| 6 | 中国人能飞 | ✅ | 真 |
| 7 | 吴家村 | ✅ | 真 |
| 8 | MJ宇宙 | ✅ | 真 |
| 9 | 最怕BLG夺冠之人 | ❌ | **假阳性** |
| 10 | 非凡之爱 | ❌ | **假阳性** |
| 11 | PiuPiuPiu | ✅ | 真 |
| 12 | 狗尾草表情包 | ✅ | 真 |
| 13 | 雪豹眉眉 | ❓ | 不确定/观察 |
| 14 | 腋毛攻击 | ❌ | **假阳性** |
| 15 | 没希望，佩不上 | ✅ | 真 |
| 16 | 明码标价 | ❌ | **假阳性** |

指标：P@5 **1.00**、P@10 **0.80**、P@16 **0.688**（11 真 / 4 假 / 1 不确定）。

## 3. 仍未解决的问题

### 3.1 4 条假阳性集中在 9/10/14/16

- `最怕BLG夺冠之人`：电竞赛事的一次性标题/口号，不是可复制符号。
- `非凡之爱`：视频标题（AI 灰太狼）而非梗；有 meta 标题、无独立使用。
- `腋毛攻击`：一次性说法，证据不足。
- `明码标价`：普通成语/短语，被 meta 标题带进来。

说明 LLM gate 对「梗百科标题 + 少量 usage」仍偏宽；`--verify-gate` 后 40 审 14 剔，但这 4 条没被剔。

### 3.2 代码缺陷

1. **`is_generic` 未接入 `blocked_reason`**：`meme_info/knowledge.py:149` 有 lexicon 判断，`:166-179` 只查 `common_words`；`音乐` 在 `data/lexicon.txt` 里，规则版仍排到 #4（gate 会剔，但规则版应自身过滤）。
2. **单条 `signals` 缺 `evidence_rate_2x2`**：报告/README 说每项有，实际 `/tmp/mi_check.json` 的 `memes[].signals` 没有；只有 `eval` 汇总里有。
3. **银标循环性**：71 条银标来自同一搜索通道，不能当 ground truth；用户 16 条盲标更可信，但样本还小。
4. **稳定性未验证**：同一 `--now` 连跑两次的 Top diff 还没做。

## 4. 判定

- **架构/工程：通过。**
- **质量：条件性通过。** Top8 全对、P@10=0.80 达到「可做 shadow」的门槛；但 P@16=0.688、4 条假阳性，**不能自动发布**。
- **下一步不是直接替换 app，而是：先修 4 条假阳性 + 补盲标回归，再做 shadow adapter。**

## 5. 返工清单（给工程师会话）

| # | 任务 | 验收 |
|---|---|---|
| 1 | `blocked_reason` 接入 `is_generic`；补 proper noun/普通名词/成语过滤 | 规则版不出现 `音乐`、`明码标价`；不误杀 `扎啤/吴家村` |
| 2 | LLM gate 要求「≥2 条 comment/danmaku + ≥2 视频」且拒绝纯 meta-only；对 4 条假阳性加回归 | `最怕BLG夺冠之人/非凡之爱/腋毛攻击/明码标价` 被 reject；Top8 仍保留 |
| 3 | 把 `meme_info/labels/blind_20261114_user.json` 纳入 eval；同一 `--now` 连跑 2 次 | 盲标 P@10 ≥0.9、P@16 ≥0.8；Top20 diff 可解释 |
| 4 | 单条 `signals` 输出 `evidence_rate_2x2`；文档同步 | `/tmp/*.json` 每项可读该字段 |
| 5 | 更新 REPORT：区分「银标 1.0」与「用户盲标 0.8」，不混用 | 报告口径一致 |

## 6. 组装顺序（返工通过后）

1. app 加 adapter：`HotMemeReport.memes` 只读落新表，旧榜不动；
2. shadow：每轮模块 + 旧 pipeline 双跑，API 加 `?source=meme_info` 供内部对比；
3. 发布门：只发 `hot/rising` 且 2×2 证据达标的条目，meta-only 进观察；
4. 盲标 P@10 ≥0.9 后再切 `list=new` 数据源；
5. 最后才拆采集/规则/LLM 服务（架构图 seams）。

## 7. 证据

- `/tmp/mi_check.json`（规则版）
- `/tmp/mi_check_gate.json`（gate 版）
- `meme_info/labels/blind_20261114_user.json`（16 条用户盲标）
- `meme_info/REPORT.md`、`meme_info/examples/report_20261114_snapshot.json`
- `tests/test_meme_info/` 49 个
