# 采集性能 / 异步改造设计（WP2）

- 会话：async-design（session-`22cb2e18-c5f8-479b-bed4-7a8276ca4b87`）
- 基线：git `84a4b28`，只读审计，未改任何代码/测试/数据/README
- 证据来源：`app/http.py`、`app/bili.py`、`app/pipeline.py`、`app/api.py`、`app/config.py`、`app/store.py`、`docs/02-capture-and-scoring.md`、生产库 `data/hotmeme.db`（`mode=ro`）
- 标记约定：**【实测】**= 有命令/文件行号/DB 查询支撑；**【推算】**= 由实测数据代入公式；**【推测】**= 假设驱动，需 WP3 压测确认

---

## 0. 结论先行

| # | 结论 | 性质 |
|---|---|---|
| 1 | 一轮 43.0s = **38.9s 网络（串行 2.21 rps）+ 4.13s CPU/DB**；两轮实测数据唯一可解，模型自洽（复现 43.0s） | 【推算】 |
| 2 | **限速器 3 rps 从未生效**：单请求往返 452ms > 限速间隔 333ms，`RateLimiter.wait()` 每次都算不出等待。真正卡住吞吐的是「串行往返」，不是限速 | 【推算】 |
| 3 | **推翻 PM 结论的一半**：「naive async 无收益」不成立——异步能把 43.0s 压到 32.8s（−10.2s），上限是 `N/G = 86/3 = 28.7s`。但 32.8s 只勉强压线 35s，且**必须以 3 rps 违规运行为前提** | 【推算】 |
| 4 | **分端点限速桶无法为「提高总 rps」背书**：86 个请求中 84 个打 `api.bilibili.com`（另 2 个是 spi/nav），风控看的是**域名总频率**，不是端点。分桶只能用于「保护敏感端点、隔离故障」，不能用于「把总 rps 抬到 5」 | 【实测】 |
| 5 | 因此硬约束是：**合规 2 rps 下，35s 预算只够 61 个请求**（`(35−4.13)×2`）。现状 86 个请求在 2 rps 下需要 47.1s——**今天 43.0s 是靠 2.21 rps 轻微超线换来的** | 【推算】 |
| 6 | **推荐 C 优先（砍请求），A 有条件（并发）**：C1（cid 复用 + 评论缓存）把 86 → 31 请求，2 rps 全合规串行即 **19.6s**，且不引入任何线程；A 的并发只在批准提 rps 到 3 后才有额外收益（14.5s） | 【推算】 |
| 7 | 方案 B（asyncio+httpx 全量重写）**拒绝**：2 rps 下收益 0（47.1s），3 rps 下与 A 同为 32.8s，但改动面、依赖、回归成本是 A 的 5 倍以上 | 【推算】+【推测】 |
| 8 | PM 的「CPU 与 SQLite 保持串行」**成立**：CPU/DB 段仅 4.13s（9.6%），并行上限收益 4s，风险（SQLite 多写、判定不确定性）远大于收益 | 【实测】 |

**一句话**：先砍 25 个 pagelist + 30 个评论请求（零并发、零新依赖、2 rps 内合规），把轮耗时从 43s 压到 19.6s；并发改造作为可选第二步、默认关闭，只有 PM 明确批准放宽到 3 rps 时才打开。

---

## 1. 请求清单与耗时预算

### 1.1 实测两轮（只读查询，可复现）

```bash
sqlite3 "file:data/hotmeme.db?mode=ro" \
  "SELECT id,status,started_at,finished_at,stats_json FROM cycle ORDER BY id DESC LIMIT 3;"
```

| 轮次 | 请求 | 耗时 | danmaku_cached | comment_requests | 错误 |
|---|---|---|---|---|---|
| cycle#1（冷） | **136** | **65.6s** | 0 | 30 | 0 |
| cycle#2（热） | **86** | **43.0s** | 50 | 30 | 0 |

> `stats_json` 里没有分端点请求计数——**这是第一个监控缺口**（见 §6.4）。

### 1.2 请求清单（warm 态 86 个，逐项可核对）

| 阶段 | 来源（文件:行） | 端点 | 请求数 | 说明 |
|---|---|---|---|---|
| fetch_videos · 分区榜 | `pipeline.py:35-40` → `bili.py:276-296` | `api.bilibili.com/x/web-interface/ranking/v2` | **8** | `ZONES` 8 个分区 |
| fetch_videos · 热门 | `pipeline.py:41` → `bili.py:298-315` | `api.bilibili.com/x/web-interface/popular` | **1** | `ps=50` |
| fetch_videos · 搜索 | `pipeline.py:46-51` → `bili.py:317-340` | `api.bilibili.com/x/web-interface/wbi/search/type` | **20** | `SEARCH_QUERIES` 20 组 |
| 会话初始化 · buvid | `bili.py:218-235`（`_ensure` 触发） | `api.bilibili.com/x/frontend/finger/spi` | **1** | 每轮 1 次（`_bootstrapped` 进程内不跨轮） |
| 会话初始化 · wbi 密钥 | `bili.py:242-254`（首次 `sign` 触发） | `api.bilibili.com/x/web-interface/nav` | **1** | 每轮 1 次 |
| collect_docs · 弹幕 | `pipeline.py:97-124` → `bili.py:378-391` | `comment.bilibili.com/{cid}.xml` | **0** | 50 个目标全部命中 `danmaku_cache`（TTL 24h） |
| collect_docs · 补 cid | `pipeline.py:99-101` → `bili.py:342-359` | `api.bilibili.com/x/player/pagelist` | **25** | 搜索视频 `cid=None`，每轮重补 |
| collect_docs · 评论 | `pipeline.py:126-147` → `bili.py:361-376` | `api.bilibili.com/x/v2/reply` | **30** | 每个目标 1 页（匿名每页 ~3 条 < 20 → break） |
| **合计** | | | **86** | 与 `stats.requests` 完全一致【实测】 |

冷态 136 = 86 + 50（弹幕全未缓存），与 cycle#1 完全一致【实测】。

### 1.3 关键发现：**86 个请求，84 个打同一个域名**

- `api.bilibili.com`：8 + 1 + 20 + 1 + 1 + 25 + 30 = **86**（其中 spi/nav 2 个也是该域）
- `comment.bilibili.com`：**0**（已全缓存）

> 风控（`-352`/`412`）按**域名/IP/UA** 计数，不按 path。这意味着：**分端点限速桶在「提高总 rps」上是无效的**——把 5 个端点的桶各设 2 rps，域名总频率仍是 10 rps，风控看到的就是 10 rps。分桶的真实价值只有两个：① 避免把额度浪费在低价值端点上（如搜索把预算吃光导致评论全失败）；② 单端点故障能被隔离（熔断粒度）。**这条推翻了「分桶后可到 4-5 rps」的前提**（见 §6）。

### 1.4 耗时公式与反推

串行执行下，一轮墙钟：

```
T_cycle = T_net + T_cpu_db
T_net   = N × max(1/G, RTT_eff)        # 串行：限速间隔与往返取大
```

两轮实测只有两个未知数（边际成本 `m`、固定成本 `C`）：

```
136m + C = 65.6
 86m + C = 43.0
→ m = 22.6 / 50 = 0.452 s/req   （纯请求 2.21 rps）
→ C = 43.0 − 86×0.452 = 4.13 s  （extract/judge/score + 7 次 commit + prune）
```

