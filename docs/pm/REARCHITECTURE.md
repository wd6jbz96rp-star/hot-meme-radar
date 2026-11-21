# 热梗雷达 · 大改版设计文档

> 状态：**设计定稿（待实现）；后续变更仍追加到本文档**  
> 更新：2026-11-21  
> 用途：记录已确认的流程、数据模型、算法、改动计划；后续讨论继续追加。  
> 标记说明：✅ 已确认 ｜ 🟡 建议/待深化 ｜ 🔴 尚未讨论

---

## 0. 文档使用约定

- 这是一份**持续更新**的设计文档，不要求一次写完。
- 新决策先追加到第 11 节「决策日志」，再同步到对应章节。
- 未确认的数字/阈值统一标 🟡，不能当成最终实现参数。
- 改代码前先确认对应章节状态；标 🔴 的流程不实现。

---

## 1. 当前基线（改造前）

### 1.1 线上

- 公网：`http://124.220.189.74/hotmeme/`
- 云服务器：腾讯云 Ubuntu 24.04，`124.220.189.74`
- 架构：浏览器 → Nginx `:80 /hotmeme/` → FastAPI `127.0.0.1:8090` → JSON 报告
- worker：`systemd hotmeme-worker`，`MEME_INFO_VERIFY=0`（LLM 审核暂停）
- 代码：`/home/ubuntu/hotmeme`
- 报告：`/home/ubuntu/hotmeme/var/meme_info_latest.json`
- 同机 80 根路径 Next.js、8080 Caddy、8000 FastAPI 均未改动

### 1.2 代码

- app：只读 FastAPI，`/api/meme-info`、`/api/health`、`/api/refresh` 410
- 无 SQLite / 无 Postgres / 无旧 pipeline
- `meme_info`：独立模块，单轮 `collect → discover → heat → report`
- 当前无跨轮池状态；报告直接覆盖
- LLM：`meme_info/verify.py` 存在但未启用

### 1.3 已知问题

- 评论接口偶发 `http:None` 大量失败，报告仍成功写，可能覆盖上一份好报告，导致新梗榜 0–1 条
- 弹幕随机抽 200 条，未按时间窗口过滤
- 评论 30 天窗口，不能直接用于 15 分钟比例
- worker 实际周期约 800s（跑完再 sleep 600s），窗口不稳定

---

## 2. 目标总流程

```text
每 15 分钟一轮：
  1. 候选视频发现（A/B 双线）
  2. 查在线人数 / 新增指标 → 视频热度排序
  3. 选 45 个评论目标 + 10 个弹幕目标
  4. 拉评论/弹幕 → 候选抽取 → 梗族归并
  5. 15 分钟计数 → 比例加减分 → 池1/池2 迁移
  6. 池2 热度打分/排名 → 生成报告
  7. 可选：LLM 审核 / 人工审核
```

---

## 3. 已确认的核心决策

### 3.1 时间与样本

- ✅ 每轮 **15 分钟**，固定墙钟窗口，不采用“跑完再 sleep”。
- ✅ 每轮**总采样量固定**；各分区/各视频配额可以不同。
- ✅ 比例计数只统计 **15 分钟窗口内**的新评论/新弹幕。
- ✅ 30 天窗口数据只用于可信度/审核证据，不参与比例。
- ✅ 计数单位：去重后的评论、弹幕、独立视频；不是原始字符串出现次数。
- ✅ 采样不足（如评论接口失败）→ `degraded=true`，冻结比例加减分与撤池。

### 3.2 B 路线

- ✅ 首轮：从 8 个分区按权重选近期热门视频。
- ✅ 后续轮：**每轮重新发现“当前最热视频”**，不是跟踪旧视频增量。
- ✅ 当前热门视频由以下因素决定：
  - 当前观看人数
  - 新增评论
  - 新增弹幕
  - 新增播放量
- ✅ 允许从分区榜、热门、搜索、`newlist`、相关推荐等来源发现候选。
- ✅ 候选视频池 **300** 个：240 个常规来源 + 60 个探索来源。
- ✅ 从候选池选 **45 个评论目标 + 10 个弹幕目标**；两者可重叠。
- ✅ 在线人数使用 B站公开接口：
  `GET https://api.bilibili.com/x/player/online/total?bvid=...&cid=...`
