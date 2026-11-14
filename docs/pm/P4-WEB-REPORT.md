# P4c · 看板 v2：新梗 / 经典 / 全部 tab + LLM 审核展示 · 验收报告

> 会话：frontend-v2（`0c1d10a3-b0c6-401e-ae08-63ecaf063e54`），委派方 lead。
> 项目根：`/Users/wanjunqiang/Desktop/hot_meme_search`，HEAD `0a2b258`。
> 写权限：仅 `app/static/index.html` 与本文件。**未修改** `app/*.py`、`tests/`、`data/`、`README`、`docs/01-02`；未装依赖、未 commit、未跑真机采集。
> 验证服务：`127.0.0.1:8011`（临时库 `/tmp/p4-web.db`），验证完成后已关闭；**未占用/重启 8000**。

## 0. 结论（TL;DR）

| 项 | 结果 |
|---|---|
| 三 tab | ✅ 新梗榜 / 经典榜 / 全部；`?list=new|classic|all`；localStorage 记忆；方向键/Home/End 可切换 |
| LLM 审核展示 | ✅ 顶部 `llm` 摘要（mode/approved/rejected/fallback_reason）+ 每卡 verdict/reason；无 `llm` 字段显示「规则版」 |
| 新梗证据 | ✅ `age_days` / `comment_7d_count` / `comment_like_weighted` 有则显示；`list` 标签；rank=0 显示「观察」 |
| 空态 | ✅ `list=new` 为空显示「本轮没有通过审核的新梗」+「查看经典榜 / 查看全部」按钮（实测可点击） |
| 降级态 | ✅ `fallback_reason` 存在时显示「LLM 未启用/降级，当前为规则版：<reason>」；卡片 verdict 标注「本轮未生效」 |
| 兼容 | ✅ 无 `llm` / 无 `list` / 无 `llm_review` / 无 `components.comment` 均不崩（无 llm 场景已用响应拦截实测） |
| 保留能力 | ✅ sparkline、SSE、30s 轮询、键盘展开、移动端、无 CDN、单文件、`esc()` |
| 前端语法 | ✅ `node --check` OK（658 行内联脚本） |
| 浏览器运行 | ✅ Chrome headless CDP 实测，`CONSOLE_ERRORS=[]` |
| 测试数据 | `/tmp/p3.db` 副本 `/tmp/p4-web.db`（10 条 new / 4 条 classic / 8 条 all，demote）；空态/降级态使用**临时库合成**，见 §2.4 |

## 1. 改动说明（`app/static/index.html`，+311 / −19）

### 1.1 双榜 tab

- 新增 `#tabs[role=tablist]`，三个 `role=tab` 按钮：`data-tab=new|classic|all`，默认 `all`（兼容旧行为）。
- 选择写入 `localStorage["hotmeme.dashboard.tab.v2"]`，读取时校验枚举；localStorage 异常（隐私模式）try/catch 回退 `all`。
- 请求：`/api/top?list=<tab>&limit=10`；切 tab 时 `lastSig=""`、历史缓存 epoch 失效、`aria-busy=true`，响应回来才替换列表。
- `reqSeq` 请求序号 + 发起时 tab 快照，丢弃快速切换 tab 后的过期响应。
- 键盘：左右方向键循环、Home/End 跳转；roving `tabindex`，`aria-selected` 唯一。
- `all` 响应只含 rank>0；`new` / `classic` 可能含 rank=0 的观察项，渲染为「观察」且不显示误导性的 delta。

### 1.2 LLM 审核展示

**顶部 `#llmBar`**

| `llm` 状态 | 展示 |
|---|---|
| 字段缺失 | `规则版` + 「接口未返回 llm 字段，按规则榜展示（兼容旧版 API）」 |
| `mode=off` | `规则版` + 「LLM 审核未启用（mode=off）」 |
| `shadow` / `demote` / `require_approve` | `LLM <mode>` + 模式说明 + `mode=` 原文 + 发布门是否生效 + `通过/否决/已审` + 缺口/沿用轮次 |
| `fallback` 或 `fallback_reason` | `规则版（降级）` + `#llmNotice` 提示「LLM 未启用/降级，当前为规则版：<reason>」 |

