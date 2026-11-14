# 捕获与统计

## 1. 为什么从 B站 评论/弹幕做起

热搜标题只能说明「有人在讨论这个事件」，不能说明「这个符号被模仿使用」。梗的核心是**模仿性使用**，而弹幕天然是「同一句话被大量人重复」的场所：

- 弹幕整句跨视频重复 → 梗句扩散的直接证据；
- 评论区出现「这是什么梗 / 什么出处」→ 语境依赖的语义证据；
- 评论/弹幕里同一表达覆盖多个 UP 主、多个分区 → 自发扩散而非官方推广。

实测（2026-11，免登录）：

| 接口 | 用途 | 备注 |
|---|---|---|
| `x/frontend/finger/spi` | 取 buvid3/buvid4 | 免登录，用于通过排行榜风控 |
| `x/web-interface/ranking/v2?rid=` | 分区榜，约 90–100 条/区 | 带 view/danmaku/reply/like/coin/fav/share |
| `x/web-interface/popular` | 全站热门补充 | ps ≤ 50 |
| `x/v2/reply/main?type=1&oid=&mode=3&next=0&ps=20` | 热度评论（mode=3）/ 时间序评论（mode=2） | 免 wbi、免登录；首屏 `next=0`，`ps=20`；响应含 `replies`、可选 `top_replies`、`cursor.next` |
| `comment.bilibili.com/{cid}.xml` | 弹幕 | raw-deflate，`zlib.decompress(data, -15)` |
| `x/web-interface/wbi/search/type` | 搜索「热梗/梗百科/热梗盘点」等视频 | 需 wbi 签名 |
| `x/player/pagelist` | 搜索视频补 cid | 搜索结果不含 cid |
| `x/web-interface/search/square` | 热搜旁证 | 暂未参与打分 |

## 2. 四条通道

| 通道 | 内容 | 作用 |
|---|---|---|
| A 视频池 | 8 个分区榜 + 全站热门 | 候选视频与官方统计 |
| B 语料 | 标题、简介、热评、弹幕 | 新词发现与「被使用」证据 |
| C 检索 | 20 组「热梗/梗百科/年度热梗」视频及其标题、标签 | 提升梗的召回，并提供「梗源覆盖」信号 |
| D 知识 | 种子梗表 148 条 + 别名表 | 先验、归并、区分老梗 |

分区权重按造梗密度设定：鬼畜 1.4、生活 1.2、动画 1.1、游戏 1.1、影视 1.0、音乐 0.9、娱乐 0.9、知识 0.7。

### 2.1 语料配额与窗口（v2 P0 评论区优先）

评论是第一语料区，弹幕降为「梗句扩散」旁证：

| 项 | 口径 |
|---|---|
| 评论视频 | 30 个（分区榜按 `reply` 取 + 10 个梗搜索视频），`COMMENT_VIDEOS=30` |
| 评论接口 | `/x/v2/reply/main`，每视频 `mode=3`（热度）+ `mode=2`（时间）各 1 页，`ps=20`，`COMMENT_MODES=(3,2)`、`COMMENT_PAGES=1` |
| 每视频保留 | 两种 mode 合并去重后 ≤40 条（`COMMENT_MAX_PER_VIDEO=40`）：热度评论按赞、时间评论按 `ctime` 各取一半，再按赞补齐 |
| 评论窗口 | 只收 `ctime` ≤30 天（`COMMENT_WINDOW_DAYS=30`）；≤7 天单独计数用于增速（`COMMENT_RECENT_DAYS=7`） |
| 评论去重 | 先按 `rpid`，再按归一化文本；同一评论同时出现在两个 mode 只留一条 |
| 评论落库 | `text_sample.raw_time = ctime`（Unix 秒）；`TextDoc` 以动态属性携带 `ctime/mode/rpid`，不改 `extract.TextDoc` 字段 |
| 弹幕视频 | 10 个（`DANMAKU_VIDEOS=10`），每视频随机采样 ≤200 条（`MAX_DANMAKU_PER_VIDEO=200`） |
| 弹幕定位 | 只作整句扩散旁证与「老梗刷屏」识别，不参与评论证据门控 |