- ✅ B站公开接口**没有全局按在线人数排序的视频列表**；只能“先有候选，再逐个查在线人数”。
- ✅ 新增播放量代理已确认：首见 `view/(age_minutes+60)`，二次见 `(view_now-view_prev)/dt_minutes`；dt<5min 退回首见公式；log1p + robust z；权重 10%。
- ✅ 候选池 300、45/10 配额、探索 20%、单分区上限 40% 已确认。

### 3.3 评论/弹幕采样

- ✅ 评论目标 45 个视频；弹幕目标 10 个视频。
- ✅ 比例计数评论：`mode=2` 时间序，翻到本轮窗口起点，只保留窗口内。
- ✅ 30 天证据评论：`mode=3` 热评 + `mode=2` 时间序，30 天窗口，每视频最多 40 条。
- ✅ 弹幕：解析发送时间，只保留 15 分钟窗口内；按 `dmid` 去重。
- ✅ 弹幕当前随机抽 200 条的做法不再用于比例计数。
- ✅ 固定目标条数：新评论 1000、新弹幕 2000；低于 60%（600/1200）标 degraded。
- 🟡 评论/弹幕的 `ctime` 窗口边界、时区、跨轮重叠处理需在实现前定稿。

### 3.4 候选发现与梗族归并

- ✅ 候选不是“评论条数”，而是抽出的 surface，再归并为 `family`。
- ✅ 归并后池子的单位是 **family**，不是单条评论。
- ✅ 每轮必须为本轮未出现的活跃 family 补 0 计数行，才能触发缺席惩罚。
- ✅ 归并优先级：种子/人工别名 > 精确匹配 > 包含/相似度 > 分开待人工。
- 🟡 归并阈值（Jaccard/长度差/编辑距离）沿用当前模块默认值，后续调参。
- ✅ 元话语-only 允许进池1，但 `status=provisional`、`meta_only=true`。
- ✅ provisional 条件：专门梗视频 / ≥2 独立元话语 / 种子或人工确认；弱盘点提及不建。
- ✅ provisional 初始分 30–35；前 2 轮零计数不重罚；2 轮后按正常零惩罚淘汰；出现使用转 normal。
- ✅ 池2例外：强元话语 + 种子/人工确认可无使用进池2，标记 `explainer-only`；每轮最多 2 条；要求 30 天内强元话语；连续 3 轮无新元话语/无使用降池；LLM 不能单独放行。

### 3.5 可信度打分与池1

- ✅ `cred_base` 五特征：meta 0.30 / usage 0.30 / spread 0.15 / fresh 0.10 / prior 0.15。
- ✅ 初始分：有使用 `clamp(cred_base, 0, 80)`；元话语-only 30–35 provisional；种子/人工确认 + 强元话语 `max(40, cred_base)`。
- ✅ 每轮更新：`score = clamp(0.85*prev + 0.15*cred_base + ratio_delta + audit_delta, 0, 100)`。
- ✅ 池1阈值：`P1_in=40`、`P1_out=32`；32–40 滞回保持原状态。

### 3.6 比例加减分

- ✅ 固定采样下，直接使用去重计数比例。
- ✅ 公式方向：

```text
ratio = (count_now + α) / (count_prev + α)
delta = K * log2(ratio)
delta = clamp(delta, -10, +10)
score_t = clamp(score_{t-1} + delta, 0, 100)
```

- ✅ 建议初始参数：`α=5`、`K=5`；单轮 ±10。
- ✅ 本轮为 0：
  - 上一轮 >0、本轮 0 → 零出现惩罚（建议 -5）
  - 上一轮 0、本轮 0 → 持续缺席惩罚（建议 -2/轮）
  - 连续 3 轮为 0 → 额外加速（建议额外 -2）
- ✅ 新信息无上一轮 → 不参与比例，给初始可信度分。
- ✅ 采集失败 → 冻结，不当作真 0。
- 🟡 上述惩罚数值待参数标定。

