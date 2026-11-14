# WP2C 看板实现报告

- 会话：frontend（`1e9c853b-cf26-47c2-bd5a-3e668613a045`）
- 范围：**仅** `app/static/index.html`（单文件、无构建、无 CDN / 外部字体）
- 基线：git `84a4b28` + 并行 Wave 2A 的 `app/*.py`、`tests/*`（只读，未修改）
- 时间：2026-11-14 14:17–14:35（Asia/Shanghai）

## 1. 改动点

保持现有暗色视觉与 CSS 变量，增量修改；HTML 骨架与 `esc()` / `timeAgo()` / `fmtTime()` /
`deltaHtml()` 等既有函数保留，新增能力集中在 script 的数据层。

### 1.1 历史曲线（展开懒加载 + 同 key 缓存）

- 新增 `sparkHtml()`（L195）、`paintHistory()`（L259）、`ensureHistory()`（L272）。
- 卡片首次展开时请求 `GET /api/meme/{encodeURIComponent(family_key)}/history?limit=60`；
  `family_key` 可能含空格/中文/`/`，全部经 `encodeURIComponent`。
- 缓存 `Map<family_key,{cycleId,state,points}>`：同一轮内重复展开不再发请求；
  `cycle.id` 变化后自动失效并按需重取；失败缓存 `err`，展示「暂无历史」。
- 内联 SVG（`viewBox 0 0 340 76`，`width:100%`）：
  - 分数：蓝色实线 + 末点圆点；**分数轴自适应**（最小跨度 2 分，上下留 0.5 边距，钳制 0–100），
    左侧标注上/中/下界数值，避免 0–100 固定轴把 93.0→93.2 画成直线。
  - 名次：紫色虚线，**名次轴反转**（rank 1 在顶部，域为 1..max(10, 实际最大名次)）；
    `rank=0` 的轮次不画点并断开线段。
  - `role="img"` + `aria-label` 描述首末分数、名次与轮数；图例标注「名次（越靠上越前）」。
- 历史为空 / 请求失败 → 「暂无历史」；单点 → 单点；加载中 → 「加载历史…」。
  fetch 失败只影响该卡片，不阻塞列表渲染。

### 1.2 新晋标签

- `isNew()`（L179）优先使用 API 的 `item.is_new`：
  - boolean 直接采用；非空真值（如 `1` / `"true"`）按真值处理；
  - 字段缺失 / `null` 时回退 `first_seen < 36h`；
  - 两者都缺失安全返回 `false`，不抛错。
- 当前真实 API 已含 `is_new`（Wave 2A 已落 `store.top_items`），本轮数据 10/10 为 `false`，
  因此真实页面没有「新晋」角标；回退与优先级已用注入式脚本验证（见 §2）。

### 1.3 状态与空态

- 状态机（`updateStatus()` L342）：
  `首轮采集中`（无 cycle）/ `本轮采集中`（running + 已有 top）/ `数据正常` /
  `降级`（`status==="degraded"` 或 `health.errors` 非空，显示异常源数量，`title` 展示明细）/
  `采集异常`（`error` 或 `status==="failed"`，notice 显示截断原因）/ `服务未连接`（fetch 失败，灰点）。
- 头部常驻显示：状态、**轮次 · 耗时 · 请求数**、**更新时间**（`cycleMeta` / `updatedAt`，L389）；
  footer 保留视频/评论/弹幕/候选等完整统计。
- SSE 断线降级（`subscribe()` L537）：`onerror` → `connMode="poll"`，启动 30s
  `setInterval(refresh,30000)`，notice 提示「实时推送不可用，已切换为 30 秒轮询」；
  `EventSource.onopen` 恢复后停止轮询并清除提示。`EventSource` 不支持或 12s 未连上同样走轮询。
- 空态区分：
  - 无 cycle → 「首轮采集中」+ spinner；
  - 有 cycle 但无上榜 → 「本轮暂无上榜」；
  - `error` / `status==="failed"` → 「采集异常」+ 原因；
  - fetch 失败 → 「服务未连接」+ 重试提示；**保留已有卡片不清空**。
- 渲染去重：`listSignature()` 由 `cycle.id + key + rank + score + is_new` 组成，未变化时跳过整表
  重建，避免 30s 轮询/SSE 把展开状态、键盘焦点重置。

### 1.4 可访问性 / 移动端

- 卡片：`tabindex="0"`、`role="button"`、`aria-expanded`、`aria-controls="detail-{i}"`、
  `aria-label`（第 N 名 / 名称 / 分数）；Enter、Space 切换并 `preventDefault`；
  整卡点击仍可展开；`openKeys` 按 `family_key` 跨重绘保留展开态；重绘后按 key 还原焦点。