- 计数来源 `/api/top.llm`（approved/rejected/reviewed/shortfall/gate_applied/new_list_cycle）。
- 所有插入均 `esc()`；notice 用 `textContent`。

**卡片**

- `llm_review.verdict` → `LLM 通过 / 否决 / 待定 / 合并` 彩色徽标；`merge` 追加 `→ <merge_into>`。
- `reason` 摘要（84 字截断，`title` 存全文），展开卡片显示完整「LLM 审核：verdict · mode · model · 合并至 · 理由」。
- 降级时徽标旁标注「本轮未生效」，避免把审计记录误读成本轮已生效结论；活跃审核模式下 `new` 列表无审核记录的条目标注「LLM 未审核」。
- 兼容：`llm_review=null` / 缺字段时不显示审核行，不崩溃。

### 1.3 新梗证据

- 读取顺序：`components.comment` → `components.raw.comment_evidence` → `components.raw`（P1/P0 两种历史落库形态都覆盖）。
- 展示（有值才输出，`null`/缺失隐藏）：`梗龄 <age_days> 天`、`近 7 天评论 <comment_7d_count>`、`评论赞加权 <comment_like_weighted>`。
- 名称行增加 `list` 标签：`新梗` / `经典`；`rank<=0` 显示「观察」并使用虚线卡片。
- 0 是有效值会显示（如「近 7 天评论 0」），字段缺失才隐藏；实测 `evidenceHtml({components:{comment:{}}}) === ""`。

### 1.4 空态 / 降级态

- `list=new` 且本轮有 cycle、但无条目：
  - 标题「**本轮没有通过审核的新梗**」；
  - 说明按实际状态区分：`fallback` → 规则版；`mode=off` → 审核未启用；无 `llm` → 接口未返回；否则 → 候选均未通过审核；
  - 明确「不是服务故障」+ 两个操作按钮「查看经典榜」「查看全部」，点击即切 tab（实测成功）。
- **没有 cycle**（首轮）仍走原「首轮采集中」spinner，不会误报空榜。
- `fallback_reason` → 顶部独立 `#llmNotice` 提示，`role=status`，不覆盖原有采集降级/连接状态条。
- 采集异常/失败/数据源降级：沿用原 `updateStatus` + `#notice` + `renderOffline` 逻辑，未改文案与判定。

### 1.5 保留项

- sparkline：`ensureHistory` / `sparkHtml` 原样保留；缓存失效键升级为 `轮次|tab|沿用轮次`，切 tab 后重新拉取，避免把另一榜单的周期数据当缓存。
- SSE：每轮推送仍作为“有新数据”的通知；`onmessage` 触发当前 tab 的 `/api/top` 重新拉取（保证 `list`/`llm` 字段一致），不再直接渲染 `all` 快照。
- 30s 轮询、SSE 失败降级轮询、键盘 Enter/空格展开、焦点恢复、`prefers-reduced-motion` 均保留。
- 移动端：tabs 三等分、LLM bar 换行、卡片单列、证据/审核行换行；CDP 以 390×844@2 实测。
- 无 CDN/外链请求；仅内联 data-URI favicon。单文件。

## 2. 验证

### 2.1 环境与启动

```bash
cp /tmp/p3.db /tmp/p4-web.db
HOTMEME_PORT=8011 HOTMEME_HOST=127.0.0.1 HOTMEME_DB=/tmp/p4-web.db \
  HOTMEME_DISABLE_SCHEDULER=1 .venv/bin/python run.py
# 验证结束后 kill；lsof :8011 → 空闲
```

### 2.2 HTTP / API 实测

```text
GET /                          → 200
GET /api/top?list=new          → 200, status=done, n=10, llm.mode=demote, approved=10, rejected=9, fallback_reason=""
GET /api/top?list=classic      → 200, status=done, n=4,  llm 同上
GET /api/top?list=all          → 200, status=done, n=8,  llm 同上
```

真实字段抽样（`list=new` 首条）：