### 3.7 数据库与表

- ✅ 使用 **Postgres**，不用 SQLite。
- ✅ 2 张池子表：`pool1`、`pool2`。
- ✅ 支撑表：`round`、`video_metric`、`audit_event`。
- ✅ 再加：`family_round_stat`、`pool_transition`、`family_alias`。
- ✅ `video_metric` 保留 **7 天**。
- ✅ 其他表保留期：`round` 90 天、`family_round_stat` 30 天、`audit_event` 90 天、`pool_transition` 90 天。
- ✅ app 仍只读 JSON 报告，不直接读 Postgres。

### 3.7.1 部署位置与连接

- ✅ Postgres 安装在同一台云服务器，不开公网 5432。
- ✅ 监听 `127.0.0.1:5432`；worker/admin 本机连接。
- ✅ 数据库 `hotmeme`；角色 `hotmeme_worker`、`hotmeme_admin`、`hotmeme_migrator`，不用超级用户。
- ✅ 连接串放 `/etc/hotmeme/env`（600），systemd 用 `EnvironmentFile`。
- ✅ 每日 `pg_dump -Fc`，保留 7 天；切流前额外备份。
- ✅ Admin 页面第一版走 SSH 隧道 `ssh -L 8091:127.0.0.1:8091`，不直接暴露公网；有域名/HTTPS 后再加 Nginx Basic Auth + IP 白名单。

### 3.8 审核

- ✅ 人工审核 **可选做**，但第一版做 **Admin 审核页面**。
- ✅ LLM 审核当前暂停（`MEME_INFO_VERIFY=0`）。
- ✅ LLM 加减分：pass **+8**、fail **-15**、unclear **0**。
- ✅ 人工加减分：pass **+15**、fail **-25**、merge 合并后重算。
- ✅ 审核分采用“**最新决定性结论替换**”，不累加历史；人工覆盖 LLM。
- ✅ 同一 `evidence_hash` 不重复审核；证据变化后才允许重新审核。
- ✅ Admin 页面独立于公开只读 app，需登录/白名单，写 Postgres。

---

## 4. 数据模型（Postgres）

### 4.1 `round`

```sql
round(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  window_start TIMESTAMPTZ NOT NULL,
  window_end TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,              -- running / ok / degraded / failed
  videos_sampled INT DEFAULT 0,
  comments_sampled INT DEFAULT 0,
  danmaku_sampled INT DEFAULT 0,
  degraded_reason TEXT,
  stats JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now()
)
```

### 4.2 `video_metric`

```sql
video_metric(
  round_id BIGINT REFERENCES round(id),
  bvid TEXT NOT NULL,
  cid BIGINT,
  title TEXT,
  zone TEXT,
  pubdate BIGINT,
  source TEXT,
  view BIGINT,
  reply BIGINT,
  danmaku BIGINT,
  online INT,
  new_view BIGINT,
  new_reply BIGINT,
  new_danmaku BIGINT,
  play_proxy REAL,
  hot_score REAL,
  selected_comment BOOLEAN DEFAULT false,
  selected_danmaku BOOLEAN DEFAULT false,
  error TEXT,
  PRIMARY KEY(round_id, bvid)
);

CREATE INDEX ON video_metric (bvid, round_id DESC);
CREATE INDEX ON video_metric (round_id, hot_score DESC);
```

### 4.3 `pool1`（候选/可信池）

```sql
pool1(
  family_key TEXT PRIMARY KEY,
  canonical TEXT NOT NULL,
  aliases_json JSONB DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',   -- active / out
  score REAL NOT NULL DEFAULT 0,
  count_prev INT DEFAULT 0,
  count_now INT DEFAULT 0,
  zero_streak INT DEFAULT 0,
  audit_state TEXT DEFAULT 'none',
  audit_hash TEXT,
  first_seen_round BIGINT,
  last_seen_round BIGINT,
  evidence_json JSONB DEFAULT '[]',
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON pool1 (status, score DESC);
CREATE INDEX ON pool1 (last_seen_round);
```