校验：`86/43.0 = 2.00`（整体有效 rps），`1/0.452 = 2.21`（纯请求 rps）。**「有效 rps ≈ 2」不是限速的结果，是往返时延的结果**。

### 1.5 为什么实测 43s 明显高于「理论下限 32.8s」

| 口径 | 计算 | 结果 |
|---|---|---|
| 理论下限（G=3 生效） | 86/3 + 4.13 | **32.8s** |
| 实测 | — | **43.0s** |
| 差额 | 86×(0.452−0.333) | **10.2s** |

根因在 `http.py:60-64`：

```python
def wait(self) -> None:
    delta = time.monotonic() - self._last
    if delta < self.min_interval:          # 333ms
        time.sleep(self.min_interval - delta)
    self._last = time.monotonic()
```

`_last` 在**请求发出前**打点，下一次调用时 `delta` ≈ 上一请求的**完整往返**。实测往返 452ms > 333ms，条件永远不成立 → **限速器一次都没睡过**，`MAX_RPS=3` 是死配置。吞吐完全由「串行 × 往返」决定。

**这同时证明了两件事**：
1. 想提速，要么**减少请求数**（直接乘性收益），要么**重叠往返**（并发）；
2. 单纯把 `HOTMEME_MAX_RPS` 从 3 改到 10，**一分钱都省不下来**（0 收益），因为瓶颈不在这里。

### 1.6 预算边界表（35s 目标）

| 全局上限 G | 35s 内最多请求数 `(35−4.13)×G` | 现状 86 个请求的耗时 |
|---|---|---|
| **2 rps（docs/02 合规线）** | **61** | **47.1s** ❌ 超线才勉强达标 |
| 3 rps（当前配置，超文档） | 92 | 32.8s（串行做不到，需并发） |
| 4 rps | 123 | 25.6s |

> **结论**：想「合规 + 达标」，请求数必须 ≤ 61。现状 86 无解。这是 §3 推荐 C 优先的量化依据。

---

## 2. HttpClient 并发安全审计

审计对象：`app/http.py` 全文 + `bili.py` 的会话状态。Python 3.15.1（`.venv/bin/python -V`【实测】）。

### 2.1 逐项判定

| # | 对象 | 位置 | 是否安全 | 依据 / 后果 |
|---|---|---|---|---|
| 1 | `self.opener`（OpenerDirector） | `http.py:73-76` | ⚠️ **实现级可用，契约级不安全** | `open()` 只使用局部变量，`process_request/handle_open/...` 构造后只读（`inspect.getsource(OpenerDirector.open)` 已核【实测】）。但官方未声明线程安全，且 handler 链上的 `HTTPCookieProcessor` 携带可变状态。**结论：可共享，但不得在运行期再改 `addheaders`/`handlers`** |
| 2 | `self.jar`（CookieJar） | `http.py:71` | ✅ 结构安全 / ⚠️ 语义竞态 | CPython `CookieJar.__init__` 建了 `_cookies_lock = threading.RLock()`，`add_cookie_header`/`extract_cookies` 全程持锁（源码已核【实测】）→ 不会崩、不会损坏。但**读-写交错**：线程 A 正在 `bootstrap()` 写 buvid3，线程 B 已带着旧 cookie 发出请求 → 个别请求缺 buvid3，可能触发 `-352` |
| 3 | `RateLimiter._last` | `http.py:58-64` | ❌ **不安全（最严重）** | 读-改-写三步无锁。N 线程同时进入：都读到同一个 `_last` → 都算出同一个 `sleep(Δ)` → 同时醒来 → **瞬时突发 N 倍 rps**，恰好是风控最敏感的形态 |
| 4 | `self.requests += 1` | `http.py:77,135` | ❌ 少计 | `+=` 是 LOAD/ADD/STORE 三条字节码，GIL 保证单条原子、不保证组合原子 → 并发下丢更新。`stats.requests` 会偏低，**污染所有以请求数为基准的收益核算** |
| 5 | `_curl_get` 计数与限速 | `http.py:151-166` | ❌ **双重缺陷** | ① `subprocess.run(...)` 之后才 `self.limiter.wait()`（`:165`）→ **curl 请求本身完全没被限速**，且 `requests` 不计数（`:135` 只给 urllib 路径）；② 并发下会同时 fork N 个 curl 进程；③ `result.stdout` 无大小上限 |
| 6 | `bootstrap()` / `_bootstrapped` | `bili.py:218-235` | ❌ check-then-act | 并发首请求时 N 个线程同时判定 `not _bootstrapped` → N 次 spi 请求、N 次 `set_cookie`。`force=True`（`-352` 路径 `bili.py:213-215`）时更糟：`_wbi_keys = None`（`:234`）与另一线程的 `sign()` 竞态 → `TypeError: 'NoneType' is not subscriptable` |
| 7 | `_wbi_keys` / `_wbi_keys_load` | `bili.py:196,242-254` | ⚠️ 加载安全、刷新不安全 | 加载完成后是不可变 tuple，纯读安全；风险仅来自 `:234` 的置 `None` |
| 8 | `sign()` / `_mixin_key()` | `bili.py:256-273` | ✅ 安全 | 纯函数：读 tuple + `time.time()` + 局部变量，无共享可变状态。**前提是 keys 不被并发清空**（见 #6/#7） |
| 9 | `self.errors.append` | `bili.py:202` | ✅ 安全 | `list.append` 单次调用原子；顺序不确定但仅是展示 |
| 10 | `self.context`（SSLContext） | `http.py:72` | ✅ 安全 | SSLContext 创建连接是可共享的（官方文档明示，且 urllib 多线程复用是常见用法） |
| 11 | 连接复用（每线程连接 vs 共享） | `http.py:137` | ✅ 无此问题 | `AbstractHTTPHandler.do_open` **每次请求新建 `http.client` 连接**，urllib 没有连接池 → 「每线程连接」这个选项在 urllib 下不存在，只有「共享 opener + 每请求新连接」一种。TLS 握手成本已含在 452ms 往返里 |
| 12 | `store.connect(check_same_thread=False)` | `store.py:20` | ⚠️ 安全阀 ≠ 许可证 | 允许跨线程用同一个 conn，但 sqlite3 内部串行化只避免崩溃，不避免 `database is locked` 与事务交错。**必须保持单写者（§4.5）** |

### 2.2 最小改造点（按优先级，全部可独立回滚）

| 序 | 改造 | 位置 | 最小 diff |
|---|---|---|---|
| M1 | `RateLimiter` 改为**槽位预约** + `threading.Lock` | `http.py:53-64` | 12 行 |
| M2 | `requests` 计数加锁 / 改 `itertools.count()`；`_curl_get` 补计数并把 `wait()` 移到 `subprocess.run` **之前** | `http.py:135,151-166` | 6 行 |
| M3 | 新增 `BiliClient.warmup()` = `bootstrap()` + `_wbi_keys_load()`，**主线程 fan-out 前调用一次** | `bili.py:190-199` | 8 行；这一步让 #6/#7 在正常路径上永不触发 |
| M4 | `bootstrap()` / `_wbi_keys_load()` 加 `threading.Lock` 双检（防御性）；`_wbi_keys` 刷新用 `self._wbi_keys, keys = keys, keys` 原子替换，**不置 None** | `bili.py:218-254` | 10 行 |
| M5 | 并发执行器只包裹「输入参数 → 输出 bytes/dict」，**不传 conn、不写 DB、不读 `client.errors`** | 新增 `app/fetch.py` | 新文件 |