```json
{"rank":0,"canonical":"也不","list":"new","score":83.5,
 "llm_review":{"verdict":"approve","reason":"fake-heuristic","mode":"demote","model":"fake-heuristic"},
 "components":{"comment":{"age_days":16.5,"comment_7d_count":8,"comment_like_weighted":8.07}}}
```

> `reason="fake-heuristic"` 是 P3 无 key 时注入的假 reviewer 文本，如实展示，未伪造为真实 LLM 结论。

### 2.3 浏览器实测（Chrome 157 headless + CDP，真实点击/刷新）

脚本：`/tmp/p4_cdp.mjs`；日志 `/tmp/p4_cdp_out.txt`。
CDP 同时监听 `Runtime.exceptionThrown`、`Log.entryAdded(error/warning)`、`Runtime.consoleAPICalled(error)`。

| 步骤 | 结果 |
|---|---|
| 新 profile 首次打开 | 默认「全部」，8 张卡，`llm=demote` 摘要 |
| 点击「新梗榜」 | 选中 new，10 张卡；全为 rank=0「观察」；`LLM 通过` 徽标 + reason + 证据 |
| 点击「经典榜」 | 选中 classic，4 张卡，全部「经典」标签 |
| 键盘 ArrowRight（焦点在经典榜） | 切到「全部」，`aria-selected` 唯一 |
| 选「新梗榜」后刷新页面 | 仍为「新梗榜」，`localStorage=...tab.v2=new` |
| 展开首条卡片 | `.spark-svg` 存在；文本「最近 3 轮走势…分数 83.3 → 83.5 ▲0.2」 |
| 移动端 390×844@2 | tabs 三等分、卡片单列、无横向溢出；展开正常 |
| console errors | `CONSOLE_ERRORS=[]` |

截图（临时目录，未写入仓库）：`/tmp/p4-web-shots/`

| 文件 | 内容 |
|---|---|
| `01-tab-all.png` | 全部榜 8 卡、LLM demote 摘要、新/经典混排 |
| `02-tab-new.png` | 新梗榜 10 个观察项、LLM 通过 + 证据行 |
| `03-tab-classic.png` | 经典榜 4 卡 |
| `04-tab-new-expanded-sparkline.png` | 新梗卡展开 sparkline |
| `05-mobile-new.png` / `06-mobile-new-expanded.png` | 390 宽移动端 |
| `07-empty-new.png` | 空态标题 + 两个操作按钮 |
| `08-no-llm-field.png` | 无 `llm` 字段 → 规则版 |
| `09-fallback.png` | 降级提示 + 「本轮未生效」 |

### 2.4 空态 / 降级态 / 无 llm 实测

> 三种都是**临时库/响应拦截的受控场景**，不是生产数据；服务只监听 8011。

1. **空新梗榜**：`/tmp/p4-empty.db`（`/tmp/p3.db` 副本，cycle 3 stats 改为 `require_approve` + 0 通过，删除该轮 `llm_review`）。
   - API `new` 返回 0 条；页面显示「本轮没有通过审核的新梗 / 本轮候选均未通过 LLM 审核，不是服务故障 / 可以切到经典榜…」；
   - 点击「查看经典榜」→ `selectedTab=classic`、4 张卡、`storage=classic`，`EMPTY_ACTION_OK=true`。
2. **LLM 降级**：`/tmp/p4-fallback.db`（副本，stats 改 `mode=shadow`、`fallback=true`、`fallback_reason=not_configured: 未设置 HOTMEME_LLM_API_KEY`）。
   - 顶部 `规则版（降级） LLM mode=shadow 本轮未生效，已回退规则榜 通过 4 · 否决 2 · 已审 6`；
   - `#llmNotice` 显示「LLM 未启用/降级，当前为规则版：not_configured: 未设置 HOTMEME_LLM_API_KEY」；
   - 卡片 verdict 徽标旁「本轮未生效」。
3. **无 `llm` 字段**（兼容旧 API）：CDP `Fetch` 拦截 `/api/top` 响应删除 `llm` 后重放页面。
   - `#llmBar` →「规则版 接口未返回 llm 字段，按规则榜展示（兼容旧版 API）」；
   - 空态副文案同步说明「当前接口未返回 LLM 审核信息」，无异常；`CONSOLE_ERRORS=[]`。