### 4.4 `pool2`（发布/热度池）

```sql
pool2(
  family_key TEXT PRIMARY KEY REFERENCES pool1(family_key) ON DELETE CASCADE,
  round_id BIGINT REFERENCES round(id),
  heat_score REAL,
  rank INT,
  promoted_at TIMESTAMPTZ DEFAULT now(),
  reason TEXT
);
```

约束：`pool2 ⊆ pool1`；pool1 撤出时同步删除 pool2。

### 4.5 `family_round_stat`

```sql
family_round_stat(
  id BIGSERIAL PRIMARY KEY,
  round_id BIGINT REFERENCES round(id),
  family_key TEXT REFERENCES pool1(family_key),
  comment_count INT DEFAULT 0,
  danmaku_count INT DEFAULT 0,
  video_count INT DEFAULT 0,
  meta_count INT DEFAULT 0,
  count_total INT DEFAULT 0,
  ratio REAL,
  score_before REAL,
  score_after REAL,
  delta REAL,
  reason TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(round_id, family_key)
);
```

### 4.6 `audit_event`

```sql
audit_event(
  id BIGSERIAL PRIMARY KEY,
  family_key TEXT REFERENCES pool1(family_key),
  round_id BIGINT REFERENCES round(id),
  kind TEXT NOT NULL,                -- llm / human
  verdict TEXT NOT NULL,             -- pass / fail / unclear / merge
  delta REAL DEFAULT 0,
  confidence REAL,
  reason TEXT,
  operator TEXT,
  model TEXT,
  prompt_version TEXT,
  evidence_hash TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(family_key, evidence_hash, kind)
);
```

### 4.7 `family_alias`（建议）

```sql
family_alias(
  id BIGSERIAL PRIMARY KEY,
  family_key TEXT REFERENCES pool1(family_key) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  norm_alias TEXT NOT NULL,
  source TEXT,                       -- seed / manual / auto
  confidence REAL,
  UNIQUE(norm_alias)
);
```

### 4.8 `pool_transition`（建议）

```sql
pool_transition(
  id BIGSERIAL PRIMARY KEY,
  family_key TEXT REFERENCES pool1(family_key),
  round_id BIGINT REFERENCES round(id),
  from_pool TEXT,
  to_pool TEXT,
  reason TEXT,
  score REAL,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## 5. 每轮实现流程

### 5.1 时间窗口

```text
window_start = 上一轮 window_end
window_end   = 本轮开始时间
固定 15 分钟
```

- 上轮未跑完：跳过本轮。
- 采集失败：`degraded=true`，不做降池。

### 5.2 候选视频发现

来源：

- 8 个分区榜
- 全站热门
- 搜索（关键词 + 按发布时间）
- 分区最新视频 `newlist`
- 高在线视频的相关推荐
- 重点 UP 主最新投稿
- 可选：评论区/弹幕中出现的视频链接

首轮：

```text
8 分区按权重选近期热门 → 候选视频池
```

后续轮：

```text
每轮重新发现候选 → 查在线/新增 → 排序 → 选片
```

候选池 300 的组成：

```text
240 常规：分区榜 + 全站热门 + 搜索 + 高在线相关推荐
60 探索：newlist + 按发布时间搜索 + 新 UP 主 + 冷门分区
```

60 个探索候选不是最终评论/弹幕目标，它们只是进入候选池、参与在线指标排序；  
如果在线/新增表现好，可以进入 45/10；表现差就淘汰。

### 5.3 视频指标与选片

对候选视频：

```text
online   = /x/player/online/total
new_view = view_t - view_prev（有基线时）
new_reply = reply_t - reply_prev（有基线时）
new_danmaku = danmaku_t - danmaku_prev（有基线时）
play_proxy：
  首见：view / (age_minutes + 60)
  二次见：max(0, view_now - view_prev) / dt_minutes
  dt < 5min：退回首见公式
  统一：log1p + robust z
```

视频热度：

```text
hot_video =
  0.40 * z(online)
