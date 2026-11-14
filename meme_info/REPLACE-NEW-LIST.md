# `list=new` 噪声处置：明确由模块替代

> 2026-11-15 工程师会话。本文只做**决定与契约**，不改 `app/`（app 由 PM 组装）。
> 证据：`docs/pm/SHADOW-EVAL.md` §1.2 + 2026-11-15 00:40 gate 修复轮。

## 1. 现象（不是偶发）

同轮 app 临时库 `/tmp/shadow-app.db`：

```
new 98 条 / classic 7 条
list=new Top10（按 score DESC）：
ai 87.5 / 看完 86.3 / b站 85.8 / ge 85.5 / 这句 85.5 /
on 85.5 / 11 85.2 / 20 85.1 / 到了 85.1 / in 84.8
```

这些是 `rank=0` 的 `comment_backed` 观察项：绕过 `MEME_MIN` 进 observing，
不占主榜名次，但 `store.list_items(list=new)` 只按 `score DESC` 排，不过滤 rank=0，
于是把真正有 rank 的新梗（咕咕嘎嘎 63.6 / 嘉豪 44.6 / 那咋了 16.9）挤出 new Top10。

**结论：旧 `list=new` 不能作为切榜基线，也不能用来证明 app 旧 pipeline 的 new 能力。**

## 2. 决定：由模块 `publishable` 替代

- 新榜数据源：`GET /api/meme-info?only=publishable&limit=N`（app 现有只读 shadow adapter）。
- 只包含：`status ∈ (hot, rising)` **且** `signals.evidence_rate_2x2=true` **且** LLM gate 通过（`--verify-gate` 时未审项直接不进 `memes`）。
- 不包含：`rank=0` 概念、种子先验直通、旧 app 的 `new/classic` 打标。
- 旧 `list=new`：PM 在产品入口隐藏/标「不可用」，或只作 shadow 对照；**不要**在未修前拿它当基线。

### 2.1 字段映射

| 旧 `list=new` 字段 | 模块 `publishable` 字段 | 说明 |
|---|---|---|
| `canonical` | `canonical` / `family_key` | 展示词 / 稳定 key |
| `score` | `score`（0–100） | 模块热度分，口径见 README §4 |
| `rank` | `rank`（报告内序号，不等于主榜 rank） | 仅展示顺序 |
| `status` | `status`（hot/rising） | publishable 只留这两个 |
| 无 | `evidence_rate_2x2` | 2 评论/2 视频独立使用徽章 |
| 无 | `verified` | LLM `is_meme/is_current/heat/confidence/reason` |
| `components`/`reasons` | `evidence` + `status` + adapter 摘要 | 可解释 |

### 2.2 切榜门槛（未满足前不要切）

1. 同参数 shadow 连跑 ≥3 轮，Top10 无用户 ❌，publishable 数量稳定（当前单轮 7/10）；
2. 用户复核新的 Top10 未标项（蓝色大肥鱼 / doge / 心生爱慕）；
3. 产品接受「只发 publishable（约 7–8 条/轮）」的内容换血；
4. 旧 `list=new` 已从 UI/API 下线或明确标 shadow，不再作为对照基线。

## 3. 如果 PM 要保留旧 pipeline 的 `list=new`

那需要改 `app/store.py::list_items`：`list_type == "new"` 时排除 `rank=0`（或只取 `rank>0` + 发布门），并改 `tests/test_p1_novelty.py` 里「rank=0 观察项应出现在 new」的断言。
**本工程师会话不改 app/（边界）；本决定选择模块替代，不采用这条路径。**

## 4. 本次 gate 修复后的 shadow 证据

- 2026-11-15 00:40 轮（`/tmp/gate_fix.json`）：40 条送审、18 条 rejected、`unverified_omitted=9`；
- `最怕BLG夺冠之人`、`腋毛攻击`、`非凡之爱` → `llm_not_meme`；`明码标价` → 规则 `common_phrase`；
- Top10：7 用户 ✅ / 0 用户 ❌ / 3 未标；`publishable=7/10`；
- 这说明 gate 漏网两条已修，模块 publishable 可作为 new 来源；但单轮仍不足以切榜，继续 shadow。
