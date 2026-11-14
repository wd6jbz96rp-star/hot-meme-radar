# Shadow tab 交付报告（模块 shadow · 只读对比）

- 会话：shadow-tab（2026-11-15 00:09–00:15）
- 基线：HEAD `03413d1`（工作区另有 lead 未提交的 `docs/pm/BOARD.md` 编辑与 `.archify/`，本会话未触碰）
- 目标：看板加第四个只读 tab「模块 shadow」，展示 `GET /api/meme-info` 的 status/age/榜单/2×2/LLM/证据；**不改 `/api/top` 三 tab、SSE、轮询、sparkline，不写库、不跑采集、不重启 8000**
- 结论：**达成**。tab 行为、字段展示、四种状态（ok/stale/missing/invalid）、空态/降级态、移动端与 headless 验证全部通过；console/page/http errors 均为 0。

---

## 1. 改动文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/static/index.html` | 修改（+412 / -5 行） | 新增第四 tab + shadow 视图、样式、数据请求与状态渲染；原有 3 tab/SSE/轮询/sparkline 逻辑仅做最小分派改造 |
| `docs/pm/assets/shadow-tab.png` | 新增 | shadow tab 桌面全页截图（1280×2815，749 KB） |
| `docs/pm/SHADOW-TAB-REPORT.md` | 新增 | 本报告 |

未触碰（已核对 git status）：`app/*.py`（含 `api.py`/`meme_info_adapter.py`）、`tests/**`、`data/**`、`README*`、`docs/01-02`、`meme_info/**`、`tools/**`。未 commit。`docs/pm/BOARD.md` 的 `M` 是 lead 的既有编辑，本会话未改。

## 2. Tab 行为

### 2.1 结构与交互

```html
<button class="tab" id="tab-shadow" role="tab" data-tab="shadow"
        aria-selected="false" aria-controls="shadowView" tabindex="-1">模块 shadow</button>
<section id="shadowView" class="shadow-view" role="tabpanel" aria-labelledby="tab-shadow" hidden>…</section>
```

- **第四个 tab**：位于「新梗 / 经典 / 全部」之后，`data-tab="shadow"`；`role="tablist"` 内由原有 `syncTabs()` 统一维护 `aria-selected` / `tabindex`（roving tabindex）。
- **localStorage 记忆**：key 仍是 `hotmeme.dashboard.tab.v2`（未升版，向后兼容旧值）；合法值扩展为 `new|classic|all|shadow`。切到 shadow 写入 `shadow`，刷新页面仍停在 shadow（headless 已验证 reload 后恢复）。
- **方向键/Home/End**：原有 `tabsEl` keydown 在 4 个 tab 上自动生效（数组来自 `querySelectorAll('[role="tab"]')`，无需改逻辑）。headless 实测：shadow 上按 `ArrowLeft` → 选中「全部」，`aria-selected`、视图、localStorage 同步，且恢复 `/api/top` 拉取。
- **移动端**：`≤640px` 时 tab 栅格由 3 列改为 **2×2**（4 个 tab 在 390px 一行里点击目标不足 84px）；390×844 实测无横向溢出（`scrollWidth=390`），shadow 卡片改为 rank + 内容两列、score 下移。
- **视图切换**：进入 shadow 时隐藏 `#list`、`#llmBar`（LLM 是通过 `/api/top` 的字段，不能与 shadow 混用），显示 `#shadowView`；切回列表 tab 时恢复，并由 `/api/top` 重绘 header/llmBar/列表。

### 2.2 顶部固定提示

常驻 banner（shadow 视图顶部）：

> **模块 shadow · 仅对比，不参与发布**
> 数据来自 `GET /api/meme-info` 的只读报告：不写库、不触发采集、不替换 `/api/top`。「可发布标记」只是模块发布门结论（hot/rising + 2×2 证据），**不等于已发布**；线上榜单仍由原「新梗 / 经典 / 全部」tab 决定。

卡片上 `可发布标记` 徽标的 `title`、页面 footer `#meta` 也重复标注「仅对比，不参与发布（可发布标记 ≠ 已发布）」，避免被误读为已上线。

## 3. 数据与展示

### 3.1 请求

选中 shadow 时：

```
GET /api/meme-info?limit=10&only=all           # 默认：全量对比
GET /api/meme-info?limit=10&only=publishable   # 勾选「只看可发布」时
```