+ 0.30 * z(new_comments)
+ 0.20 * z(new_danmaku)
+ 0.10 * z(play_proxy)
```

新增播放量代理细则（已确认）：

```text
age_minutes = max((now - pubdate) / 60, 1)
dt_minutes  = 本次时间 - 上次快照时间

首见：
  play_rate = view / (age_minutes + 60)

二次见：
  dt_minutes >= 5 → play_rate = max(0, view_now - view_prev) / dt_minutes
  dt_minutes < 5  → play_rate = view / (age_minutes + 60)

play_proxy = log1p(play_rate)
```

边界：

- `pubdate` 缺失：`view / 1440` 兜底；
- `view` 缺失/负数：`play_proxy = null`，按中位数处理；
- `view` 下降：按 0 处理；
- 基线保留在 `video_metric` 7 天内；超时重新按首见。

选片：

```text
评论目标：35 个按 hot_video 排名 + 10 个探索/分区配额 = 45
弹幕目标：8 个按 new_danmaku/热度 + 2 个探索 = 10
单分区最多占评论目标的 40%
```

固定样本目标：

```text
新评论 1000 条
新弹幕 2000 条
低于 60%（600/1200）→ degraded，冻结比例与撤池
```

### 5.4 评论/弹幕采样

评论：

```text
15 分钟计数：
  mode=2 时间序，翻到 window_start，只保留窗口内
30 天证据：
  mode=3 热评 + mode=2 时间序，30 天窗口，每视频 ≤40 条
```

弹幕：

```text
解析 ctime + dmid
只保留窗口内
按 dmid 去重
固定目标条数
```

### 5.5 候选抽取与归并

```text
评论文本/弹幕/标题
  → 引号/梗上下文/话题/种子/n-gram
  → 证据校验（quote 必须原文子串）
  → 过滤（通用词/新闻/营销/敏感/弱证据）
  → 归一化 + 梗族归并
  → family 计数
```

计数：

```text
count_total = comment_count + danmaku_count + video_count
meta_count 单独存，不参与比例
```

### 5.5.1 元话语-only 规则

```text
进入池1：
  强元话语 + 满足其一：专门梗视频 / ≥2 独立元话语 / 种子或人工确认
  status = provisional
  meta_only = true
  score = 30–35

宽限：
  前 2 轮 count=0 不重罚（或 -0.5）
  第 3 轮起按正常零计数惩罚
  出现 usage → status=normal，参与比例

池2例外（explainer-only）：
  强元话语 + 种子/人工确认
  可无使用进池2
  必须标记 explainer-only
  每轮最多 2 条
  必须 30 天内强元话语
  连续 3 轮无新元话语且无使用 → 降池
  LLM 不能单独放行；必须种子或人工确认
```

### 5.5.2 可信度打分与池1

```text
cred_base = 100 * clamp(
  0.30 * meta_strength
+ 0.30 * usage_strength
+ 0.15 * spread
+ 0.10 * freshness
+ 0.15 * prior
, 0, 1)
```

特征口径：

```text
meta_strength:
  专门梗视频=1.0 / 盘点解释=0.60 / 搜索关注=0.30 / 无=0

usage_strength:
  0.45*sat(去重评论/40)
+ 0.25*sat(去重弹幕/200)
+ 0.30*sat(独立视频/12)

spread:
  0.50*sat(分区数/3)
+ 0.30*sat(独立视频/10)
+ 0.20*sat(来源通道/2)

freshness = clamp(1 - 最近证据 age_days/30, 0, 1)

prior:
  种子/人工确认=0.9；known=0.75；meme_prob*0.5；搜索+热门双命中 +0.1