**明确不做**：
- ❌ 不每线程建一个 `HttpClient`（会复制 CookieJar → buvid3 不同步 → `-352` 概率上升，且 `requests` 计数分裂）
- ❌ 不给 urllib 造连接池（等于把方案 B 偷偷塞进 A）

> M1 的核心实现（唯一正确写法：**锁内完成槽位预约**，睡眠在锁外）：

```python
class Bucket:
    def __init__(self, rps: float, jitter_ms: int = 0):
        self.interval = 1.0 / max(0.1, rps)
        self._lock = threading.Lock()
        self._next = 0.0
        self._jitter = jitter_ms / 1000.0

    def _reserve(self) -> float:
        with self._lock:                    # ← 三段临界区，保证 N 线程拿到互不相同的槽位
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.interval
            return slot

    def wait(self) -> None:
        delay = self._reserve() - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if self._jitter:
            time.sleep(random.uniform(0, self._jitter))
```

正确性：`sleep` 在锁外 → 不串行化网络；`_next` 在锁内推进 → 严格保序、不突发。**这比「加锁包住整个 wait()」更好**：后者会把并发退化成串行。

---

## 3. 方案对比

### 3.1 总表（N=86，G 为 `api.bilibili.com` 域级 rps，C=4.13s，RTT=0.452s）

| 方案 | 请求数 | G | 并发 | 预计轮耗时 | 相对现状 | 改动面 | 风险 | 回滚 | 测试成本 |
|---|---|---|---|---|---|---|---|---|---|
| **现状** | 86 | 3（失效） | 1 | **43.0s**（实测） | — | — | — | — | — |
| **合规基线** | 86 | 2 | 1 | 47.1s | +4.1s | 0 | 无 | 改配置 | 0 |
| **A** 同步+线程池+分桶 | 86 | 3 | 4 | **32.8s**（31–36） | −10.2s | 中 | 中 | 高（`c=1` 即回退） | 中 |
| **A（合规 G=2）** | 86 | 2 | 4 | 47.1s | **0** | 中 | 中 | 高 | 中 |
| **C1** cid 复用+评论缓存 | **31** | 2 | 1 | **19.6s**（18–22） | −23.4s | **小** | **低** | 高（配置开关） | 低 |
| **C2** C1+搜索缓存 | 11 | 2 | 1 | 9.6s（9–12） | −33.4s | 小 | **中**（池子冻结） | 高 | 低 |
| **A+C1** | 31 | 2 | 4 | 19.6s（**并发 0 收益**） | −23.4s | 中大 | 中 | 高 | 中 |
| **A+C1**（若批 3 rps） | 31 | 3 | 4 | **14.5s** | −28.5s | 中大 | 中 | 高 | 中 |
| **B** asyncio+httpx | 86 | 2 | 8 | 47.1s（**0 收益**） | 0 | **大** | **高** | 低（要回滚整个采集层） | 高 |
| **B**（若批 3 rps） | 86 | 3 | 8 | 32.8s | −10.2s | 大 | 高 | 低 | 高 |

> **A（G=2）和 B（G=2）都是 0 收益**，因为 2 rps 是硬上限，串行 2.21 rps 已经打满。这不是估算误差，是 `T_net = N/G` 的结构性结论：**只要 G 不变，并发与异步都无法让 86 个请求快于 43s**。

### 3.2 方案 A：保留同步 + ThreadPoolExecutor 有界并发 + 分端点桶

| 项 | 内容 |
|---|---|
| 改动文件/函数 | `app/http.py`：`RateLimiter`→`Bucket`、`HttpClient.get`（取桶）、`HttpClient.request_count`（加锁）；`app/bili.py`：`warmup()`、`bootstrap`/`_wbi_keys_load` 双检；`app/pipeline.py`：`collect_docs` 拆三段（§4.5）、新增 `fetch_all`；`app/config.py`：6 个新键 |
| 预计轮耗时 | 86 请求 / G=3 / c=4 → **32.8s**；P95 抖动（jitter 120ms + 桶粒度）→ **31–36s** |
| 收益来源 | 把「串行 × 452ms 往返」换成「4 路并发 + 桶节流」，吞吐从 2.21 → 3.0 rps |
| 风险 | ① 线程池 + 共享 CookieJar 的语义竞态（`-352` 概率上升）；② 计数/限速改造若写错 → **突发超速**，比慢更危险；③ 快 10s 但离 35s 仅 2s 余量，一次 P95 抖动就破线 |
| 可回滚性 | **高**。`HOTMEME_FETCH_CONCURRENCY=1` → 退化为串行（M1/M2 的锁在 c=1 下无行为变化）；分桶键缺省值可让 `Σrps_e` 等于旧 `MAX_RPS` |
| 测试成本 | 中。需新增：桶的确定性测试（mock `time.monotonic`，断言 N 线程槽位=0,1,2…×interval）、`c=1` 与 `c=4` 结果一致性、离线 fixtures 全绿 |

### 3.3 方案 B：asyncio + httpx 全量重写采集层

| 项 | 内容 |
|---|---|
| 改动文件/函数 | 重写 `http.py`、`bili.py` 全部 I/O 方法（`get/get_json/get_text/_curl_get/ranking/popular/search_videos/comments/danmaku/cid_for/hot_search/bootstrap/_wbi_keys_load`）；`pipeline.collect_docs` 改 async 或 `to_thread` 混合；`run_cycle` 需 async 版本；`api.py` 的 `asyncio.to_thread(pipeline.run_cycle)` 改 `await`；新增 `httpx` 依赖 |
| 预计轮耗时 | 86 / G=3 / c=8 → **32.8s**（与 A 相同，因为桶是瓶颈）；G=2 → 47.1s（0 收益） |
| 风险 | ① **依赖安装**（本项目当前 0 第三方 HTTP 依赖，httpx 引入 h11/httpcore/anyio 链）；② 本机 Python 3.15 的 CA 校验必须显式 cafile（`http.py:31-50`），httpx 需重做一遍；③ `_curl_get` 的 412 兜底要改成 async subprocess；④ CookieJar 语义要换（httpx.Cookies）；⑤ wbi 签名链路重测；⑥ **无任何现成测试覆盖**（`tests/` 仅 `test_pipeline_offline.py` 间接触及） |
| 可回滚性 | **低**。git 里是一次大 diff，新旧两套采集层要长期共存才能灰度 |
| 测试成本 | 高。等同于把采集层的验收重做一遍（8 个端点 + 412/352/CA/压缩 4 条异常路径） |
| 判定 | **不推荐**。在 G=3 下与 A 等价，在 G=2 下更差；用 5 倍成本买同一结果 |

### 3.4 方案 C：先砍请求，再决定是否并发