旧 `/x/v2/reply` 实测匿名仅返回 3 条 → 一轮 90 条评论；`reply/main` 单页 ~19–20 条，30 视频 ×2 mode ≈ 1200 条。旧 fixture 结构（只有 `data.replies`）仍可解析。

## 3. 候选发现

1. **归一化**：NFKC、去 emoji/零宽、合并重复字符（保留数字梗原形，23333 → 2333）、按标点切段。
2. **模式抽取**：引号内容、`XX 是什么梗/出处` 上下文、`#话题#`、【】标题、弹幕整句（3–20 字）。
3. **新词发现**：2–6 字 n-gram，要求
   - 频次 ≥ 3
   - 凝固度 `min PMI ≥ 3.0`（log2）
   - 左右邻接熵 `min ≥ 1.0`
   - 文档频率 ≥ 2
   - 不在通用词表/口语常用词表中（种子词除外）
4. **合并**：模式候选与 n-gram 候选合并，按 `频次 × log(1+视频数)` 排序取前 1500。

凝固度公式：对词 w 的每种切分 (a,b)，`PMI = log2( p(w) / (p(a)p(b)) )`，取最小值。

邻接熵：对左/右邻字符分布求信息熵，取左右较小值；熵越高说明该词越能独立成词。

## 4. 热度模型

每轮对每个梗族计算：

| 信号 | 定义 |
|---|---|
| 曝光 E | Σ log1p(视频播放) × 分区权重 |
| 互动 G | Σ log1p(弹幕 + 2×评论 + 点赞/20) |
| 广度 B | 视频数、分区数、UP 数的分位均值 |
| 增速 V | (本轮频次+1) / (过去 7 天日均+1) |
| 梗性 P | 判定分 0–1（含「梗源覆盖」：出现在热梗类搜索视频中的条数） |
| 评论证据 | 见 §4.1：窗口计数、年龄归一赞、7 天增速、弹幕刷屏标记 |

### 4.1 评论证据（P0）

按梗族汇总 ≤30 天评论证据，写入 `components.raw`（API 原样透传），同时留 `components.comment`：

| 字段 | 定义 |
|---|---|
| `comment_count_recent` | ≤30 天命中该族的评论文档数 |
| `comment_7d_count` | ≤7 天命中数；`comment_mid_count` = 7–30 天命中数 |
| `comment_growth_7d` | 7 天增速 = `(comment_7d_count+1) / (comment_mid_count+1)`，>1 表示在加速 |
| `comment_like_weighted` | `Σ log1p(like) × 0.5^(age_days / 14)`（`COMMENT_LIKE_HALF_LIFE_DAYS=14`），点赞按年龄归一 |
| `comment_df_video` | 命中该族的评论视频数 |
| `earliest_comment_at` / `age_days` | 窗口内最早评论时间（ISO）/ 距今天数 |
| `danmaku_only` | 弹幕命中 ≥3 且 `comment_df_video<2`、`comment_7d_count<2` → 老梗刷屏旁证 |

**P0 门控**：当本轮 `comments_recent ≥ COMMENT_GATE_MIN_DOCS=200`（评论采集健康）时，非种子候选若 `comment_count_recent == 0` → `rejected`（原因 `no_recent_comment`）。种子仍走原有规则；评论采集不健康时整体跳过门控，避免单源失败清空候选池。

归一化：每个信号在本轮候选池内取百分位，避免量纲与轮次波动。

综合分：

```
base  = 0.30E + 0.22G + 0.18B + 0.18V + 0.12P
score = 100 × base × 0.5^(age_hours / 48) × (0.9 + 0.1×novelty)
S     = 0.6 × score + 0.4 × 上一轮      # 首轮无历史时直接使用 score
```

约束：