```

初始分：

```text
有使用：clamp(cred_base, 0, 80)
元话语-only：30–35 provisional
种子/人工确认 + 强元话语：max(40, cred_base)
```

每轮更新：

```text
score_t = clamp(0.85*score_{t-1} + 0.15*cred_base + ratio_delta + audit_delta, 0, 100)
```

池1：

```text
P1_in=40；P1_out=32；32–40 滞回保持原状态。
```

### 5.6 比例加减分

```text
ratio = (count_now + 5) / (count_prev + 5)
delta = clamp(5 * log2(ratio), -10, +10)
```

本轮为 0：

```text
prev>0, now=0 → -5
prev=0, now=0 → -2/轮
zero_streak≥3 → 额外 -2
```

采集失败 → 冻结，不扣。

### 5.7 池迁移

✅ 已确认阈值：

```text
P1_in=40, P1_out=32
P2_in=65, P2_out=55
```

- `P1_in`：进入池1
- `P1_out`：撤出池1（同时撤出池2）
- `P2_in`：进入池2
- `P2_out`：池2→池1
- 迁移写 `pool_transition`

普通条目进池2必须同时满足：

```text
status = normal
score >= 65
usage_count > 0
```

explainer-only 例外：强元话语 + 种子/人工确认，可无 usage 进池2，标记 explainer-only，每轮≤2。

### 5.8 热度与排名

- 只对 pool2 计算/展示热度排名。
- 复用 `HeatModel`，或后续独立热度模型。
- 新梗榜只读 pool2 Top8。
- 模块 shadow 读 pool1/pool2 全量。

### 5.8.1 2×2 证据的定位

- 2×2 = organic usage 中至少 2 条独立评论/弹幕文档（doc_id 去重）**且**至少 2 个独立视频（bvid 去重），并排除该梗自己的梗百科/梗指南视频下的评论。
- ✅ 2×2 只作为**展示徽标**，不再单独卡池2。
- 池2准入由 `score + usage + audit` 决定；2×2 用于说明“有独立使用强证据”。
- 页面保留 `2×2 证据` 徽标；未达标条目仍可因 score/audit 进入池2（explainer-only 例外）。

### 5.9 审核（可选）

- LLM：`SemanticVerifier`，通过/否决/不确定。
- 人工：Admin 审核页面，可选做。
- 审核结果写 `audit_event`，更新 `pool1.audit_state`。
- 同一 `evidence_hash` 只加一次分。
- ✅ 审核定位：**模式 B：否决 + 加分**，不是硬门槛。
  - `fail` → 否决，撤出池2；
  - `pass` → 加分；
  - `unclear` → 不影响；
  - 未审核不阻挡池2。

### 5.9.1 审核加减分与替换规则

```text
LLM pass  +8
LLM fail  -15
LLM unclear 0

