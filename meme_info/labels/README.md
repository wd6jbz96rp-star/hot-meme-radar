# 标签与盲标

## 1. 已有银标 `current_20261114.json`

- 71→72 条，来源：B站「梗百科/梗指南/梗学堂/是什么梗」标题、盘点视频 + 人工核对；
- **不是 ground truth**，且与查询共用搜索通道，precision 会偏高；
- `heat` 是 1–5 的粗标（按可见播放/活跃度），用于排序校准的弱信号；
- 2026-11 PM 复核后把 `吴家村`（地名）、`明码标价`（普通短语）、`扎啤`（普通名词）改为负例。

## 2. 已有用户盲标 `blind_20261114_user.json`（16 条）

- 2026-11-14 用户对 22:25 gate 版 Top16 的盲标；
- ✅ 真梗 11 条：吃了不烧心、司凤音乐、**扎啤**、老实赚钱很枯燥吧、灭建国、中国人能飞、**吴家村**、MJ宇宙、PiuPiuPiu、狗尾草表情包、没希望，佩不上；
- ❌ 非梗 4 条：**最怕BLG夺冠之人、非凡之爱、腋毛攻击、明码标价**；
- ❓ 不确定 1 条：雪豹眉眉（保留 observing）；
- **用户盲标优先于工程师/PM 的银标推测**；`知识库` 不得误杀 扎啤/吴家村（2026-11-14 `ENGINEER-CORRECTION.md`）。

注意：16 条里的 11 条真梗决定了 **P@16 的上限 = 11/16 = 0.6875**；`
P@16 ≥ 0.8` 在当前标签集上数学不可达，需要扩到 50–100 条盲标后重算。

## 3. 盲标模板 `blind_template_20261114.csv`（60 条）

> 2026-11-14 已由 AI 按网页检索补标（`label/current/heat/confidence/notes` 已填），逐条依据与来源见 [`docs/pm/BLIND-LABEL-REVIEW.md`](../../docs/pm/BLIND-LABEL-REVIEW.md)。该稿是 **AI 初标 + 用户 16 条锚点**，不是用户盲标；P@10 只能作 sanity，不能当独立人工验收。

PM 返工要求「用户盲标 50–100 条，不由梗百科标题生成」。模板由：

```bash
python -m tools.meme_info blind \
  --pred /tmp/mi_fix_gate.json \
  --out meme_info/labels/blind_template_20261114.csv \
  --n 60 --top 8 --seed 20261114
```

生成，包含：

- 当前 gate 后 Top8；
- 从候选池随机抽的 52 条（固定 seed，n=60）；
- 顺序已打乱；**隐藏分数、状态、元话语标题、模型理由**；
- `evidence` 只放评论/弹幕原文（最多 3 条），用于独立判断。

填写列：

| 列 | 值 |
|---|---|
| `label` | `new_meme` / `recent_meme` / `rising_old` / `evergreen` / `classic_meme` / `news_event` / `proper_noun` / `marketing` / `non_meme` / `unclear` |
| `current` | `true` / `false`（最近约 90 天是否仍活跃） |
| `heat` | 0–5（0 不是梗；5 当前最热） |
| `confidence` | A/B/C（A=有直接证据，C=不确定） |
| `notes` | 任何证据瑕疵、出处、是否同名/专名 |

不要参考 `meme_info/REPORT.md` 的 Top 表再填，否则不是盲标；先填完再对照。

## 4. 用 CSV 评测

```bash
python -m tools.meme_info eval \
  --pred /tmp/mi_fix_gate.json \
  --labels meme_info/labels/blind_template_20261114.csv \
  --k 10 --markdown
```

- 空 `label` 按 `unclear` 计；
- `current` 留空时按 label 推断（new/recent/rising/current/meme 为 true）；
- P@k 分母固定为 k：预测不足 k 条时缺位按不相关计。

验收线（PM）：盲标 P@10 ≥ 0.8，且 evergreen/news 误入 ≤ 1/10。