| 子项 | 机制 | 省请求 | 数据新鲜度代价 | 定级 |
|---|---|---|---|---|
| **C1a** cid 复用 | `video.cid` 已有 251/380 行；但 `梗搜索` 类 **cid 全为 NULL**（129→130 行，两次查询间漂移，因生产库仍被主线写入；`SELECT zone,COUNT(*),COUNT(cid) FROM video GROUP BY zone`【实测】）。根因：`upsert_videos` 的 `ON CONFLICT ... cid=excluded.cid`（`store.py:186-193`）会把搜索视频的 `cid=None` **反向覆盖**；且 `collect_docs` 里补到的 cid 从未回写 | **−25** | **无**（cid 是不变量） | P0 |
| **C1b** 评论缓存 | `COMMENT_CACHE_SECONDS` 已定义（`config.py:77`）但**全仓零引用**【实测 grep】。按 `(aid,page)` 缓存 1800s | **−30** | 10 分钟一轮 → 每 3 轮才刷一次评论。评论仅占语料 90/(16444+90)=**0.54%**，可接受 | P0 |
| **C1c** 搜索缓存 | 搜索 20 组结果 20 分钟内几乎不变；TTL 1200s | −20（轮换态） | **中**：候选池 42%（159/377）被冻结 20 分钟，「新面孔」信号变钝 | P1（可关） |
| **C1d** 弹幕缓存 | **已生效**（24h TTL，`CACHE_TTL_SECONDS`） | −50（对冷轮） | 无 | 已完成 |
| 合计 C1（a+b） | | **86 → 31**（−64%） | 评论每 3 轮刷新 | |
| 合计 C1+C2（a+b+c） | | **86 → 11**（−87%） | 候选池 20 分钟冻结 | |

**C1 的轮耗时**（2 rps 合规、完全串行、零并发）：`31×0.5 + 4.13 = 19.6s`（区间 18–22s）
**C2 的轮耗时**：`11×0.5 + 4.13 = 9.6s`（区间 9–13s）

> 注意 C1 对**冷轮无效**：冷轮弹幕未缓存，仍是 31+50=81 请求（44.6s）。生产库 `danmaku_cache` 现有 50 行、且弹幕按 cid 命中率取决于目标池稳定性；稳态下才是 31。**首轮/缓存过期轮仍需 A 的并发兜底**——这是 A 的真实价值定位：**削峰，不是提速**。

### 3.5 推荐与理由

**推荐：C1（P0）→ 有条件 A（P1，默认 `c=1` 关闭）。B 拒绝。**

理由（全部可量化）：
1. **C1 在合规线内就把 43 → 19.6s，是唯一同时满足「≤35s」与「≤2 rps」的路径**。A/B 在 2 rps 下收益为 0（结构性，不是调参问题）。
2. **C1 不引入并发**，线程安全审计（§2）的 12 项风险全部不触发；回滚只需配置开关，diff 约 120 行。
3. **A 的价值是削峰**：冷轮（81 请求）与缓存过期轮仍会破 35s，只有并发能把尖峰压下去。所以 A 应当**独立 PR、默认关闭、按需打开**，而不是作为主改造。
4. **B 在两种 rps 假设下都不优于 A**，且引入外部依赖与 CA 回归面。
5. 排序与 PM 的「先砍请求」一致，但**理由需要修正**：不是因为「naive async 无收益」（它在 G=3 下能省 10.2s），而是因为**合规线 G=2 才是天花板，砍请求是唯一在 G=2 下有效的杠杆**。

---

## 4. 详细设计

### 4.1 限速：域级 + 端点级两层桶

**先纠正一个前提**：86/86 个请求同域，因此**域级桶才是风控对齐的那一层**，端点桶是保护层。

| 层 | 键 | 默认 rps | 作用 |
|---|---|---|---|
| 域级 | `api.bilibili.com` | **2.0**（`HOTMEME_RPS_API`，缺省 = `HOTMEME_MAX_RPS`） | **合规主闸**，对应 docs/02 的 2 rps |
| 域级 | `comment.bilibili.com` | 2.5（`HOTMEME_RPS_DANMAKU_DOMAIN`） | 独立域名，可略高 |
| 端点级 | ranking | 1.5 | 最敏感，且只需 8 次 |
| 端点级 | popular | 1.0 | 单发 |
| 端点级 | search（wbi） | 1.0 | `-352` 高发端点，压到最低 |
| 端点级 | pagelist | 1.5 | 与 player 同域 |
| 端点级 | comments | 1.5 | 匿名限流严 |

- **取桶顺序固定**：`域级 → 端点级`（永不反向 → 无死锁）。等效 `rps_e = min(rps_domain, rps_endpoint)`，端点桶只会更严。
- **jitter**：`sleep(uniform(0, HOTMEME_FETCH_JITTER_MS))`，默认 **120ms**，在槽位睡眠**之后**追加。目的：打散 N 线程的同相位（N 线程在同一毫秒醒来是风控最易识别的特征）。代价：每请求 +60ms 均值 → 31 请求 +1.9s，可接受。
- **不做「Σrps_e > G 就提速」的推理**（§1.3 已推翻）。

### 4.2 并发度

```
c = clamp(ceil(RTT_p95 × G × 安全系数), 1, 8)
  = ceil(0.60 × 2.0 × 1.3) = 2   # 合规 2 rps 下只需 2 路就够打满
  若 G=3：ceil(0.60 × 3 × 1.3) = 3
```

默认 `HOTMEME_FETCH_CONCURRENCY=4`（3 rps 场景留余量）；`=1` 即完全回滚为串行。

**关键**：并发度只决定「能否打满桶」，不决定吞吐。G=2 时 c=2 与 c=8 的耗时完全一样（19.6s），所以**没有必要为了性能开大并发**——开的都是风险。

### 4.3 超时与重试

| 项 | 现行 | 建议 | 理由 |
|---|---|---|---|
| 超时 | 12s（`HTTP_TIMEOUT`） | 采集段 **8s**（`HOTMEME_FETCH_TIMEOUT`），保留 12s 作 API 默认 | 452ms 均值 → 8s = 17.7 倍，已极宽松；12s × 3 次尝试 = 36s 会独占整个预算 |
| 重试 | `retries=2`（共 3 次） | 保留，仅对幂等 GET | 现有语义正确 |
| 退避 | `0.8×(n+1)` 固定（`http.py:148`） | `0.8×2^n + uniform(0,0.5)` | 固定退避在并发下会同步重试，形成二次脉冲 |
| 412/403 | 直接 curl 兜底，不重试（`http.py:140-145`） | 保留 + **计入熔断** | 412 是风控信号，不能当普通错误吞掉 |
| 429 | 未处理 | 视为 412 同类，**暂停域桶 60s** | 显式退让 |
| 总预算 | 无 | 采集段 `deadline`：进入 collect 前记 `t0`，超 90s 未完成则放弃剩余任务、用已取到的数据继续 | 防止单端点拖死整轮（当前无保护） |

### 4.4 熔断与失败降级

```python
class CircuitBreaker:          # 每端点一个，进程内单例，threading.Lock 保护
    fails = 5; window = 60; cooldown = 120
```

| 状态 | 触发 | 行为 |
|---|---|---|
| CLOSED | — | 正常 |
| OPEN | 60s 窗口内失败 ≥5 且失败率 >50% | 该端点**立即返回空**（不发请求），记 `source_error`；省下的 rps 额度让给其他端点 |
| HALF_OPEN | 冷却 120s 后 | 放行 1 个探针，成功 → CLOSED，失败 → 再 OPEN 120s |

**与现有降级链的衔接**（全部已存在，只需扩展，不需重写）：

| 层 | 现有机制 | 扩展 |
|---|---|---|
| L1 | curl 兜底（`http.py:151`） | 保持，修 M2 的两个缺陷 |
| L2 | 端点失败 → `[]`（`bili.py:288-290` 等） | 保持 |
| L3 | `zone_counts[name]==0` → `status="degraded"`（`pipeline.py:406`） | 保持 |
| L4 | 弹幕缓存（24h） | 新增 **stale-while-error**：网络失败时允许使用 ≤6h 的过期弹幕缓存（`HOTMEME_STALE_MAX=21600`） |
| L5 | 评论缓存（新增） | 同上 |
| L6 | 上一轮快照继续服务（`api.py:42-50`） | 保持 |