- `:focus-visible` 2px 强调色轮廓；状态文本 `aria-live="polite"`；notice `role="status"`。
- `prefers-reduced-motion: reduce`：全局动画/过渡近乎关闭、spinner 静态、panel hover 不位移。
- 小屏：`grid-template-columns: 44px minmax(0,1fr)`、标题/样例 `overflow-wrap:anywhere`、
  score 换行、SVG `width:100%`；375px 实测 `scrollWidth === innerWidth`（无横向溢出）。
- 新增 data-URI SVG favicon（L7），避免浏览器请求 `/favicon.ico` 产生 404 控制台错误。
- `esc()` 继续覆盖所有插值：canonical、type、sample、family_key、reasons、错误文本、aria-label；
  注入 `<x>&"`、`a b/<测试>` 的验证见 §2。

### 1.5 自包含

- 无 CDN、无外链字体、无构建步骤；`src|href` 非 `data:`/`#` 的外链数量为 **0**。
- 未改 API 契约；未修改 `app/*.py`、`tests/`、`data/`、`README.md`、`docs/01-02`。

## 2. 验证命令与结果

环境：macOS，`.venv/bin/python`（项目虚拟环境），Node `v24.22.0`，Chrome `157.0.8103.52`（headless）。
浏览器验证脚本为 `/tmp/wp2c-check.mjs`、`/tmp/wp2c-fresh.mjs`、`/tmp/wp2c-offline.mjs`（一次性，未入库）。