Human pass  +15
Human fail  -25
Human merge 合并后重算
```

替换规则：

```text
pool1.audit_adjustment = 最新一次决定性审核的分数变化
```

- 同一 `evidence_hash` 不重复审核；
- 新审核结果**替换**旧 adjustment，不累加；
- 人工结论覆盖 LLM；
- `audit_event` 保留完整流水；
- 分数计算只使用当前 `audit_adjustment`。

### 5.9.2 Admin 审核页面

- 建议独立服务：`app/admin.py` 或 `admin/`，绑定 `127.0.0.1:8091`。
- 公开 app 仍只读，不获得 Postgres 写权限。
- Nginx 暴露 `/admin/`，Basic Auth + IP 白名单；当前无 HTTPS 时优先走 SSH 隧道。
- 队列优先级：
  1. score 55–70、接近池2；
  2. LLM unclear / fail 但证据强；
  3. explainer-only 待确认；
  4. 用户举报/人工标记；
  5. 长期未复审的池2条目。
- 操作：approve / reject / merge / need_more；reject/merge 必填理由和 operator。
- 写 `audit_event`，更新 `pool1.audit_state`/`audit_adjustment`，触发池迁移。

---

## 6. 当前代码到目标的改动

| 模块 | 当前 | 目标 |
|---|---|---|
| `meme_info/collect.py` | 30 天评论、随机弹幕、固定视频配额 | 双窗口：15 分钟计数 + 30 天证据；弹幕按 ctime/dmid |
| `meme_info/discover.py` | 内存候选/归并 | 复用，输出 family 计数 |
| `meme_info/query.py` | 单轮打分出报告 | 接池状态、比例、迁移 |
| `meme_info/heat.py` | 全量打分 | 保留热度；池2排名 |
| `meme_info/verify.py` | 可选 LLM gate，当前停用 | 审核事件/加减分，可选恢复 |
| `app/meme_info_adapter.py` | 读单报告 | 增加 pool/cred/audit 字段 |
| `app/api.py` | `/api/meme-info` | 保持只读，不写 Postgres |
| 新 admin 服务 | 无 | `app/admin.py` 或 `admin/`，127.0.0.1:8091，审核写库 |
| `app/static/index.html` | 新梗榜/ shadow | 新梗榜只读 pool2；显示池状态 |
| 新模块 | 无 | `meme_info/credibility.py`、`pools.py`、`state.py` |
| DB | 无 | Postgres，worker 单写 |
| 部署 | systemd worker 600s | 15 分钟固定调度；Postgres 连接配置 |

---

## 7. 实施阶段

### Phase A：Postgres 状态层

- 建表/迁移脚本
- `round`、`video_metric`、`pool1`、`pool2`、`family_round_stat`、`audit_event`
- worker 单写事务
- 验收：本地/云上建库、插入模拟轮次、报告仍可生成

### Phase B：B 线候选视频与选片

- 多来源候选发现
- 在线人数 API 接入
- 视频指标/新增计算
- 45/10 选片
- 验收：单轮输出候选视频指标与 selected 标记

### Phase C：15 分钟窗口计数

- 评论 mode2 时间序翻页
- 弹幕 ctime/dmid 过滤
- 固定目标条数 + degraded
- 验收：同一轮可复算 count；失败轮冻结

### Phase D：family 状态与比例

- family 计数写入
- 未出现 family 补 0
- 比例公式/零惩罚
- 验收：连续两轮模拟，分数按公式变化

### Phase E：池迁移与报告

- pool1/pool2 迁移
- heat/rank
- report schema 增加 pool/cred
- app adapter/UI 适配
- 验收：新梗榜只显示 pool2；shadow 全量

### Phase F：审核（可选）

- LLM 审核恢复开关
- Admin 审核页面（独立服务 + 鉴权）
- audit_event 加减分（最新替换，不累加）
- 验收：同一 evidence_hash 不重复加分；人工覆盖 LLM；审核 fail 可撤出池2

### Phase G：部署与切换

- 云服务器本地 Postgres（127.0.0.1:5432），角色/连接串/备份
- systemd timer 15 分钟
- 影子运行对比
- 切流、回滚方案

---

## 8. 验收指标

- B 线：候选视频在线/新增指标覆盖率；45/10 选片稳定性
- 计数：15 分钟窗口 count 可复算；失败率/degraded 比例
- 比例：连续轮分数变化符合公式；无误杀/异常抖动
- 池：P1/P2 进出次数、停留时长、撤出原因分布
- 质量：用户锚点 P@K、无 ❌ 进新梗榜
- 稳定性：评论接口失败时不清空榜单

---

## 9. 风险与保护

- 评论接口失败：degraded 冻结，不覆盖上一份好报告
- 弹幕/评论采样偏差：固定目标 + 去重 + 按时间窗口
- 在线人数接口限流：2 rps，候选预算控制
- 池状态抖动：滞回阈值 + 单轮 ±10 限制
- 新信息冷启动：初始分，不参与第一轮比例
- 人工审核缺失：先只做 LLM/规则，人工接口可选
- Postgres 单点：备份；worker 单写；app 不直连

---

## 10. 待确认 / 待讨论

- 无（7 项设计决策已定稿）；实现过程中的新问题继续追加。

---

## 11. 决策日志

| 日期 | 主题 | 决策 | 状态 |
|---|---|---|---|
| 2026-11-21 | 轮次间隔 | 固定 15 分钟 | ✅ |
| 2026-11-21 | 采样量 | 每轮总采样固定，分区配额可不同 | ✅ |
| 2026-11-21 | 比例窗口 | 比例只用 15 分钟新评论/弹幕 | ✅ |
| 2026-11-21 | 证据窗口 | 30 天数据只用于可信度/审核 | ✅ |
| 2026-11-21 | B 线 | 首轮分区加权；后续每轮重新发现当前热门视频 | ✅ |
| 2026-11-21 | 选片 | 候选池约 300 → 45 评论 + 10 弹幕 | ✅ |
| 2026-11-21 | 在线人数 | 使用 B站 `/x/player/online/total` | ✅ |
| 2026-11-21 | 计数单位 | 去重评论/弹幕/视频 | ✅ |
| 2026-11-21 | 比例公式 | log2 比例 + 平滑 + 单轮封顶 | ✅ 方向 |
| 2026-11-21 | 零计数 | 0/0 也要扣分，不靠连续多轮 | ✅ 方向 |
| 2026-11-21 | 数据库 | Postgres | ✅ |
| 2026-11-21 | 表 | 2 池表 + round/video_metric/audit_event | ✅ |
| 2026-11-21 | 保留期 | video_metric 7 天 | ✅ |
| 2026-11-21 | 人工审核 | 可选做 | ✅ |
| 2026-11-21 | 候选归并 | 池子单位是 family；每轮补 0 行 | ✅ |
| 2026-11-21 | B 线参数 | 候选池 300（240 常规 + 60 探索）；45/10；探索 20%；单分区 ≤40% | ✅ |
| 2026-11-21 | 固定样本 | 新评论 1000 / 新弹幕 2000；低于 60% 标 degraded | ✅ |
| 2026-11-21 | 新增播放量 | 首见年龄归一化 + 二次见基线差值；dt<5min 退回首见；log1p/z；权重 10% | ✅ |
| 2026-11-21 | 元话语-only | 可进池1 provisional；强元话语+种子/人工确认可 explainer-only 进池2；每轮≤2；LLM 不单独放行 | ✅ |
| 2026-11-21 | 可信度打分 | 五特征 cred_base；score 0.85/0.15 平滑；初始上限 80；P1 40/32 滞回 | ✅ |
| 2026-11-21 | 2×2 证据 | 只做展示徽标，不再单独卡池2；池2看 score+usage+audit | ✅ |
| 2026-11-21 | 池2阈值 | P2_in=65/P2_out=55；普通条目需 usage>0；explainer-only 例外 | ✅ |
| 2026-11-21 | 审核模式 | 模式 B：否决+加分，不是硬门槛 | ✅ |
| 2026-11-21 | 审核加减分 | LLM +8/-15/0；人工 +15/-25；最新替换不累加 | ✅ |
| 2026-11-21 | 人工审核入口 | 独立 Admin 页面 + 鉴权/白名单，写 Postgres；公开 app 保持只读 | ✅ |
| 2026-11-21 | Postgres 部署 | 同服务器本地，127.0.0.1:5432；worker/admin 分角色；7 天 pg_dump；Admin 先走 SSH 隧道 | ✅ |

---

## 12. 后续追加区

> 后续讨论每确定一项，在这里追加，并同步到对应章节。

### 待追加

- 2026-11-21：第 1 项 B 线采集参数已确认（候选池 300、45/10、探索 20%、单分区 40%、评论 1000/弹幕 2000、60% degraded）。
- 2026-11-21：第 2 项新增播放量代理已确认（首见年龄归一化 + 二次见基线差值，log1p/z，权重 10%）。
- 2026-11-21：第 3 项元话语-only 规则已确认（provisional 进池1；种子/人工确认可 explainer-only 进池2，每轮≤2）。
- 2026-11-21：第 4 项可信度打分已确认（五特征、初始上限 80、0.85/0.15 平滑、P1 40/32）。
- 2026-11-21：第 5 项已确认——2×2 只做展示徽标；P2 65/55；普通条目 usage>0；审核模式 B（否决+加分）。
- 2026-11-21：第 6 项已确认——LLM +8/-15/0、人工 +15/-25、最新替换不累加；人工审核用独立 Admin 页面。
- 2026-11-21：第 7 项已确认——同服务器本地 Postgres，127.0.0.1；worker/admin 分角色；7 天 pg_dump；Admin 先走 SSH 隧道。
- 设计阶段完成，下一阶段：Phase A（建 Postgres 状态层与迁移脚本）。