### 4.5 SQLite 单写者

**原则：worker 线程零 DB 访问。** 取数并发、落库串行。

现状问题：`collect_docs` 把网络、缓存读、缓存写、`insert_text_samples` 混在一个循环里（`pipeline.py:104-141`），30 个视频 = **30 次 `conn.commit()`**（`store.py:214-229` 每次 commit）。

拆成三段：

```
① plan_docs(conn)     主线程    纯读：批量 get_danmaku_cache / get_comment_cache / cid_map
                                → List[FetchTask(kind, key, url, params)]
② fetch_all(tasks)    线程池    纯网络 + parse → List[FetchResult(key, raw, parsed, error)]
                                ★ 不传 conn、不写 DB、不碰 client.errors
③ persist(conn)       主线程    单事务：put_danmaku_cache / put_comment_cache /
                                insert_text_samples / update_video_cid → 1 次 commit
```

| 保障点 | 做法 |
|---|---|
| 单写者 | 只有主线程持有 `conn`；`collect_docs` 签名里 `conn` 不再传入 worker 闭包 |
| 事务 | ③ 用 `with conn:` 包住全部写入 → 1 次 fsync（现在 30+ 次） |
| 隔离级别 | 保持 `WAL` + `synchronous=NORMAL`（`store.py:22-23`）；`busy_timeout=15s` 已是兜底 |
| 断言 | 加 `assert threading.current_thread() is threading.main_thread()` 于 `persist()` 入口（`HOTMEME_DEBUG_WRITER=1` 时启用），把误用变成立即失败而不是偶发 `database is locked` |
| 读写分离 | `snapshot()` / `top_items()` 走独立只读 conn（`api.py:94-99` 已是），WAL 下并发读不阻塞写 |

**不需要**：连接池、`check_same_thread=True`（会破坏 `api.py` 的 `to_thread` 模式）、重试写循环。

### 4.6 api.py：单任务队列 + asyncio.Lock

**已确认缺陷**（BOARD §5 第 1 条）：`/api/refresh` 的检查（`api.py:121`）与置位（`:127`，在 `_refresh` 内部）分离，中间有 `await asyncio.create_task` 边界；`_scheduler` 同样「检查（`:27`）→ 置位（`:28`）」。两者可同时通过检查 → **两轮采集并发跑**，两个 `run_cycle` 各开一个 conn → 写冲突 + 请求翻倍（风控风险）。

设计：

```python
RUN_LOCK = asyncio.Lock()
TIMER: asyncio.Queue[str] = asyncio.Queue(maxsize=1)   # 容量 1 = 天然去重

async def _worker():                       # 进程内唯一消费者
    while True:
        reason = await TIMER.get()         # 'scheduler' | 'manual'
        async with RUN_LOCK:               # 双保险：即使误投递两次也串行
            STATE["running"] = True
            try:
                snap = await asyncio.to_thread(pipeline.run_cycle)
                STATE["snapshot"], STATE["error"] = snap, ""
                await _broadcast(snap)
            except Exception as exc:
                STATE["error"] = str(exc)[:300]
            finally:
                STATE["running"] = False
                TIMER.task_done()

# /api/refresh
try:
    TIMER.put_nowait("manual")
except asyncio.QueueFull:
    raise HTTPException(409, "已有采集任务在跑/排队")
return JSONResponse({"started": True, "queued": True}, status_code=202)
```

| 要点 | 说明 |
|---|---|
| 原子性来源 | 事件循环单线程 + `put_nowait` 无 `await` → **入队成功 == 占位成功**，检查与置位不可能被切开 |
| 409 语义 | 队列满（容量 1）→ 409；语义从「正在跑」扩展为「正在跑或已排队」，与 BOARD 契约兼容（409 仍是「已在跑」） |
| 去重 | 调度器 tick 与手动刷新撞车时，只跑一次（多的被 QueueFull 拒） |
| 取消 | `lifespan` 关闭时 `worker.cancel()`；`finally` 保证 `running=False` |
| 不阻塞 | `run_cycle` 仍走 `asyncio.to_thread`（SQLite 是同步的），不引入 async DB 驱动 |
| SSE | 不改 |

### 4.7 配置键（全部 `HOTMEME_*`，缺省值即「安全默认」）

| 键 | 默认 | 说明 | 回滚语义 |
|---|---|---|---|
| `HOTMEME_MAX_RPS` | `3`（现状）→ **建议 `2`** | 域级全局上限（保留旧键，兼容 README） | 调大=提速+风险 |
| `HOTMEME_RPS_API` | 缺省取 `MAX_RPS` | `api.bilibili.com` 域桶 | |
| `HOTMEME_RPS_DANMAKU` | `2.5` | `comment.bilibili.com` 域桶 | |
| `HOTMEME_RPS_RANKING` | `1.5` | 端点桶 | |
| `HOTMEME_RPS_SEARCH` | `1.0` | 端点桶（wbi） | |
| `HOTMEME_RPS_COMMENTS` | `1.5` | 端点桶 | |
| `HOTMEME_RPS_PAGELIST` | `1.5` | 端点桶 | |
| `HOTMEME_FETCH_CONCURRENCY` | **`1`**（先关）/ 压测后 `4` | 线程池大小 | **`1` = 完全回滚串行** |
| `HOTMEME_FETCH_JITTER_MS` | `120` | 抖动上限 | `0` = 关 |
| `HOTMEME_FETCH_TIMEOUT` | `8` | 采集段超时 | |
| `HOTMEME_FETCH_RETRIES` | `2` | 重试次数 | |
| `HOTMEME_DEADLINE` | `90` | 采集段总预算（秒） | |
| `HOTMEME_CIRCUIT_FAILS` | `5` | 熔断阈值 | `0` = 关熔断 |
| `HOTMEME_CIRCUIT_COOLDOWN` | `120` | 熔断冷却 | |
| `HOTMEME_COMMENT_CACHE_SECONDS` | `1800` | **接上已有配置** | `0` = 关缓存（回现状） |
| `HOTMEME_SEARCH_CACHE_SECONDS` | `1200` | 搜索缓存 | `0` = 关 |
| `HOTMEME_CID_REUSE` | `1` | cid 复用 | `0` = 回现状 |
| `HOTMEME_STALE_MAX` | `21600` | 网络失败时允许的过期缓存上限 | `0` = 不降级 |
| `HOTMEME_DEBUG_WRITER` | `0` | 开启 `persist()` 的线程断言 | |

> 所有并发相关键的缺省值都必须让**行为等价于今天**（`c=1`、`jitter=0` 之外的桶不改变总 rps），这样单个 PR 合入即可上线，不会悄悄变快或变险。

---

## 5. 收益估算

### 5.1 公式

```
T_cycle = C + max( N / G_domain ,  max_e(N_e / rps_e) ,  ceil(N / c) × RTT_p95 )
其中 C = 4.13s（extract+judge+score+DB，与请求数无关，实测反推）
```

### 5.2 假设表

