# 前端 UI：两 tab 只读看板（Aceternity 组件化重做）

- 日期：2026-11-16
- 范围：`app/static/index.html`（单文件、无新依赖、无 CDN/外部字体/构建步骤）
- 迭代：
  1. 重做两 tab 数据看板并清理旧榜死代码（commit `eb43104`）；
  2. 第一次 Aceternity 风格视觉升级（基于组件名与视觉语言）；
  3. 本次：**读取 Aceternity UI registry 真实组件源码**（`/registry/<name>.json` 的 `.tsx` 内容 + `/api/components` 元数据）后，选择一套适合「只读数据看板」的组件组合，重新实现场景、Bento、卡片交互与文字动效。
- 结论：**完成**。页面只轮询 `GET /api/meme-info`；桌面 1280 / 移动 390 无横向溢出；console / page exception / Log error / HTTP≥400 = **0 / 0 / 0 / 0**；ok / stale / missing / empty / 离线 / reduced-motion / XSS 转义均实际验证。

![桌面 1280 · 新梗榜](assets/ui-desktop.png)

## 1. 组件选型：为什么是这一套

Aceternity 组件大多面向营销落地页（强动效、强氛围）。本项目是**只读数据看板**，首屏要读报告状态和榜单，因此选型原则是：

1. 动效只服务层级，不遮挡数据；
2. 背景最多两层光效，避免「landing page 化」；
3. 交互仅增强 hover 反馈，不改变阅读路径；
4. 不引入 React / Tailwind / Motion / Three.js 等运行时，只复刻视觉参数。

最终组合（8 个组件 + 2 类 section 参考）：

| # | Aceternity 组件（registry key） | 选用原因 | 原生实现（本项目） |
|---|---|---|---|
| 1 | **Spotlight New**（`spotlight-new`） | 顶部两条 45° 光柱，比经典 Spotlight 更克制，适合深色数据看板 | `.scene-spotlight` + `.spotlight-col.left/right`：每侧 3 个旋转渐变矩形，`translateY(-290px) rotate(±45deg)`，基础渐变取自源码 `hsla(210,100%,85%,.08~.10)`，`@keyframes spot-sway-*` 12s / ±70px 缓慢摆动（源码 560×1380、7s ±100px、translateY(-350)，此处按视口缩放） |
| 2 | **Dotted Glow Background**（`dotted-glow-background`） | 点阵缓慢呼吸，增强 hero 深度但不抢数据 | `<canvas id="dotsCanvas">` 原生实现：18px 网格、radius 1.5、per-dot `phase` + `speed`(0.4–0.9)、alpha 0.25–0.8 三角波、亮度 >0.62 时 7px 阴影辉光；限制在 hero 下方 540px 区域，30fps 上限、`IntersectionObserver` 离屏暂停、标签页隐藏暂停；`prefers-reduced-motion` 只绘制一帧静态点阵 |
| 3 | **Grid and Dot Backgrounds**（`grid`） | 64px 网格 + 径向遮罩，提供「控制台/观测台」结构感 | `.scene-grid`：两层 `linear-gradient` 网格 64px，`mask-image: radial-gradient(ellipse ...)` 顶部淡出 |
| 4 | **Bento Grid**（`bento-grid`） | 非对称状态板比横排小格更有层级，适合状态/统计混排 | `.bento` 12 栏 grid；status 跨 5 列 2 行，pub/age/gen/page 不等宽，LLM 审核通栏；`group-hover` 式内容位移（hover 时大数字 +3px） |
| 5 | **Card Spotlight**（`card-spotlight`） | 卡片内指针跟随柔光，提升玻璃质感且不影响文字 | `.meme-card` 背景 `radial-gradient(340px circle at var(--mx) var(--my), ...)`，指针坐标由委托 `pointermove` + rAF 写入（源码默认 radius 350 / mask 到 80%，此处取 340px） |
| 6 | **Glowing Effect**（`glowing-effect`） | 卡片边缘冷光描边，是这套组件的「签名感」，hover 时才出现 | `.card-glow`：`repeating-conic-gradient(from calc((var(--start) - 30) * 1deg), ...)` + `mask-composite: exclude` 只保留 1px 边框；JS 计算指针相对卡片中心角度写入 `--start`，hover 加 `.is-glowing`；品牌蓝→薄荷配色（源码为多色 rainbow，按本项目语义收敛） |
| 7 | **3D Card Effect**（`3d-card`） | 轻微透视倾斜增强 hover 触感，但必须限幅 | 沿用源码公式 `x=(clientX-left-w/2)/25，y=(clientY-top-h/2)/25`，再 **clamp 到 ±3°**（源码不 clamp，对数据卡会过度倾斜）；`perspective(1200px)`，90ms 过渡，触屏/reduced-motion 关闭 |
| 8 | **Text Generate Effect**（`text-generate-effect`） | hero 标题逐字 blur 入场，只播一次，不干扰后续阅读 | `h1` 拆 4 个 `.tg-char`，每个字符独立渐变 background-clip；`opacity 0 + blur(10px)` → `1/0`，0.72s + 85ms stagger（源码 words + 0.5s + 0.2s stagger，此处按中文字符调整）；reduced-motion 直接显示 |
| 9 | **Empty States**（blocks 参考） | 空/缺失/损坏/离线要有「系统状态」感，而不是空白 | `.state` 中心 `.state-orb`（径向高光球 + 虚线光环）+ 两圈缓慢旋转虚线轨道；文案与按钮仍由状态机驱动 |
| 10 | **Stats Sections**（blocks 参考） | 报告状态需要「一眼看到数字」 | Bento 大数字 + publishable 真实比例进度条 + LLM 审核 ok/no/err 三段堆叠条（宽度由 `verification` 真实计数计算） |