### 2.5 verdict 渲染与 `esc()` 探针

在页面运行时传入合成 `llm_review`（不写库、不改变服务状态）：

```text
approve → "LLM 通过"
reject  → "LLM 否决"
unclear → "LLM 待定"
merge   → "LLM 合并 → 抽象"
reason  → "测试 <script>alert(1)</script>" 被转义；scriptInjected=false, reasonEscaped=true
evidence 缺字段 → ""（隐藏）；仅 c7 → 只显示「近 7 天评论 3」
```

### 2.6 语法检查

```bash
sed -n '/^<script>$/,/^<\/script>$/p' app/static/index.html | sed '1d;$d' > /tmp/p4_index.js
node --check /tmp/p4_index.js
# node --check: OK (658 lines)
```

## 3. API 字段兼容性

| 缺失/异常字段 | 前端行为 |
|---|---|
| 无 `llm`（或非对象） | 顶部「规则版」，不显示审核统计；卡片不显示“未审核”噪音 |
| `llm.fallback_reason` 缺失/空 | 不显示降级条；`fallback=true` 仍显示「规则版（降级）」 |
| 无 `list`（旧 API） | 不显示新/经典标签；按接口顺序渲染 |
| `llm_review=null` / 无 verdict | 不显示审核行（活跃审核的 new 项显示「LLM 未审核」） |
| reason 空 | 只显示徽标，不显示空 reason |
| 无 `components.comment` / `raw.comment_evidence` | 证据行整行隐藏 |
| 证据单个字段缺失 | 只隐藏该字段，其余照常 |
| `rank=0`（观察项） | 显示「观察」，虚线卡片，不显示 delta；history 仍可展开 |
| 无 `cycle` / `stats` | 维持原「首轮采集中」；meta 清空 |
| 单条 `item` 非法（缺 rank） | 渲染前过滤，不抛异常 |

## 4. 已知限制与说明

1. **新梗榜数据质量**：`/tmp/p3.db` 的 approve 条目是 fake reviewer 判的「也不/烂梗/还没」等垃圾候选，页面如实展示为「观察 + LLM 通过 + fake-heuristic」，未美化、未伪造。真实 LLM 质量待有 key 的 shadow 验证。
2. **本报告的空态/降级截图是受控临时库**：来自 `/tmp/p3.db` 副本的 stats 改写（明确标注），仅证明 UI 分支，不代表生产轮次。
3. **8000 未动**：全程未向 8000 发请求；最终检查 8000 仍为原 PID 39002 LISTEN。8011 已关闭（`8011 free`）。
4. **生产库 `data/hotmeme.db`**：本会话未读写；观察其 mtime 在 17:03 变化（既有 8000 服务/并行会话的调度写入），非本会话操作。所有验证使用 `/tmp/p4-web.db`。
5. **SSE**：每轮推送后额外一次 `?list=<当前tab>` 请求（低频，10 分钟一轮），换取 tab 与 `llm` 字段一致；未改变 SSE 端点契约。
6. 切 tab 时旧列表保留到新响应返回（`aria-busy` 半透明）；这是有意避免闪烁，不视为旧数据展示。
7. `classic` / `all` 保留全局名次（如 1/3/5/7），没有在 tab 内重新编号；与 `/api/top` 的 rank 语义一致。
8. 截图仅在 `/tmp/p4-web-shots/`，**未加入仓库**（写权限只允许改 `index.html` 和本报告）。

## 5. 变更文件

| 文件 | 改动 |
|---|---|
| `app/static/index.html` | +311/−19：tabs/localStorage、`#llmBar`/`#llmNotice`、verdict+reason、证据行、rank=0 观察项、空态操作、移动端、SSE 改为按 tab 重取 |
| `docs/pm/P4-WEB-REPORT.md` | 本文件 |

未 commit；`git status` 中 `app/bili.py`、`app/config.py`、`app/pipeline.py`、`tests/*`、`docs/02` 等为并行会话改动，非本会话产物。