| 参数 | 值 | 来源 |
|---|---|---|
| RTT（含 TCP+TLS 握手） | **452ms** 均值；300–600ms 区间 | 【实测】边际反推 |
| C（CPU+DB） | **4.13s** | 【实测】两轮联立求解 |
| 域名 G | 2.0（合规） / 3.0（现状配置） | `docs/02:95` / `config.py:17` |
| 弹幕缓存命中率 | 稳态 100%（50/50）、冷轮 0% | 【实测】cycle#1 vs #2 |
| 评论缓存命中率 | 稳态 100%（TTL 1800s > 轮间隔 600s） | 【推测】 |
| 搜索缓存命中率 | 稳态 ~95%（每 20 分钟失效一次，摊到 2 轮） | 【推测】 |
| 并发 c | 1（C） / 4（A） | 设计值 |
| 抖动 | jitter 120ms → +60ms/请求 | 【推测】 |

### 5.3 结果

| 场景 | N | G | c | 网络 | +C | **合计** | 区间（RTT 300–600ms） |
|---|---|---|---|---|---|---|---|
| 现状 | 86 | 3 | 1 | 38.9 | 4.13 | **43.0s**（实测吻合） | 40–48 |
| 合规基线（不改代码，只改 G=2） | 86 | 2 | 1 | 43.0 | 4.13 | 47.1s | 44–52 |
| **A**（不砍请求，批 3 rps） | 86 | 3 | 4 | 28.7 | 4.13 | **32.8s** | 31–36 |
| **A**（不砍请求，守 2 rps） | 86 | 2 | 4 | 43.0 | 4.13 | 47.1s | 44–52 |
| **C1**（cid+评论缓存，守 2 rps） | 31 | 2 | 1 | 15.5 | 4.13 | **19.6s** | 18–22 |
| **A+C1**（守 2 rps） | 31 | 2 | 4 | 15.5 | 4.13 | 19.6s（**并发无收益**） | 18–22 |
| **A+C1**（批 3 rps） | 31 | 3 | 4 | 10.3 | 4.13 | **14.5s** | 14–17 |
| **C2**（+搜索缓存，守 2 rps） | 11 | 2 | 1 | 5.5 | 4.13 | 9.6s | 9–13 |
| 冷轮（弹幕缓存全失效） | 81 | 2 | 1 | 40.5 | 4.13 | 44.6s | 42–50 |
| 冷轮 + A（批 3 rps） | 81 | 3 | 4 | 27.0 | 4.13 | 31.1s | 29–34 |

### 5.4 哪些是推测

- ✅ **实测/推算，可直接引用**：43.0s、86/136 请求的完整构成、452ms 边际、4.13s 固定开销、86/86 同域、"限速器从未生效"、2 rps 下 35s 只够 61 请求
- ⚠️ **推算，依赖公式**：各方案耗时（RTT 与 C 在并发下可能变化——并发时 TLS 握手并行，单请求 RTT 可能上升 10–20%，故区间已放宽）
- ❓ **纯推测，WP3 必须压测确认**：缓存命中率（评论/搜索）、抖动对 P95 的影响、并发下 `-352` 概率变化、SQLite 事务合并后的实际 C 值、HTTP keep-alive 若将来引入能省多少（估计 100–150ms/请求，但不在本次范围）

> **Amdahl 上限**：即使网络降到 0，一轮也要 **4.13s**。所以 9.6s（C2）已逼近该架构的舒适下限；若还要更快，唯一出路是把 extract/judge 移出主路径——**而那是 WP4 的范畴，不是并发能解决的**。

---

## 6. 风控与合规

### 6.1 现状已经越线（必须记录）

| 项 | 文档要求 | 实际 | 判定 |
|---|---|---|---|
| 请求频率 | `docs/02:95`「限速 **2 rps**」 | `config.py:17` `MAX_RPS=3`；实测有效 **2.21 rps** | ⚠️ **配置超文档、实测超配置附近** |
| 轮间隔 | `docs/02:100`「10 分钟低频」 | `HOTMEME_INTERVAL=600` | ✅ 一致 |
| 登录/身份 | 不登录、不存用户身份 | 无 cookie 持久化，每轮新 buvid | ✅ 一致 |

> 这 43.0s 恰好「卡在」2.21 rps，**是巧合而非设计**——一旦网络变快（RTT 降到 250ms），串行吞吐会升到 4 rps，**悄无声息地翻倍**。这是比"慢"更严重的合规缺陷：**当前架构没有任何东西在真正约束频率**。修好 M1/M2 之后，限速器才第一次真正生效，而它生效的**第一个后果是变慢到 47.1s**。这一点必须在决策时讲清楚，否则会被误判为"改造把系统搞慢了"。

### 6.2 `-352` / `412` 的机制与分桶边界

| 码 | 含义 | 现有处理 | 并发下的变化 |
|---|---|---|---|
| `-352` | 风控校验失败（buvid/wbi 不合法或请求过快） | `bili.py:213-215` 强制 re-bootstrap 后重试 1 次 | ⚠️ 并发时 N 个线程同时 re-bootstrap → 更多请求、更易连环触发。**必须单飞（single-flight）** |
| `412` | UA/TLS 指纹被拒 | `http.py:141-145` 转 curl 兜底 | ⚠️ curl 兜底本身未被限速（`http.py:165` 顺序错误）→ **越限时反而更快，正反馈** |
| `429` | 显式限流 | 未处理 | 新增：暂停域桶 60s |

**分桶提 rps 的边界**（明确写死，防止后人乱调）：

1. **域级 ≤ 2.0 rps**（`api.bilibili.com`）。这是唯一有意义的合规闸，**任何端点桶都不允许把域总量推高到 2 以上**。
2. 端点桶只能 **≤ 域桶**，作用是「分配」而非「扩容」。
3. 总请求数上限：`N ≤ (35 − C) × G`。G=2 时 **N ≤ 61**，写进 CI 断言。
4. 提额的前提（三者同时满足才可考虑 2.5–3.0）：① 连续 3 天 `412=0`、`-352 ≤ 1/天`；② P95 < 800ms；③ 有实时回滚开关。
5. **不允许**为了让某个方案"显得快"而调高 G——§3 已证明那是在买风险，不是买性能。

### 6.3 监控指标（当前零覆盖，全部需新增）

`stats_json` 现有字段只有 `requests / elapsed / errors`（`pipeline.py:402-404`），**没有分端点计数、没有延迟分布、没有 412 计数**。

| 指标 | 采集点 | 告警阈值 | 用途 |
|---|---|---|---|
| `requests_by_endpoint{}` | `Bucket` 旁挂计数器 | 单项 > 预算 1.5× | 定位预算被谁吃掉 |
| `p50/p95/p99 latency` | `HttpClient.get` 包裹 `time.monotonic()` | P95 > 1500ms | RTT 漂移，c 需要调 |
| `status_412 / -352 / 429` 计数 | `get()` 的 `HTTPError` 分支 + `_api` | 412 ≥ 1 / 轮 → 降 G 到 2 以下 | **一级风控信号** |
| `cache_hit_rate{}`（danmaku/comment/search） | `plan_docs` | 命中 < 80%（稳态） | 缓存失效检测 |
| `circuit_state{}` | `CircuitBreaker` | 任一 OPEN | 端点故障 |
| `elapsed_breakdown{fetch,cpu,db}` | `pipeline.run_cycle` 打点 | fetch 占比异常 | 验证本设计的收益是否兑现 |
| `T_cycle` P95 | 已有 `elapsed` | > 35s 连续 3 轮 | WP3 验收线 |

### 6.4 回滚开关（三级，秒级生效）