**未采用及原因**：

- **Background Beams / Beams With Collision**：与 Spotlight New 属同类顶部光效，叠加会过亮且 SVG/motion 路径成本高。
- **Aurora / Background Gradient Animation**：大面积流动渐变会让数据看板变成营销页，且移动端 blur 成本高。
- **Wobble Card**：鼠标位移 ± 缩放偏 playful，会干扰长列表阅读。
- **Meteors / Sparkles / Canvas Reveal / WebGL shader**：需要 canvas/three 或高频粒子，性能与信息密度不匹配。
- **Moving Border**：更适合 CTA 按钮；本页 tab/刷新按钮已有清晰 focus 与 hover，无需持续动画。
- **Layout Grid / Card Stack / Draggable Card**：交互模型是「浏览/选择」而非「扫描榜单」，不适用。
- **React / Tailwind / Motion 运行时、shadcn CLI**：只读参考源码参数，不引入依赖；不复制其 React 代码。

参考入口：[组件目录](https://ui.aceternity.com/components) · [组件元数据 API](https://ui.aceternity.com/api/components)（114 components / 9 blocks / 184 examples）· [Spotlight registry](https://ui.aceternity.com/registry/spotlight.json) · [Dotted Glow registry](https://ui.aceternity.com/registry/dotted-glow-background.json) · [Bento Grid registry](https://ui.aceternity.com/registry/bento-grid.json) · [Glowing Effect registry](https://ui.aceternity.com/registry/glowing-effect.json)。

## 2. 设计目标

1. **先看报告状态，再看榜单**：hero 摘要（状态/年龄/可发布数）+ Bento 状态板（status、generated_at、age/max_age、publishable/total、本页条数、LLM 审核）。
2. **publishable 不歧义**：明确「**publishable 是发布门标记，不等于已发布**」，卡片把 publishable / 2×2 / LLM 拆成三个独立徽标。
3. **可解释优先**：新梗榜卡片保留 LLM 结论（梗性/当下/热度/置信）+ 完整理由 + 前 2 条证据。
4. **两个视图角色分明**：新梗榜=发布门视图（蓝青冷光）；模块 shadow=全量观察视图（虚线、紫色 accent、OBSERVE、观察徽标）。
5. **不伪造数据**：stale/missing/invalid/空 全部显示真实状态与 report_path/error，不补位、不回退旧接口。
6. **高级感克制**：背景/动效是「控制台氛围」，数据仍是第一层；移动端、键盘、reduced-motion 全部优先。

## 3. 信息架构

```
Scene        fixed：grid / Spotlight New / Dotted Glow canvas / vignette（纯装饰，aria-hidden）
Hero         品牌 + eyebrow + 逐字渐变大标题 + 副标题 + hero-meta（状态/年龄/可发布）+ 连接 pill + 刷新
Bento        6 张玻璃瓦片：报告状态(大) / 可发布·总条数 / 报告年龄 / 生成时间 / 本页条数·scope / LLM 通过·已审
Viewbar      新梗榜（publishable）｜模块 shadow（only=all）        ← 方向键 / Home / End
Policy       “publishable 是发布门标记，不等于已发布” + 判定公式
Notice       stale / missing / invalid / 离线 / 预算耗尽解释条
Active View
  ├─ 新梗榜        卡片：rank · canonical · status · publishable · 2×2 · type · meta/usage
  │                LLM 结论 + 理由 · 前 2 条证据 · score；hover 指针光 + 冷光描边 + 轻倾斜
  └─ 模块 shadow   OBSERVE banner + 全量卡片（虚线、紫色 rank、观察徽标、同字段）
Details      <details> 报告与审核明细：stats / verification / report_path / age
Footer       字段口径 · 数据来源与边界 · 已知限制 · 报告路径
```

## 4. 视觉系统与动效

- **背景**：近黑 `#05070c`；64px 网格 + 顶部两条 Spotlight New 光柱 + canvas 点阵呼吸 + 边缘 vignette。
- **层次**：hero（大字号）→ Bento（玻璃大数字）→ tabs/policy → 数据卡（信息密度最高）→ details/footer（弱化）。
- **玻璃卡**：`linear-gradient + backdrop-filter: blur(18px)` + 1px 低对比边框 + inset 高光；hover 边框/外发光 + 位移 ≤3px。
- **语义色**：绿=ok/publishable/LLM通过，琥珀=stale/预算耗尽，红=invalid/missing/LLM否决，紫=观察视图/LLM 审核框，蓝=2×2/信息。
- **动效周期**：Spotlight 12s 缓摆、点阵 30fps 呼吸、hero 0.72s 入场、bento 0.65s 错峰、卡片 hover 0.35s 冷光 + 0.09s 倾斜。无高频闪烁。
- **降级**：`prefers-reduced-motion` 停止 Spotlight/点阵动画、逐字入场直接显示、卡片 transform 关闭、点阵只画静态帧；触屏/无 hover 设备不启用指针光、倾斜、Glowing Effect。
- **断点**：1280（12 栏 Bento）、≤1000（status 通栏，其余 2 列，卡片 score 下移）、390（2 列 Bento、hero 纵向、tab 等宽、canvas 高 360px）。

## 5. 组件与 API 字段映射

| 组件 | 字段 | 说明 |
|---|---|---|
| hero-meta | `status / age_seconds / publishable_count / total_memes` | 顶部快速摘要，与 Bento 同源 |
| Bento status | `status` | 中文 + 原始 code chip + `data-tone` 驱动光球/大字水印（OK/STALE/MISSING/INVALID/OFF） |
| Bento publishable | `publishable_count / total_memes` | 大数字 + 真实比例进度条，副文案强调“不等于已发布” |
| Bento age | `age_seconds / max_age_seconds` | 友好化 + max_age 与原始秒数 |
| Bento generated | `generated_at` | 缺失时“未提供”，注明按文件 mtime 估龄 |
| Bento 本页 | `count` + 当前 tab 的 `only/limit` | 明确“本页”不是全量 |
| Bento LLM | `verification.ok/attempted/rejected/errors/model` | 大数字 + ok/no/err 三段堆叠条（真实计数计算宽度） |
| 卡片 rank | `rank` | 报告全量序号，过滤后允许跳号；`sr-only` 读作“报告全量排序第 N 位” |
| 卡片标题 | `canonical`（回退 `family_key`） | 缺失时“未命名条目” |
| status 徽标 | `status` | hot 红 / rising 绿 / 其他中性 |
| publishable 徽标 | `publishable` | true=“publishable”，false=“未达发布门”；title 写公式与“≠已发布” |
| 2×2 徽标 | `evidence_rate_2x2` | true=“2×2 证据 ✓”，false=“2×2 证据 —” |
| type / metrics | `type / meta_count / usage_count` | 缺失显示“—” |
| meaning | `meaning` | 有值才渲染段落（当前报告普遍为空） |
| LLM 框 | `verified.is_meme / is_current / heat / confidence / reason / error` | 未提供时明确“报告未提供该条审核结论” |
| 证据 | `evidence[0..1]` 的 `quote/like/ctime/bvid` | 前 2 条；无证据时“报告未提供评论证据” |
| score | `score` | 2 位小数 + 0–100 细条 |
| 明细 | `stats.* / verification.* / report_path / age_seconds / max_age_seconds` | 只展示 API 实际字段，缺失写“报告未提供 stats” |

## 6. 状态与空态（全部实际验证）

| 场景 | 构造 | 页面行为 |
|---|---|---|
| `ok` | 线上报告（20:32 生成，20 条 / 16 条 publishable） | 绿色“正常（ok）”、水印 OK、新梗榜 8 卡、shadow 10 卡、无 notice |
| `stale` | 临时副本 generated_at 改为 2h15m 前，默认 max_age=3600 | 琥珀“已过期（stale）”、水印 STALE、notice 含 generated_at / age 2.3 小时 / max_age 1.0 小时；保留 8 卡并标注非实时 |
| `missing` | report 指向不存在路径 | 红色“缺失（missing）”、水印 MISSING、两个 tab 均显示“报告文件缺失”+ 路径、0 卡 |
| `invalid` | 坏 JSON | 红色“损坏（invalid）”、显示 adapter 原文、0 卡（沿用上一轮实测，逻辑未变） |
| memes 为空（`ok`） | 合法 schema + `memes: []` | 新梗榜“本轮没有可发布条目”+ 去 shadow 按钮；shadow“报告全量列表为空” |
| 离线 / 请求失败 | 页面加载后断网 + 点击刷新 | header 变红“服务连接失败”、notice 说明、保留上一次成功数据、恢复后刷新回正常（沿用上一轮实测，逻辑未变） |
| 预算耗尽 | `stats.budget_exhausted=true` | 琥珀 notice 提示“条目可能不完整” |
| 未知 status | 非 ok/stale/missing/invalid | 琥珀“未知报告状态”，按未验证处理 |

![过期状态（桌面 1280）](assets/ui-stale.png)

## 7. 交互与可访问性

- 轮询：仅当前 tab 每 30 秒请求一次；页面隐藏暂停，回到前台补一次；手动“刷新”同一路径；`cache:no-store` + `reqSeq` 丢弃过期响应。
- 网络请求（CDP 实测）：`GET /` + `GET /api/meme-info?limit=8&only=publishable` / `GET /api/meme-info?limit=10&only=all`；无 `/api/top`、history、stream、health、refresh。
- Tab：`role=tablist/tab/tabpanel`、`aria-selected` + roving tabindex；`← → Home End` 可切换；写入 `localStorage["hotmeme.ui.tab.v3"]`。
- 指针交互：`pointermove` 事件委托 + rAF 节流，写入 `--mx/--my`（Card Spotlight）、`--start`（Glowing Effect）、`--rx/--ry`（3D Card）；只在 `(hover:hover) and (pointer:fine)` 且非 reduced-motion 时启用；触屏/键盘不依赖。
- 键盘：CDP `ArrowRight` 实测切 tab 且 `aria-selected`/面板显隐同步；`focus-visible` 2px 外框；skip link。
- 读屏：`connPill`、hero-meta、notice 为 `aria-live=polite`；列表 `aria-busy`；卡片 `<ol>/<li>/<article>` + `h3` + `aria-labelledby`；score `role=img` + aria-label；rank 提供 `sr-only`；scene/canvas/card-glow/水印均 `aria-hidden`。
- 转义：所有 API 字符串统一 `esc()` 后进入 `innerHTML`；样式宽度仅来自数字计算并 clamp；无外链资源、无 CDN、无 `eval`。
- 安全验证：伪造报告注入 `<img onerror>` / `<script>` / `<svg onload>` 到 canonical/meaning/type/reason/evidence 后，`window.__xss*` 全 undefined、注入节点 0、`<img` 以字面文本显示，console/exception 0。

## 8. 与旧版差异 / 清理项

### 8.1 累计删除的死代码（相对于旧 4-tab 实时榜）

- `/api/top` 请求与回退分支、`/api/meme/{family_key}/history`、`/api/stream` EventSource（subscribe/sseOpen/connMode/旧轮询）。
- 历史走势：`historyCache`、`historyEpoch`、`ensureHistory`、`paintHistory`、`sparkHtml`、`histEmpty`、sparkline SVG/CSS。
- 展开卡片：`toggleCard`、`openKeys`、`.panel.open` / `.detail` / `.chips` / `.bar` / `.toggle` / `aria-expanded`。
- 旧榜渲染：`render`、`renderList`、`listSignature`、`renderEmpty`、`renderOffline`、`updateStatus`、`updateMeta`、`updateLlm`、`paintNotice`、`setDataNotice`、`cardHtml`、`deltaHtml`、`commentEvidence`、`evidenceHtml`、`verdictTag`、`reviewDetail`、`llmSummary`、`isNew`、`TYPE_ORDER`、`REASON_LABEL`、`VERDICT_LABEL`、`MODE_LABEL`、`TAB_LABEL`。
- 旧 UI：`#llmBar` 模式条、经典/全部 tab 残留、`#list` 旧容器、Delta/新晋/代表用法/分项 reasons、`.tab-badge 模块版`、重复 banner；shadow「只看可发布」开关；旧 storage key `hotmeme.dashboard.tab.v2`。
- 页面内 `/api/top`、history、sparkline、`EventSource` 出现次数均为 **0**。

### 8.2 本次相对上一版的变化

- 背景由「模糊径向光晕 + 光束 + aurora」替换为 **Spotlight New + Dotted Glow canvas + Grid**（读源码后按真实参数实现）。
- hero 标题改为 **Text Generate Effect** 逐字入场 + 逐字渐变。
- 卡片新增 **Glowing Effect** 冷光描边（conic gradient + mask）与 **Card Spotlight** 指针柔光；3D 倾斜公式改为源码 `／25` 并 clamp ±3°。
- 清理误留在卡片上的旧 `::after` radial ring；`glow` 改为独立 `.card-glow` 子层。
- 恢复基础工具类（`.page/.mono/.muted/.sr-only/.skip-link` 等）并修复标题被 `background-clip` + filter 子层导致的不可见问题。

量化（相对上一提交）：HTML 1547 → **1667 行**（+120），CSS 550 → 568 行，JS 798 → 901 行，文件 80.9 → 87.7 KB；`git diff --stat`：`+177 / −57`。

## 9. 验证结果

环境：macOS + Chrome 157 headless（CDP 驱动，独立 profile），临时服务 `127.0.0.1:8011`（未触碰 8000），报告 `/tmp/meme_info_latest.json`（20:32 生成）；状态用例使用 `/tmp` 临时副本。

| 检查项 | 结果 |
|---|---|
| `GET /` / `only=publishable&limit=8` / `only=all&limit=10` | 200 / 200 / 200 |
| console errors / page exceptions / Log errors / HTTP≥400 | **0 / 0 / 0 / 0** |
| 桌面 1280 横向溢出 | 无（`scrollWidth=1280`，`scrollHeight=3352`） |
| 移动 390 横向溢出 | 无（`scrollWidth=390`，`scrollHeight=4858`） |
| Scene | 4 层 scene；2 个 spotlight 列 / 6 个渐变矩形；canvas 1280×540，mode=animated |
| Hero Text Generate | 4 个字符，动画后 opacity 全 1，h1 aria-label=热梗雷达 |
| Bento | 6 瓦片、12 栏；状态 tone=ok；publishable 16/20（80%）；LLM 22/40（ok/no 堆叠条） |
| 新梗榜 | 8 卡；8 publishable、8 LLM 理由、16 条证据；首卡 rank 1 / 司凤音乐 / 79.07 |
| hover | is-glowing / is-tilting=true；`--start=82.0`，`--rx=1.07° / --ry=3.00°`，glow opacity 0.92，Card Spotlight 背景生效 |
| 模块 shadow（键盘 ArrowRight） | 10 卡、10 observe、10 card-glow、scope=only=all·limit=10、无溢出 |
| reduced-motion（加载前模拟） | canvas mode=static、Spotlight 动画 none、逐字 opacity=1、卡片 transition≈1e-06s、无溢出 |
| stale | 状态“已过期（stale）”、水印 STALE、notice 2.3h / max_age 1h、保留 8 卡、无溢出 |
| empty / missing | “本轮没有可发布条目” / “报告文件缺失”；水印 OK / MISSING；0 卡，不伪造 |
| 转义/XSS | `window.__xss*` 全 undefined、注入节点 0、字面文本渲染、console 0 |
| 网络 | 仅 `/` 与 `/api/meme-info?...` |

截图（Chrome headless + CDP `Page.captureScreenshot` full page；自动化脚本在 `/tmp`，未入库）：
桌面 1280×3352、hover 1280×3352、移动 390×4858、stale 1280×3411。

复现：

```bash
cd /Users/wanjunqiang/Desktop/hot_meme_search
HOTMEME_PORT=8011 HOTMEME_HOST=127.0.0.1 \
HOTMEME_MEME_INFO_REPORT=/tmp/meme_info_latest.json \
  .venv/bin/python run.py

curl -s -o /dev/null -w 'GET / -> %{http_code}\n' http://127.0.0.1:8011/
curl -s 'http://127.0.0.1:8011/api/meme-info?limit=8&only=publishable' | head -c 300
curl -s -o /dev/null -w 'GET all -> %{http_code}\n' 'http://127.0.0.1:8011/api/meme-info?limit=10&only=all'
# 读取参考源码（只读）：
curl -s https://ui.aceternity.com/registry/spotlight-new.json | head -c 500
curl -s https://ui.aceternity.com/registry/dotted-glow-background.json | head -c 500
```

## 10. 已知限制

1. **点阵 canvas 有绘制成本**：约 2k 个点、30fps 上限，仅 hero 区域；离屏/隐藏暂停、reduced-motion 静态。极低端设备上仍比纯 CSS 背景耗电。
2. **Spotlight New 为氛围光**：低透明度 CSS 渐变，截图/低亮度屏幕上偏淡；这是有意的克制，避免数据看板变成 landing page。
3. **指针效果仅桌面**：Card Spotlight / Glowing Effect / 3D 倾斜只在 fine pointer + 非 reduced-motion 启用；触屏仅保留静态 hover 视觉。
4. **非当前 tab 不轮询**：首次进入前另一个 tab 的计数显示“–”。
5. **页面不生产数据**：worker 约 860s 一轮，age/stale 是报告新鲜度，不是页面刷新时间。
6. **publishable 不 top-up**：过滤后可能不足 8 条，rank 会跳号；`publishable_count/total_memes` 为报告级计数。
7. **shadow 只显示前 10 条**；更多条目需读 API。
8. 无历史曲线 / 无 SSE / 无发布动作；当前报告 `meaning` 基本为空，卡片有值才显示。
9. 仅 Chrome 157 headless 验证；未在 Safari/Firefox 与真实读屏软件下人工验收。
10. 截图为 2026-11-16 20:32 报告快照与临时 stale 副本，不是固定 fixture；PNG 未压缩（1.3–2.1 MB/张）。

## 11. 文件清单与边界

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/static/index.html` | 升级 | 两 tab 只读看板；Spotlight New / Dotted Glow canvas / Grid / Bento / Card Spotlight / Glowing Effect / 3D Card / Text Generate / Empty+Stats；单文件内联 CSS/JS |
| `docs/pm/UI-DESIGN.md` | 更新 | 本报告（含真实 registry 源码参考映射） |
| `docs/pm/assets/ui-desktop.png` | 更新 | 桌面 1280 · 新梗榜 |
| `docs/pm/assets/ui-mobile.png` | 更新 | 移动 390 · 新梗榜 |
| `docs/pm/assets/ui-hover.png` | 更新 | 桌面 hover：Card Spotlight + Glowing Effect + 3D 轻倾斜 |
| `docs/pm/assets/ui-stale.png` | 更新 | stale 状态桌面截图 |

未触碰：`app/*.py`、`meme_info/`、`tests/`、`data/`、`README.md`、`docs/01-02`、`docs/pm` 其他文件、8000 端口服务；未 commit。

## 12. 截图

![桌面 1280 · 新梗榜](assets/ui-desktop.png)

![桌面 1280 · hover 冷光描边与轻倾斜](assets/ui-hover.png)

![桌面 1280 · 过期状态](assets/ui-stale.png)

![移动 390 · 新梗榜](assets/ui-mobile.png)
