# Phase C 交付报告：15 分钟窗口计数

> 日期：2026-11-21  
> 状态：**完成**（未接 worker、未切流量）

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `meme_info/window.py` | 15 分钟评论/弹幕采样、30 天证据、round 回写 |
| `tools/window_sample.py` | CLI：`--round` / `--no-db` / `--json` / `--targets-json` / `--discover` |
| `tests/test_window.py` | 20 单元 + 2 Postgres 集成（事务回滚） |
| `meme_info/bili.py` | 评论 `include_top`；弹幕解析 `ctime`/`dmid`；offline fixture |
| `meme_info/models.py` | `Danmaku.dmid`、`WindowTarget/WindowComment/WindowDanmaku/WindowSample` |

## 2. 参数（PM 复核后）

| 项 | 值 |
|---|---|
| 计数窗口 | 15 分钟；异常跨度 >20min 时 clamp 到最近 15min，标 `window_clamped` |
| 评论计数 | 目标 1000，低于 600 degraded；mode=2 时间序 |
| 弹幕计数 | 目标 **600**，低于 **300** degraded；dmid 去重 |
| 评论去重 | comment_id 全局；文本 **per_video**（跨视频同文本保留） |
| 30 天证据 | 评论每视频 ≤40；弹幕每视频 ≤200；与计数分字段 |
| 请求预算 | 700，超预算 degraded |

## 3. 验证

- 本地：**146 passed, 18 skipped**
- 云上真实 Postgres：`tests/test_window.py` **22 passed**（事务回滚，0 残留）
- Live smoke：3 个当前热门视频，窗口内评论 259 / 弹幕 110；XML 封顶单视频 3600 条
- round 回写：`comments_sampled/danmaku_sampled/status/degraded_reason/stats.phase_c`；不改 videos_sampled/finished_at
- 未改 app/worker/nginx/数据库业务数据

## 4. 已知限制

- 弹幕 XML 封顶（约 3000–3600 条/视频，span 约 1.2 天），阈值已按实测下调到 600/300。
- 评论 mode=2 单视频最多 10 页；超热视频可能 pages_exhausted。
- 异常窗口 clamp 后仍标 degraded，Phase D 应冻结比例。
- 未接 worker；Phase G 单写者/调度保证。