| 级别 | 动作 | 效果 |
|---|---|---|
| L1 | `HOTMEME_COMMENT_CACHE_SECONDS=0`、`HOTMEME_CID_REUSE=0`、`HOTMEME_SEARCH_CACHE_SECONDS=0` | 请求削减全部失效，回到 86 |
| L2 | `HOTMEME_FETCH_CONCURRENCY=1` | 并发退化为串行（M1/M2 的锁在 c=1 下无行为变化） |
| L3 | `HOTMEME_MAX_RPS=1`、`HOTMEME_CIRCUIT_FAILS=0` | 强降速 / 关熔断 |
| L4（紧急） | 反向切回上一个 commit（采集层改动都在 3 个文件内） | 全量回滚 |

**自愈**：连续 2 轮出现 412 或 `-352 ≥ 3` → 自动把域桶降到 1.5 rps 并跳过本轮剩余搜索请求（搜索是 20 个请求里最容易招惹风控的）。

---

## 7. 文件级实施任务单（WP3，7 个 PR 大小任务）

> 全部按「先零风险收益、后有条件并发」排序。**WP3-1/2/3 不碰并发，也不改变任何端点频率之外的行为**，可独立合入上线。

### WP3-1 · 接入评论缓存（`COMMENT_CACHE_SECONDS` 从死配置变活）

- **改动**：`app/store.py` 新增 `comment_cache` 表 + `get_comment_cache/put_comment_cache`；`app/pipeline.py` `collect_docs` 评论循环先查缓存；`app/config.py` 加 `HOTMEME_COMMENT_CACHE_SECONDS`（默认 1800）
- **收益**：−30 请求/轮（86 → 56），`T` 43.0 → 28.5s @2.21 rps
- **验收标准**：① 稳态 `stats.comment_requests == 0` 且 `stats.comments > 0`；② 强制 `=0` 时行为与今天逐字节一致（`comment_requests == 30`）；③ 离线 fixtures 全绿；④ `text_sample` 的 `kind='comment'` 行数不下降
- **测试用例**：`test_comment_cache_hit`（两次 `collect_docs`，第二次 0 请求）、`test_comment_cache_ttl_expired`（mock `_fresh` 返回 False → 重新请求）、`test_comment_cache_disabled`（TTL=0 → 30 请求）

### WP3-2 · cid 复用（修掉每轮 25 个 pagelist）

- **改动**①：`app/store.py` `upsert_videos` 的 `ON CONFLICT` 子句 `cid=excluded.cid` → **`cid=COALESCE(excluded.cid, video.cid)`**（`store.py:186-193`，这是根因）；
- **改动**②：新增 `store.cid_map(conn, bvids) -> dict[str,int]`；
- **改动**③**：`pipeline.collect_docs` 弹幕循环先读 `cid_map`，未命中才 `cid_for()`，拿到后 `store.update_video_cid(conn, bvid, cid)` 回写
- **收益**：−25 请求/轮（86 → 61；与 WP3-1 叠加 → 31）
- **验收标准**：① 第二轮起 `stats.pagelist_requests == 0`；② DB 断言 `SELECT COUNT(*) FROM video WHERE zone='梗搜索' AND cid IS NOT NULL` 从 0 变正数并保持；③ 弹幕条数不下降（`stats.danmaku` 与基线差 ≤1%）；④ `HOTMEME_CID_REUSE=0` 可回退
- **测试用例**：`test_upsert_videos_preserves_cid`（先写有 cid 再写 cid=None，断言 cid 未被清空）、`test_cid_reuse_skips_pagelist`、`test_cid_reuse_disabled`

### WP3-3 · `collect_docs` 三段式重构（为并发铺路，零行为变化）

- **改动**：`app/pipeline.py` 拆 `plan_docs` / `fetch_all`（此 PR 内串行实现 `for t in tasks`）/ `persist`；`store.insert_text_samples` 合并为单事务
- **收益**：0（**结构与 DB 收益**：commit 次数 30+ → 1，预计省 0.3–0.8s）
- **验收标准**：① 离线集成测试输出与改造前**完全一致**（`families/candidates/top` 三个数字 diff 为 0）；② 生产灰度一轮 `elapsed` 不上升 >0.5s；③ `persist()` 内 `threading.current_thread() is main_thread()` 断言通过
- **测试用例**：`test_collect_docs_segments`（plan 只读、fetch 不碰 conn、persist 只写）、`test_persist_single_transaction`（`conn.total_changes` 与 commit 次数断言）、`test_collect_docs_equivalence`（与旧实现快照对比）

### WP3-4 · 限速器改造（M1 + M2，修正确性缺陷）

- **改动**：`app/http.py` `RateLimiter` → `Bucket`（槽位预约 + Lock + jitter）；`requests` 计数加锁；`_curl_get` 的 `wait()` 前移 + 补计数
- **收益**：不直接提速，但**修掉"限速器从未生效"与"curl 越限"两个合规缺陷**（§6.1）；同时让 §5 的公式第一次可预测
- **风险**：修好后**第一轮会变慢到 47.1s**（如果 G=2）或维持 43.0s（G=3）——必须与 WP3-1/2 同批上线，否则会被误判为性能倒退
- **验收标准**：① `HOTMEME_MAX_RPS=2` 下实测 `N/elapsed ≈ 2.0 ± 0.1`；② `requests` 计数在各端点求和 == `stats.requests`（含 curl 兜底）；③ 单测证明 8 线程并发下首尾时间 ≥ `N×interval`（不允许突发）
- **测试用例**：`test_bucket_serializes_threads`（mock monotonic，断言槽位 0,1,2…）、`test_bucket_no_burst_under_8_threads`、`test_request_counter_thread_safe`（8 线程 ×100 次 → 恰好 800）、`test_curl_counts_and_waits`

### WP3-5 · 双层桶 + 配置键（默认行为不变）

- **改动**：`app/http.py` 加 `CompositeLimiter`（域桶 → 端点桶，固定顺序）；`app/bili.py` 各方法把端点名传给 `get()`；`app/config.py` 加 7 个 `HOTMEME_RPS_*`
- **验收标准**：① 缺省配置下 `stats.requests` 与耗时与 WP3-4 完全一致（**桶不改变总 rps**）；② 单端点桶生效可测（把 `RPS_SEARCH=0.2` → 20 个搜索请求 ≥ 100s，或单测断言）；③ 无死锁（两桶顺序固定，加 30s 超时断言）
- **测试用例**：`test_composite_limiter_order`、`test_endpoint_bucket_throttles_only_its_endpoint`、`test_default_config_is_behavior_preserving`

### WP3-6 · 熔断 + 降级 + 指标（不含并发）

- **改动**：新增 `app/fetch.py`（`CircuitBreaker`、`FetchStats`、`fetch_all(serial)`）；`app/pipeline.py` 记录 `requests_by_endpoint` / `latency_p50_p95` / `status_412/352` / `cache_hit_rate` 到 `stats`；`app/bili.py` 弹幕/评论 stale-while-error（`HOTMEME_STALE_MAX`）
- **验收标准**：① 注入 5 次 500 → 端点 OPEN、后续请求 0 次发出、`status='degraded'`；② 冷却后 HALF_OPEN 放行 1 个探针；③ `stats` 新增字段在 `/api/health` 可见；④ 全断网时一轮仍 `done/degraded`，用过期缓存出榜（不 `failed`）
- **测试用例**：`test_circuit_opens_after_5_fails`、`test_circuit_half_open_probe`、`test_stats_breakdown_fields`、`test_stale_cache_fallback`、`test_pipeline_survives_all_endpoints_down`

