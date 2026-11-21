# Phase E 交付报告：报告 v2 与 adapter

> 日期：2026-11-22  
> 状态：**后端完成**（前端 E2 待做；未切线上）

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `meme_info/report.py` | 从 `PoolRoundResult` 或 Postgres 生成报告 v2；heat/rank；原子写 |
| `tools/build_report.py` | CLI：`--round latest/id`、`--out`、`--no-db --pool-json`、`--json` |
| `tests/test_report.py` | 25 单元 + 4 Postgres 集成（事务回滚） |
| `app/meme_info_adapter.py` | v2 支持（pool1/pool2），v1 完全兼容 |
| `tests/test_meme_info_adapter.py` | 追加 v2 契约/池过滤/降级/缓存用例 |
| `meme_info/pools.py` | 小改：evidence_json 持久化 `cred_base`，保证 DB 重建报告 cred_score 非空 |
| `tests/test_pools.py` | +1 evidence_json `cred_base` 覆盖 |

## 2. 报告 v2

- `schema_version = meme-info/v2`
- `memes == pool2`，按 heat/rank 降序；新梗榜数据源
- `pool1` = normal/provisional 全量（shadow）；out 只计数
- 单条新增：`pool`、`cred_score`、`heat_score`、`heat_rank`、`explainer_only`、`meta_only`、`audit_state`、`badge_2x2`、计数/证据
- `publishable` 只由 pool2 决定；2×2 仅徽标
- adapter `only=publishable` → pool2；`only=all` → pool1；v1 兼容

## 3. heat 代理公式（初值）

```text
heat_score = 100 * clamp(
  0.40 * min(1, comment_count/20)
+ 0.30 * min(1, danmaku_count/100)
+ 0.15 * min(1, video_count/5)
+ 0.15 * clamp(score/100, 0, 1), 0, 1)
```

排序：heat desc → score desc → family_key。参数待线上回测。

## 4. 验证

- 本地：**216 passed, 24 skipped**
- 云上真实 Postgres：`test_report.py` 29 passed、`test_pools.py` 33 passed（事务回滚）
- DB 重建报告 `cred_score` 非空（`evidence_json.cred_base`），与 observation 精确值一致
- 8 张业务表 0 残留、schema_version=2；服务未动
- 临时报告 `/tmp/hotmeme_report_v2.json` 经 TestClient `/api/meme-info` 200

## 5. 限制

- heat 参数未回测
- `status` 仍是新鲜度四态；degraded 用独立字段
- pool1 不含 out 历史（只计数）
- degraded 报告仍会覆盖 out；worker 侧策略在 Phase G
- pool1 全量无上限，Phase G 评估报告体积