- 切换 tab / 勾选开关 / 点「刷新」时即时请求；shadow tab 存续期间每 **30s** 轻量重取（更新 age，不依赖 `/api/top` 的 SSE）。
- `refresh()` 第一行分派：`if(currentTab === "shadow") return refreshShadow();`——**shadow 分支永不请求 `/api/top`，也不渲染 `/api/top` 数据**。
- `#shadowList` 与 `#list` 是两个独立容器：shadow 卡片类 `.s-card`，`/api/top` 卡片类 `.panel`；shadow 视图内 `.panel` 数量恒为 0（headless 断言）。

### 3.2 顶部字段（严格对应 API 字段）

| API 字段 | 页面展示 |
|---|---|
| `status` | 彩色徽标 `status ok · 正常 / stale · 过期 / missing · 缺失 / invalid · 无效`，同时写 header 状态灯与 `#statusText` |
| `generated_at` | `generated_at 11/15 00:01:25（11 分钟前）`；null 时显示「未提供（按文件 mtime 估龄）」 |
| `age_seconds` / `max_age_seconds` | `age 11 分钟 / max_age 1.0 小时`（≤90s 显示秒、<1h 分钟、<24h 小时、否则天） |
| `report_path` | `<code>` 展示，`overflow-wrap:anywhere` 防溢出 |
| `publishable_count` | `publishable_count 7 / total_memes 20`；另显示本页 `count` 与当前 `only` |
| `stats` | 视频 / 评论 / 弹幕 / 当前梗族 / 2×2 证据族 / 采集请求 六个 chip；`budget_exhausted=true` 时加「预算耗尽」 |
| `verification`（额外） | 已审/通过/否决/错误、LLM 调用次数、model、key 是否配置 |

### 3.3 每条卡片

按 API `memes[]` 顺序渲染，字段一一对应：

- `rank`（前 3 高亮）、`canonical`、`score`（2 位小数）、`status`（hot 红 / rising 绿 / observing 金 / 其它灰）；
- `publishable` 徽标：`可发布标记`（绿）或 `无可发布标记`（灰），title 写明发布门条件与「未上线、未写库」；
- `evidence_rate_2x2` 徽标：`2×2 证据 ✓` / `2×2 证据 —`；
- `meta_count`（来源视频）、`usage_count`（用法）+ `verified` 摘要：梗性/当下（是/否/未审）、热度、置信、LLM 理由（≤200 字，截断 + title 全文）；`verified` 为 null/空时显示「LLM 审核 · 报告未提供」；
- `evidence` 前 2 条：`quote`（≤120 字）+ 点赞数 + `ctime`（Unix→本地时间）+ `bvid`；无证据时显示「报告未提供评论证据」；
- `meaning`/`type` 存在时附带展示。

每卡 `role="group"` + `aria-label`（名次/名称/分数/status/是否带可发布标记），`#shadowList` 每次重绘前 `aria-busy="true"`，无 CDN、无新依赖，全部输出经现有 `esc()`。

## 4. 状态/空态/降级态

| 状态 | 页面行为 |
|---|---|
| `ok` | 正常展示；header 绿点「模块 shadow · 报告正常」 |
| `stale` | **仍展示数据** + 金色警告条：`status=stale：报告已过期（generated_at=…，age=…，超过 max_age=…）。以下仍展示最近一次数据，未做实时判断，仅供对比参考。` header 黄点「报告过期」 |
| `missing` | 红色警告条 + 空态：报告路径、生成命令 `python -m tools.meme_info_shadow --out …`、明确「不影响 /api/top 与线上发布」；不渲染任何条目 |
| `invalid` | 红色警告条 + 空态：展示 adapter 的 `error`（如 `JSONDecodeError …`），明确「不猜测、不修复」 |
| 服务不可达（fetch 失败） | header 红/灰点「服务未连接」、`paintNotice` 显示重试提示、`aria-busy` 复位；保留已有卡片不闪空；30s 重试（沿用 `startPolling()` 的既有语义） |
| `only=publishable` 过滤后为空 | 空态解释发布门条件，并提供「查看全部（only=all）」按钮一键恢复 |
| old API 缺字段（`stats`/`verification`/`generated_at` 缺失） | 分别降级为「报告未提供 stats」「已审 0…、model 未配置」「按文件 mtime 估龄」，不抛错、不显示 undefined |

## 5. 原有三 tab / SSE / 轮询 / sparkline 兼容性

