# Phase B 交付报告：B 线候选视频与选片

> 日期：2026-11-21  
> 状态：**完成**（未接 worker、未切流量）

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `meme_info/video_route.py` | 候选发现、在线/新增指标、robust z、45/10 选片、写 round/video_metric |
| `meme_info/bili.py` | 新增 `online_total`、`newlist`、`related`、`search_recent`、`view`、在线数解析 |
| `meme_info/models.py` | `VideoCandidate` / `VideoMetric` / `VideoRouteResult` |
| `tools/video_route.py` | CLI：`--limit` / `--no-db` / `--offline` / `--json` |
| `tests/test_video_route.py` | 22 单元 + 1 Postgres 集成（事务回滚） |

## 2. 实际参数

- 候选 300 = 常规 240（分区榜/热门/搜索）+ 探索 40（newlist/发布时间搜索）+ 相关推荐预留 20
- 评论 45 = 35 热度 + 10 探索；单分区 ≤40%
- 弹幕 10 = 8 热度 + 2 探索；可重叠
- 指标：online、new_comments、new_danmaku、play_proxy；robust z；权重 0.40/0.30/0.20/0.10
- 首见评论/弹幕用年龄归一化代理；播放量按 §5.3 首见/二次见规则
- 写 `round` + `video_metric`，不写 pool1/pool2/audit

## 3. 验证

- 本地：**126 passed, 16 skipped**（skip 为无本地 Postgres 集成测试）
- 云上真实 Postgres：23 passed（事务回滚，0 残留）
- Live smoke：`--limit 5 --no-db` 真实 B站 4.2s，在线覆盖 1.0，错误 0
- 写库验证：单轮 round/video_metric 写入后清理，数据库恢复 0 行
- 现有服务未改；未接 worker

## 4. 已知限制

- 评论/弹幕采样未做（Phase C）；`comments_sampled=danmaku_sampled=0`
- 完整一轮约 455 请求 @2rps ≈ 4 分钟；Phase C 叠加后需观察 15 分钟预算
- “上轮未跑完跳过”由 Phase G 单写者/调度保证
- 相关推荐失败时候选约 280；不影响 ok/degraded