- 单视频命中占比 > 0.8 → ×0.5，防单视频刷屏；
- 梗性判定分（0–1）≥ 0.45 才入候选池（`MEME_MIN`），种子命中豁免该阈值；
- 连续两轮 ≥ 55（`CONFIRM_ROUNDS=2`）才稳定上榜；首次出现、曝光分位 ≥ 0.95 可直接上榜并标「新晋」；
- 梗性达标的候选按分数补齐 Top10，尾部条目在面板中显示为「观察」；
- 老梗回锅（>14 天再出现且增速 ≥ 2）重新入池并标「回锅」；
- 至少出现在 2 个视频中（`MIN_VIDEOS_FOR_RANK`）才进稳定榜；
- 「新晋」由 `meme_signal.is_new` 持久化：梗族首次出现在本系统观测历史中即本轮为真，跨轮/重启后仍可读。

## 5. 榜单输出

Top10 以梗族为单位，每条包含：热度分、名次变化、类型、首次发现时间、代表弹幕/评论、分项得分、判定依据。

## 6. 验证

- 固定 fixture 回放：离线跑通，排序稳定。
- 黄金集：30 个已知梗 + 30 个非梗候选，检查判定分与 Top10 精度。
- 真机验收：连续 3 轮，Top10 真梗率 ≥ 60%，无明显同源变体刷屏，单源失败不空榜。

## 7. 风险与合规

| 风险 | 应对 |
|---|---|
| -352 / -412 风控 | spi 取 buvid3、限速 2 rps、缓存、熔断、回退上一轮 |
| 弹幕 raw-deflate | 显式 `zlib(-15)`，失败回退 gzip |
| Python 默认 CA 失败 | cafile 回退链 `/etc/ssl/cert.pem` → certifi |
| 接口变更 | adapter 隔离 + fixture 回归 |
| 冷启动 | 首轮无历史时跳过 EMA，直接用本轮得分；以广度 + 词典 + 梗源先验排序 |
| 合规 | 公开数据、限速 2 rps、10 分钟低频、不存用户身份、敏感词过滤、单源可停用 |

### 7.1 采集缓存与请求指标

- **弹幕缓存**：24h（`CACHE_TTL_SECONDS`），按 cid 命中。
- **评论缓存**：30 分钟（`HOTMEME_COMMENT_CACHE_SECONDS=1800`，`0` 关闭），按 `(aid, mode, page)` 命中（旧库启动时幂等迁移，历史行补 `mode=3`）；缓存内容带 `ctime`。命中时**不产生请求**，但仍按当前 cycle 写入 `text_sample`，`stats.comments` 统计口径不变。
- **cid 持久化**：搜索视频首轮补 `x/player/pagelist` 后回写 `video.cid`；后续轮 `upsert_videos` 用 `COALESCE` 保留，warm 轮 `pagelist_requests=0`。
- `cycle.stats_json` 的请求指标：`requests`（HTTP 总请求数，含 412 后的 curl 兜底）、`endpoint_counts`（ranking/popular/search/pagelist/comments/danmaku/spi/nav，`reply/main` 计入 `comments`）、`comment_requests`（评论页实际拉取次数，不含 curl 兜底）、`comment_cached`（评论缓存命中页数）、`danmaku_cached`、`pagelist_requests`。
- 评论语料指标：`comments`（本轮保留条数）、`comments_recent`（≤30 天）、`comments_7d`（≤7 天）、`comment_modes`（各 mode 保留条数）、`comment_dupes`（rpid/文本去重数）、`comment_filtered_old`（窗口外丢弃数）、`danmaku`/`danmaku_docs`（弹幕文档数）、`comment_evidence_ready`、`comment_gate_checked`（通过梗性门控、进入评论门控的候选数）、`comment_gate_rejected`（被 P0 门控拒绝的候选数）、`meme_gate_rejected`（被梗性门控拒绝的候选数）。

## 8. 扩展点

- 接入微博/抖音/头条热榜（adapter 接口已隔离，只需实现 `fetch`）。
- 接入 LLM 复核 Top 候选（规则先筛，LLM 只处理 20–50 条）。
- 图像/表情包梗识别、BGM 空耳检测。