- `refresh()` 仅在非 shadow 时走原 `/api/top` 逻辑，三 tab 请求参数、`listSignature`、`render`、`updateStatus/updateMeta/updateLlm` 全部未改行为；`/api/top` 的 `new|classic|all` 与 400 校验未动（`api.py` 一行未改）。
- **SSE**：`/api/stream` 连接/断线/轮询兜底逻辑不变；`onmessage` 在 shadow tab 下只修正连接状态并 `paintNotice()`，**不调用 `refresh()`、不消费 `/api/top` 快照**。
- **轮询兜底**：SSE 失败时仍 `startPolling()` 每 30s；在 shadow tab 下该 `refresh()` 会分派到 `refreshShadow()`，因此离线恢复后 shadow 也能自愈。
- **sparkline**：`sparkHtml`/`ensureHistory`/`historyCache`/`.panel` 展开逻辑一行未改，仍只在三个列表 tab 使用。shadow 卡片的家族多数不在 app DB，**刻意不复用** `/api/meme/{key}/history`，避免把两套数据混在同一条走势里。
- 列表 tab 的 `#llmBar`、`#notice`、`emptyActions()`、`aria-live` 等行为保留。

## 6. 验证

### 6.1 启动（只读副本，不碰 8000、不跑采集）

```bash
cp data/hotmeme.db /tmp/shadow-web.db
HOTMEME_PORT=8011 HOTMEME_HOST=127.0.0.1 HOTMEME_DB=/tmp/shadow-web.db \
HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_OFFLINE=1 \
HOTMEME_MEME_INFO_REPORT=/tmp/meme_info_latest.json .venv/bin/python run.py
```

### 6.2 curl（8011）

| 路径 | 结果 |
|---|---|
| `GET /` | **200**，65 KB |
| `GET /api/top?list=all&limit=10` | **200**，8,198 B（10 条，cycle 24） |
| `GET /api/top?list=new&limit=10` | **200**，2,230 B（本 DB 该轮 new 榜为空，既有数据状态，非本改动引入） |
| `GET /api/top?list=classic&limit=10` | **200**，8,202 B |
| `GET /api/meme-info?limit=10&only=all` | **200**，7,676 B；`status=ok, age_seconds=608, max_age=3600, publishable_count=7, total_memes=20` |
| `GET /api/meme-info?limit=10&only=publishable` | **200**，5,879 B（7 条） |

### 6.3 Chrome headless（Chrome 157，CDP 驱动；`--no-sandbox --disable-gpu --user-data-dir=/tmp/...`）

`docs/pm/assets/shadow-tab.png` 为 1280 宽桌面全页截图（banner + 状态头 + stats + 10 张卡片），正常展示：
`status ok · 正常 / generated_at 11/15 00:01:25 / age 11 分钟 / max_age 1.0 小时 / publishable_count 7 / total_memes 20 / report_path /tmp/meme_info_latest.json / stats 944·1025·2000·42·20·178`；
7 张「可发布标记」卡、3 张「无可发布标记」卡，每条含 2×2 徽标、verified 摘要与 2 条评论证据（共 29 个证据条目）。

| 检查项 | 结果 |
|---|---|
| console errors / page exceptions / HTTP ≥400 | **0 / 0 / 0** |
| 桌面 1280：横向溢出 | 0 |
| 移动 390×844：横向溢出 | **0**（`scrollWidth=390`），tab 2×2 布局，banner/状态头可见 |
| 移动 390：原「全部」tab `/api/top` | 10 张卡，溢出 0 |
| shadow tab 内出现 `.panel`（/api/top 卡片） | **0** |
| 切到 shadow 后发生 `/api/top` 请求 | **0**（网络事件：切换前 2 次 = 首屏 + SSE 通知；切换后仅 `/api/meme-info`） |
| tab 点击 → `aria-selected`/localStorage | `true` / `shadow` |
| reload 后恢复 shadow | 选中 true、视图显示、10 卡 |
| 键盘 `ArrowLeft`（shadow） | 切到「全部」，`aria-selected`/localStorage/list 恢复 |
| 「只看可发布」勾选 | 7 卡、全部带 `可发布标记`、`only=publishable` |
| 切回「全部」 | 10 张 `.panel`、`数据正常`、`第 24 轮 · 19.2s · 请求 32`、llmBar 显示「规则版」、溢出 0 |
| 新梗/经典 tab | 新梗 0 卡（该轮数据为空）、经典 10 卡，接口 200 |