### WP3-7 · 有界并发接入（默认关闭）+ api.py 单任务队列

- **改动**①：`app/fetch.py` `fetch_all` 加 `ThreadPoolExecutor(max_workers=HOTMEME_FETCH_CONCURRENCY)`，`=1` 时走原串行路径；`app/bili.py` 加 `warmup()`（spi + nav 预热）并在 fan-out 前调用；
- **改动**②：`app/api.py` 引入 `RUN_LOCK` + `asyncio.Queue(maxsize=1)` + 单个 `_worker()`，`/api/refresh` 改 `put_nowait` → 202/409（§4.6）
- **验收标准**：① `c=1` 与 `c=4` 的 `families/candidates/top` 完全一致（并发不改语义）；② `c=4` 且 G=3 时 `elapsed ≤ 36s`、`-352 == 0`、`errors == 0`；③ 并发下 `requests` 计数精确（无丢失）；④ **`POST /api/refresh` 连打 5 次只产生 1 轮**（409×4）；⑤ 调度器与手动刷新不并发（`RUN_LOCK` 断言）；⑥ 全程单写者（`persist()` 断言不触发）
- **测试用例**：`test_concurrency_equivalence`（c=1 vs c=4 结果一致）、`test_warmup_before_fanout`（warmup 后 1 次 spi + 1 次 nav，不随 c 增长）、`test_single_flight_bootstrap`（10 线程首请求 → 1 次 spi）、`test_refresh_409_on_second_call`、`test_refresh_queue_dedup`、`test_scheduler_and_manual_not_concurrent`

**合计**：7 个 PR。WP3-1/2 是纯收益（−55 请求），WP3-3/4/5/6 是地基与可观测性，WP3-7 才是并发（且默认关）。

> **与 WP3 验收线的关系**：PM 给 WP3 的验收是「轮耗时 ≤35s、0 新增错误、测试全绿」。**只做 WP3-1 + WP3-2 即可达成**（31 请求 → 19.6s @2rps，无并发、无新增错误面）。WP3-7 不是达标的必要条件，它是「冷轮削峰」的保险。

---

## 8. 风险清单

| # | 风险 | 等级 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|---|---|
| R1 | **修好限速器后轮耗时上涨**（43.0 → 47.1s @2rps） | 🔴 高 | WP3-4 单独上线 | 被误判为性能倒退，回滚掉正确的修复 | WP3-1/2 先合入并上线；限速修复与请求削减同批 |
| R2 | 并发下 `-352` 连环触发 | 🔴 高 | c>1 + 共享 CookieJar 语义竞态（§2 #2） | 大量 `[]`、`degraded`、请求数反弹 | `warmup()` 预热 + single-flight bootstrap + 熔断 + `c` 默认 1 |
| R3 | 限速改造写错 → **突发超速** | 🔴 高 | 睡眠在锁内 / 槽位未预约 | 比"慢"严重得多：可能直接被封 | 单测强约束（8 线程首尾 ≥ N×interval）；`_curl_get` 顺序修正；灰度期 G=1.5 |
| R4 | 评论缓存 TTL 过长 → 语料陈旧 | 🟡 中 | TTL 1800s、轮间隔 600s | 每 3 轮才刷一次评论（占语料 0.54%） | 可接受；若 WP4 发现评论信号权重上调，则 TTL 降到 600s |
| R5 | 搜索缓存冻结候选池 | 🟡 中 | C2 的 TTL=1200s，池 42% 被冻结 | 「新面孔」判定变钝，WP4 的常青词问题恶化 | C2 设为 P1 可选，默认**关闭**；或保留 2 个高频 query 不缓存 |
| R6 | `cid` 回写与 `COALESCE` 改动的迁移风险 | 🟡 中 | 已有 380 行历史数据 | 老行 cid 保持 NULL（不回填），只对新轮生效 | 不回填；断言只针对新写入行 |
| R7 | 线程池 + 主线程 conn 边界被后人打破 | 🟡 中 | 有人在 worker 里读 DB | 偶发 `database is locked`，难复现 | `persist()` 断言（`HOTMEME_DEBUG_WRITER`）+ code review 清单 |
| R8 | SQLite 事务合并后写放大到内存 | 🟢 低 | 单事务插入 30×~3 条 | 内存峰值 +几十 KB | 可忽略；分批 200 条 |
| R9 | `api.py` 队列容量 1 → 手动刷新被静默丢弃 | 🟢 低 | 调度器 tick 刚入队 | 用户点刷新拿 409，体验略降 | 409 响应体带 `retry_after`；看板提示「已有一轮在跑」 |
| R10 | 本设计的时间估算偏乐观 | 🟡 中 | 并发下 RTT 上升 10–20% | 实际 32.8 → 35–39s | 区间已放宽；WP3-7 必须真机压测 3 轮取 P95 再定 c |
| R11 | 冷轮（弹幕缓存全失效）仍破 35s | 🟡 中 | 新视频多 / 缓存过期 | 81 请求 → 44.6s @2rps | WP3-7 削峰（31.1s）；或把 `CACHE_TTL_SECONDS` 从 24h 提到 48h（弹幕内容变化慢） |
| R12 | 监控字段膨胀 `stats_json` | 🟢 低 | 每轮写入所有端点/分位 | DB 体积增长 | 只存聚合值（p50/p95/各端点计数），不存逐请求 |

---

## 9. 附录：证据与复现命令

```bash
# 1) 两轮实测（本报告 §1.1 / §1.4 的数据源）
sqlite3 "file:data/hotmeme.db?mode=ro" \
 "SELECT id,status,elapsed,stats_json FROM (SELECT id,status,stats_json,json_extract(stats_json,'$.elapsed') elapsed FROM cycle) ORDER BY id DESC LIMIT 3;"

# 2) cid 覆盖（§3.4 C1a 的根因证据）
sqlite3 "file:data/hotmeme.db?mode=ro" \
 "SELECT zone,COUNT(*) n,COUNT(cid) with_cid FROM video GROUP BY zone ORDER BY n DESC LIMIT 3;"
# => 梗搜索|129|0   其余分区 with_cid == n（数字会随主线每轮写入漂移 ±1）

# 3) 评论语料占比（§3.4 C1b 的代价评估）
sqlite3 "file:data/hotmeme.db?mode=ro" \
 "SELECT kind,COUNT(*) FROM text_sample GROUP BY kind;"
# => comment|90

# 4) 死配置（§3.4 C1b）
grep -rn "COMMENT_CACHE_SECONDS" --include=*.py .
# => 仅 config.py:77 定义，零引用

# 5) CookieJar 线程安全（§2 #2）
.venv/bin/python -c "import http.cookiejar,inspect;print('_cookies_lock' in inspect.getsource(http.cookiejar.CookieJar))"

# 6) 耗时公式复算（§5.3）
.venv/bin/python -c "
m=(65.6-43.0)/(136-86); C=43.0-86*m
print(f'marginal={m:.3f}s  rps={1/m:.2f}  fixed={C:.2f}s')
print('A  86/3+%.2f = %.1f' % (C, 86/3+C))
print('C1 31/2+%.2f = %.1f' % (C, 31/2+C))"
```

**只读声明**：本会话仅创建 `docs/pm/ASYNC-DESIGN.md`；未修改任何 `app/*.py`、`tests/*`、`data/*`、`README.md`；未联网、未启服务、未跑真机采集、未装依赖、未 commit。