| # | 项 | 命令 / 方法 | 结果 |
|---|---|---|---|
| 1 | JS 语法 | 提取 `<script>` → `node --check /tmp/hotmeme-board-final.js` | **PASS**，exit 0 |
| 2 | 临时服务 | `HOTMEME_PORT=8011 HOTMEME_DISABLE_SCHEDULER=1 .venv/bin/python run.py` | 启动成功，仅监听 8011；未占用/重启 8000 |
| 3 | 首页 | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8011/` | **200** |
| 4 | 榜单 API | `curl -s 'http://127.0.0.1:8011/api/top?limit=10'` | **200**，`top=10`，`cycle=3`，`is_new` 10/10 为 `false` |
| 5 | 真实数据渲染 | CDP 打开首页后取 DOM | **10 张卡片**；状态「数据正常」；头部「第 3 轮 · 42.7s · 请求 86」+「14:15:11 更新」；10 张均 `tabindex=0/role=button`、`aria-expanded=false` |
| 6 | 展开走势 | 依次点击 10 张卡片后取 DOM | `open=10`、`aria-expanded=true` 10、`svg.spark-svg` **10**、「暂无历史」**0** |
| 7 | 历史请求 | CDP `performance.getEntriesByType('resource')` | 10 条全部 **200**，路径正确编码：`/api/meme/抽象/history`、`/api/meme/咕咕嘎嘎/history` …（服务端日志同为 `%E6%8A%BD%E8%B1%A1` 等） |
| 8 | 键盘 | 聚焦卡片后派发 `keydown` | Space：`true → false`；Enter：`false → true`；焦点保持在该卡片 |
| 9 | 历史失败回退 | 临时覆盖 `fetch` 为 reject 后展开 | 卡片内显示「暂无历史」，无 SVG；列表其余卡片不受影响 |
| 10 | 状态文案 | 直接注入 `error / running / empty / done / degraded` 数据调用 `updateStatus` | 分别为「采集异常」/「首轮采集中…」/「首轮采集中…」/「数据正常」/「降级 · 1 个数据源异常」，notice 与 dot 同步（见下表） |
| 11 | 服务未连接 | 页内覆盖 `fetch` 为 reject 并调用 `refresh()` | 状态「服务未连接」、灰点、轮次/更新时间清空、notice「无法连接服务，正在每 30 秒重试…」、**10 张卡片保留**、`pollTimer` 启动；恢复 fetch 后回到「数据正常」 |
| 12 | SSE 断线 | 保持页面打开，kill 8011 服务进程 | `connMode="poll"`、`sseOpen=false`、`pollTimer=true`、notice「实时推送不可用，已切换为 30 秒轮询」、卡片保留 |
| 13 | is_new 优先级 | 注入对象调用 `isNew()` | 缺失字段 + 1h 前 first_seen → `true`；`is_new:false` + 新 first_seen → `false`；`is_new:true` + 72h 前 → `true`；`{}` → `false` |
| 14 | 转义 | 注入 `family_key='a b/<测试>'`、`canonical='<x>&"'` 调用 `cardHtml()` | `data-key` 为 `a b/&lt;测试&gt;`，名称输出 `&lt;x&gt;&amp;&quot;`，无注入 |
| 15 | 移动端 | CDP 375×812 模拟 | `innerWidth=375`、`scrollWidth=375`、`overflow=0`；panel 347px、SVG 317px；卡片不横向溢出 |
| 16 | 减少动效 | CDP `prefers-reduced-motion: reduce` | `matchMedia=true`；panel `transition-duration=1e-06s`、hover `transform=none` |
| 17 | Console | 全新 page target 打开 + 展开 | `CONSOLE_ERRORS=[]`、`EXCEPTIONS=[]`（无 404、无语法/运行时错误） |
| 18 | 截图 | `Page.captureScreenshot` | `/tmp/wp2c-desktop.png`、`/tmp/wp2c-mobile.png`；人工检查 10 卡片、展开曲线、暗色视觉、移动端均正常 |
| 19 | 收尾 | `curl 8011/9224`、检查进程 | 两个端口均无监听；未触碰 8000 |

状态注入结果（#10）：

| 输入 | statusText | dot | notice |
|---|---|---|---|
| `{error:"network down"}` | 采集异常 | warn | 采集异常：network down；下一轮会自动重试 |
| `{running:true, cycle:null}` | 首轮采集中… | warn | 空 |
| `{status:"empty", cycle:null}` | 首轮采集中… | warn | 空 |
| `{status:"done", cycle:{id:3}, health:{errors:[]}}` | 数据正常 | 绿 | 空 |
| `{status:"degraded", health:{errors:[{source:"search"}]}}` | 降级 · 1 个数据源异常 | warn | 降级：仅部分数据源可用（search），榜单可能不完整 |

## 3. 已知限制与风险

1. **`data/hotmeme.db` 出现一次 schema 变更（需 PM 知悉）**：按验收命令启动服务时，`lifespan`
   调用 `store.init_db()`，其中 Wave 2A 新增的 `_migrate()` 为 `meme_signal` 补了
   `is_new INTEGER NOT NULL DEFAULT 0`。证据：启动前 `shasum` 为 `bd88ecf6…`、mtime 14:15；
   启动后为 `ca21bdd0…`、mtime 14:20；再次启动校验和不变（幂等）；以
   `mode=ro&immutable=1` 只读查询：`cycle=3`、`meme_signal=38` 行、cycle 3 上榜 10 条，
   `is_new` 全为 0 —— **行数未变，未跑采集、未写业务行**。本会话未主动执行任何写库操作，
   但验收命令未指定 `HOTMEME_DB`，无法排除该副作用；建议后续验收统一用
   `HOTMEME_DB=/tmp/xxx.db` 的副本，并请 PM 确认该 schema 变更是否与 Wave 2A 预期一致。
2. 当前库只有 2–3 轮历史，曲线信息量有限；分数轴为自适应（最小跨度 2 分）并标注上下界，
   避免误读；名次轴固定 `1..max(10, 实际最大名次)`，不是动态 0–10。
3. 真实数据 `is_new` 全为 `false`，页面上看不到「新晋」角标；只能通过注入式测试验证优先级与回退。
   若 Wave 2A 最终把字段改成字符串/数字，前端按真值兼容，但建议契约保持 boolean。
4. 历史缓存按 `cycle.id` 失效；同一轮内即使分数微调也不会重取（符合「同 key 缓存」要求）。
5. `openKeys` 不做清理：梗掉出 Top10 后再回来会保持展开状态（可接受，非缺陷）。
6. 卡片使用 `role="button"`，部分读屏器会把内部文本按按钮名朗读；`aria-label` 仅含名次/名称/分数，
   展开后的分项在按钮语义下可能被弱化。若要更严格，可改为卡内独立 disclosure button（后续可选）。
7. 仅用 Chrome 157 headless 验证；Safari/Firefox 未实测。所用 API（fetch/EventSource/Map/SVG/CSS
   media query）均为标准能力，风险低。
8. 降级/采集异常使用注入数据模拟；当前生产库 `status=done` 且无 `health.errors`，无真实降级样本。
9. 未新增自动化测试（约束禁止改 `tests/`）；验证脚本位于 `/tmp`，不随仓库交付。
10. 页面每 30s 轮询仅在 SSE 不可用时启用；正常 SSE 下不轮询，符合原设计。

## 4. 交接说明

- 本会话唯一改动：`app/static/index.html`（`git diff --stat`：418 insertions / 96 deletions）；
  `git status` 中其余 `app/*.py`、`tests/*`、`data/blocklist.json` 的修改属并行 Wave 2A，本会话未触碰。
- 看板可直接联调：`HOTMEME_PORT=8011 HOTMEME_DISABLE_SCHEDULER=1 .venv/bin/python run.py`。
- 若 Wave 2A 后续调整 `is_new` 字段名/语义，只需改 `isNew()`（L179）一处。