### 6.4 降级状态（临时 8012/8013/8014，同 DB 副本；仅页面级验证）

| 场景 | 环境 | 页面结果 |
|---|---|---|
| stale | `HOTMEME_MEME_INFO_MAX_AGE=60` | `status stale · 过期`、黄点「报告过期」、金色警告含 age 11 分钟 / max_age 60 秒、**10 卡仍展示** |
| missing | `HOTMEME_MEME_INFO_REPORT=/tmp/shadow-no-such-report.json` | `status missing · 缺失`、红警告、空态含路径与生成命令、0 卡、header 黄点 |
| invalid | 坏 JSON 文件 | `status invalid · 无效`、红警告、空态输出 `JSONDecodeError …` 原文、0 卡、header `title` 带错误 |

三态均 console/page errors = 0、横向溢出 0；截图 `/tmp/shadow-stale.png`、`/tmp/shadow-missing.png`、`/tmp/shadow-invalid.png`（未入库）。

### 6.5 静态检查

- 内联 `<script>` 提取后 `node --check`：**通过**；
- `id` 无重复；`data-tab` 仅 `new/classic/all/shadow` + 空态按钮里的 `classic/all`；
- 无外链/CDN/新依赖（仍单文件、内联 CSS/JS）。

清理：验证完成后已关闭 8011/8012/8013/8014；8000 全程未运行、未触碰；未写 `data/hotmeme.db`（DB 副本在 `/tmp`）；未跑采集。

## 7. 已知限制

1. **shadow 没有 sparkline/历史**：历史接口是 app DB 的 `family_key` 维度，模块家族与 DB 榜单并未对齐；混用会造成误导，因此 shadow 卡不做展开走势，sparkline 只在原三 tab 保留。
2. **shadow 不接 SSE 推送**：`/api/meme-info` 无推送通道，报告更新靠切 tab、手动「刷新」或 30s 定时重取；页面 age 每 30s 变化，但生成时间本身不实时。
3. **「只看可发布」不跨刷新持久化**：每次进入 shadow 默认 `only=all`（符合 shadow 全量对比口径）；要默认 publishable 可后续加 localStorage。
4. **移动端 tab 从 3 列改为 2×2**：4 个 tab 在 390px 一行会挤压点击目标；三 tab 的键盘顺序与 aria 语义不变，仅视觉排布变化（截图仅存桌面，移动端见 `/tmp/shadow-mobile-390.png`）。
5. **只在 Chrome 157 headless 验证**，未测 Safari/Firefox；`:focus-within`、`overflow-wrap:anywhere` 在现代浏览器均支持。
6. **本页不改变发布行为**：`publishable` 只是模块标记；tab 不写库、不调用 `/api/refresh`、不触发采集。切不切 `list=new` 仍由 BOARD §22 的人工评估决定。
7. 截图 `docs/pm/assets/shadow-tab.png` 为全页 10 卡（749 KB）；如仓库在意体积，可只保留 1280×900 视口版。
8. 8011 验证用的是 `cycle 24` 的旧 DB 副本 + 00:01 的模块报告：`/api/top` 与 shadow 数据时段不同（约 6.5h），本页只做**展示与隔离性**验证，不代表两榜质量对比结论。

## 8. 复现

```bash
# 1) 服务（只读副本 + 现成报告；不碰 8000）
cp data/hotmeme.db /tmp/shadow-web.db
HOTMEME_PORT=8011 HOTMEME_HOST=127.0.0.1 HOTMEME_DB=/tmp/shadow-web.db \
HOTMEME_DISABLE_SCHEDULER=1 HOTMEME_OFFLINE=1 \
HOTMEME_MEME_INFO_REPORT=/tmp/meme_info_latest.json .venv/bin/python run.py

# 2) API
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8011/'
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8011/api/top?list=all&limit=10'
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8011/api/meme-info?limit=10&only=all'

# 3) 截图（headless；automation 脚本在 /tmp/shadow-cdp.js，未入库）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-sandbox \
  --disable-gpu --user-data-dir=/tmp/shadow-chrome --window-size=1280,900 \
  --screenshot=/tmp/shadow.png 'http://127.0.0.1:8011/'

# 4) 状态机 UI：换 env 重启（stale=HOTMEME_MEME_INFO_MAX_AGE=60；missing=指向不存在路径；invalid=坏 JSON）
```
