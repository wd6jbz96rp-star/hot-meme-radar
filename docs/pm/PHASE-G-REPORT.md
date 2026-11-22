# Phase G 交付报告：部署与切换（影子运行中，未切流）

> 日期：2026-11-22  
> 状态：**部署完成；影子验证通过；切流待 OK 轮（PM 决策：先影子跑，不在夜间强行切流）**  
> 原则：不改 Nginx、不影响同机 80/8000/8080 服务、不 commit/push、不打印密码/token。

## 0. 结论摘要

- Phase G 代码与部署文件全部完成；服务器已部署、旧 worker 保留、Admin/备份上线。
- 新 state worker 以 **systemd timer 白天 08–23 点每 15 分钟影子运行**（夜间暂停，避免与旧 worker 重复请求）：报告写 `var/meme_info_v2_shadow.json`，
  不覆盖生产；DB 正常积累 `round`/`video_metric`。
- **未切流**：夜间 Phase C 采样量低于 degraded 阈值（250/72、14/25、0/23 vs 阈值 600/300），
  round/report `degraded` → Phase D 按设计冻结新 family → `pool1/pool2=0`。
  此时切流会让公开页"候选池"tab 从有内容变空（默认"发布池"tab 在 v1 下本就为 0），属可见回退。
- 已提供 `deploy/cutover.sh`（默认不执行）：仅当影子报告 `status=ok` 且 pool1/pool2 非空、
  schema v2、app 200 时才切流；`--check-only` 已实测能正确拦截 degraded 报告、放行 ok 报告。
- 生产仍是旧 worker 写的 v1 报告，公网 200 正常；回滚路径与 DB 备份完整。

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `tools/state_worker.py` | 单轮编排：B→C→D→E（`MEME_INFO_VERIFY=1` 时 +F）；`fcntl.flock` 单写者锁；报告原子写 + degraded 备份 `.last_good.json`；`--once/--dry-run/--no-db/--json` CLI |
| `tests/test_state_worker.py` | 23 个 mock/内存测试：B→C→D→E 顺序、degraded 备份、失败保留旧报告、锁重叠跳过、dry-run 事务回滚、审核开关、CLI/JSON、密码脱敏 |
| `deploy/hotmeme-state-worker.service` | `Type=oneshot`、`User=ubuntu`、`WorkingDirectory=/home/ubuntu/hotmeme`、`EnvironmentFile=/etc/hotmeme/env`、`ExecStart=.venv/bin/python -m tools.state_worker --once`、`Nice=5`、`TimeoutStartSec=900`、`Restart=no` |
| `deploy/hotmeme-state-worker.timer` | `OnCalendar=*:0/15`、`AccuracySec=1min`、`Persistent=false` |
| `deploy/hotmeme-admin.service` | `python -m admin.app --host 127.0.0.1 --port 8091`、`Restart=always`；无 token 时 admin.app 拒绝启动（exit 2） |
| `deploy/hotmeme-backup.service` / `.timer` | 每日 03:20 跑 `deploy/backup.sh`（`pg_dump -Fc`，保留 7 天） |
| `deploy/backup.sh` | 输出 `/home/ubuntu/backups/hotmeme-YYYYMMDD.dump`（先 `.tmp` 后 mv，失败不留半成品），日志 `var/backup.log` |
| `deploy/cutover.sh` | 切流脚本（默认不执行；`--check-only` 只校验）；路径/URL 可用 env 覆盖便于测试 |
| `deploy/cutover-watch.sh` | **自动切流看守**：每 5 分钟检查；`flock` 防重叠；预检通过才切流；切流失败**自动回滚**（恢复旧报告/旧 worker、停新 timer/watcher） |
| `deploy/hotmeme-cutover-watch.service` + `.timer` | watcher oneshot，`OnCalendar=*:0/5`、`Persistent=false`（root，需 systemctl） |
| `deploy/shadow-dropin.conf` | 影子模式 systemd drop-in（当前已装：报告写 `var/meme_info_v2_shadow.json`） |
| `deploy/README.md` | 部署 / 影子 / 切流 / 回滚 / 运维说明（按当前实际状态更新） |
| `docs/pm/PHASE-G-REPORT.md` | 本报告 |

## 2. 服务器部署步骤（已完成）

1. **先备份**：`/home/ubuntu/hotmeme` → `/home/ubuntu/hotmeme.bak-phase-g-20261122-004044`（移除 .venv）；
   当前报告 → `var/meme_info_latest.json.before-phase-g`（v1，1.5MB）。
2. **上传代码**：macOS 自带 rsync 与服务器 rsync 3.2.7 协议不兼容（buffer overflow），改用
   `tar over ssh`；排除 `.git/.venv/var/data/archive/.archify/__pycache__`，**未覆盖** `var/`、
   `/etc/hotmeme/env`；清理 macOS `._*` 元数据文件；tests 一并上传。
3. **依赖**：`.venv/bin/pip install -r requirements.txt -r requirements-dev.txt`
   （psycopg 3.3.9 / pytest 9.2.0 / httpx 已装）。
4. **schema**：`python -m tools.migrate_state check` → `schema_version=2`、9 表齐全、`ok=true`（未再迁移）。
5. **服务器测试**（真实 Postgres，DB 测试用事务回滚）：
   `test_state / test_video_route / test_window / test_pools / test_report / test_audit / test_admin / test_state_worker`
   → **218 passed**；8 张业务表测试前后行数均为 **0**（无污染）。
6. **Admin token**：`openssl rand -hex 32` → `/etc/hotmeme/admin_token`（600 root）；
   `HOTMEME_ADMIN_TOKEN` 追加进 `/etc/hotmeme/env`（600 root），全程未打印。
7. **systemd units**：5 个 unit 安装、`daemon-reload`；旧 worker unit/脚本**未删除**。
8. **app 重启**：加载 Phase E/F 新代码（v1/v2 双适配），生产仍读 v1 报告，`/api/meme-info` 200。

## 3. 验证记录

### 3.1 手工单轮（2026-11-22 00:42–00:49，round 330；旧 worker 暂停）

- B：`status=ok round_id=330 candidates=300 selected=45/10`（约 3.6 分钟）
- C：`status=degraded comments=250 danmaku=72`（预算 242/700 请求、无接口错误）
- D：`status=degraded pool1=0 pool2=0 transitions=0`（degraded 冻结新 family：`degraded_new_skipped`）
- E：报告 v2 写入（1338 bytes，`status=degraded`）；旧 v1 报告备份为 `.last_good.json`
- 单轮耗时 **430.8s**（< `TimeoutStartSec=900`）
- DB：`round` 1 行（id=330）、`video_metric` 300 行（45 selected_comment + 10 selected_danmaku）
- 报告/API：`schema_version=meme-info/v2`、`report_status=degraded`、counts 全 0；公开页 200
- **风险结论（修正口径）**：旧 v1 报告下 `only=publishable`（默认"发布池"tab）本就为 0
  （v1 publishable 需 `evidence_rate_2x2` 或 LLM 通过，`MEME_INFO_VERIFY=0` 时 no）；但
  `only=all`（"候选池"tab）有 10 条。若此时切到全空 v2，"候选池"tab 也会变空（可见回退）
  → 按"不强行破坏线上"恢复旧 v1 报告 + 旧 worker，改走影子模式。

### 3.2 影子模式（timer 驱动，旧 worker 继续写生产 v1）

- drop-in：`/etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf`
  `Environment=HOTMEME_MEME_INFO_REPORT=/home/ubuntu/hotmeme/var/meme_info_v2_shadow.json`
- 旧 `hotmeme-worker` 保持 active；生产报告 `var/meme_info_latest.json` 未被影子覆盖
  （影子运行前后生产报告 mtime 均由旧 worker 更新：00:53、01:06、01:19）。
- 影子轮次：

| 轮次 | round | window | Phase C | Phase D | v2 影子报告 | 耗时 |
|---|---|---|---|---|---|---|
| 手工（00:42） | 330 | 00:26:59–00:41:59 | comments=250 / danmaku=72 | degraded，pool1=0 | degraded，counts 0，1338B | 430.8s |
| timer #1（01:00） | 331 | 00:41:59–01:00:02 | comments=14 / danmaku=25 | degraded，pool1=0 | degraded，counts 0，1335B | 404.0s |
| timer #2（01:15） | 332 | 01:00:02–01:15:01 | comments=0 / danmaku=23 | degraded，pool1=0 | degraded，counts 0，1332B（last_good 已备份） | 413.1s |

- timer 触发、单写者锁、原子写、degraded 备份均实际生效（`Deactivated successfully`，exit 0）。
- **B站评论接口夜间不稳定（非本阶段引入）**：旧 worker 自身历史即有 comments=0 的轮次
  （23:59 / 00:09 / 00:22 均 0；00:35 轮 885；01:03 轮 2），`meme_info_shadow` 的已知问题
  §1.3「评论接口偶发大量失败」在夜间更明显；影子轮次 `comment_stop` 多为 `empty_page`。
  建议长影子期如发现互相影响，可临时 `systemctl disable --now hotmeme-state-worker.timer`
  只保留旧 worker，待白天再开。
- `deploy/cutover.sh --check-only` 实测（真实影子报告）：正确拦截
  `FAIL: status='degraded' 不是 ok; pool1 为空; pool2 为空`（exit 1）。

### 3.3 自动切流 watcher（PM 决策 A：等白天 ok 非空轮）

`hotmeme-cutover-watch.timer`（每 5 分钟）→ `cutover-watch.sh`：`flock` 防重叠 →
生产报告已 v2/ok 则停用自身 → 先 `cutover.sh --check-only`（不过则记 wait 行 exit 0）→
通过才 `cutover.sh`；失败自动回滚并停用自身（防对陈旧影子报告反复重试），写
`var/cutover_watch.failed`。日志 `var/cutover_watch.log`（不含 token/密码）。

验证（/tmp 沙盒 + 假 cutover/假 systemctl，**未触碰生产报告**）：

| 场景 | 结果 |
|---|---|
| 正路径：假影子 ok+非空、假生产 v1、假 cutover 成功 | exit 0；生产报告变 v2/ok；日志 `ready`+`success`（含时间/报告/回滚点）；调用 `systemctl disable --now hotmeme-cutover-watch.timer` |
| 失败回滚：假 cutover 写坏生产报告后 exit 1 | exit 1；生产报告从 backup **恢复为旧 v1**；systemctl 调用顺序 = `disable state-worker.timer` → `stop --no-block state-worker.service` → `enable --now hotmeme-worker` → `disable cutover-watch.timer`；`var/cutover_watch.failed` 写入失败时间/rc/回滚点；日志含 cutover 输出尾部 |
| 已切流：生产报告已是 v2/ok | exit 0；不调用 cutover；停用 watcher timer |
| 真实负路径（当前 degraded 影子报告，手工触发 systemd service） | `Result=success`、exit 0；日志 `wait: cutover --check-only 未通过（...）；继续等待`；生产报告 md5/mtime **不变**；旧 worker/影子 timer/watcher timer 全部仍 active；公网 200 |
| 真实 timer 触发（01:30:01） | watcher `LAST=01:30:01`、`Result=success`、exit 0；日志含具体原因 `schema=meme-info/v2 status=degraded pool1=0 pool2=0`；生产报告 md5 `250b5e…`/mtime 01:19 **不变**；同一时刻 state worker round 4 正常开始（影子继续跑） |

### 3.3.1 切流竞态修复（PM 复核）

问题：`cutover.sh` 原流程只停旧 worker，未停 `hotmeme-state-worker.service/timer`；watcher 每 5 分钟
可能在 state worker 正写影子报告时执行 cutover，复制到写了一半/旧报告，或切流后被影子轮覆盖。

修复（`deploy/cutover.sh` / `deploy/cutover-watch.sh` / `deploy/README.md`，未 commit）：

- `cutover.sh` 新流程：停旧 worker → **`disable --now state-worker.timer` + `stop service` 并等待退出
  （60s 超时 SIGKILL）** → **冻结后再次校验影子报告**（失败→安全中止 rc=3：恢复旧 worker +
  shadow timer，生产报告不动，watcher 继续等待）→ 移除 drop-in → 复制报告 → 启 timer/触发 →
  复核。这样 watcher 预检通过后即使刚到 15 分钟 timer 点、或新一轮把影子报告覆盖成 degraded，
  最终复制的一定是"冻结且复核通过"的那一份。
- `cutover-watch.sh`：失败回滚改为 **blocking stop + 等待/SIGKILL**（不再 `stop --no-block`），
  确保 state worker 真正停止后才恢复旧报告（防止它继续写生产路径）；新增 rc=3 安全中止处理
  （只记 abort、继续等待，不触发回滚）；成功/安全中止/失败三态分明。

验证（/tmp 沙盒 + 假 systemctl/cutover，不触碰生产）：

| 场景 | 结果 |
|---|---|
| cutover 成功路径（state service 初始 active） | rc=0；systemctl 顺序 = `disable hotmeme-worker` → `disable state-worker.timer` → `stop state-worker.service` → `daemon-reload`（此时生产报告仍是 v1）→ 复制后变 v2 → `enable state-worker.timer` → `start service`；drop-in 被移除 |
| 停 worker 后影子变 degraded → 安全中止 | rc=3；生产报告保持 v1、drop-in 保留；systemctl 调用 `enable --now hotmeme-worker` + `enable --now state-worker.timer`；未复制报告 |
| watcher 失败自动回滚（state service 在跑） | rc=1；日志 `state-worker.service 已停止`；顺序 = `disable state.timer` → `stop service` → 恢复旧报告 → `enable hotmeme-worker` → `disable watcher.timer`；生产报告恢复 v1；failure marker 写入 |
| watcher 成功 / 安全中止 rc=3 | 成功 rc=0（生产变 v2、停 watcher timer）；rc=3 → rc=0 且日志 `abort: cutover 安全中止…继续等待`，无回滚调用、生产保持 v1 |
| 真实负路径（当前 degraded 影子报告，手工触发 watcher） | `Result=success`、exit 0；日志含具体原因 `schema=… status=degraded pool1=0 pool2=0`；生产报告 md5 **不变**；所有服务/timer 保持 active；公网 200 |

### 3.4 生产保护 / 恢复动作

- 影子 v2 报告证据保留：`var/meme_info_v2_round330.json`。
- 生产报告已恢复：`var/meme_info_latest.json` ← `var/meme_info_latest.json.before-phase-g`，
  随后旧 worker 继续正常刷新（最新 01:19:13，1.4MB，20 memes）。
- 旧 worker：active（`run ok 171s`），`/api/meme-info` schema=v1、`count=1`、公网 200。

## 4. Admin / 备份验证

- `hotmeme-admin`：**active + enabled**，只监听 `127.0.0.1:8091`（`ss` 确认），未暴露公网。
  `curl -H "X-Admin-Token: $(sudo cat /etc/hotmeme/admin_token)" 127.0.0.1:8091/admin/health`
  → `{"ok":true,"auth_configured":true,"db":true,"schema_version":2,"llm_enabled":false}`（token 未打印）
  `/admin/` 带 token 200、不带 401；`/admin/queue` 200（队列 JSON，当前 pool 空）。
- `hotmeme-backup.service` 手工跑一次：`Result=success`、`ExecMainStatus=0`；
  产出 `/home/ubuntu/backups/hotmeme-20261122.dump`（600 ubuntu）；跑完 3 轮后在 01:22 重跑，
  备份含 round/video_metric 数据，size=104K；日志 `var/backup.log` 有 begin/ok/retention 三行。
- 保留期逻辑实测（/tmp 假目录）：10 天前、8 天前 fake dump 被删除，3 天前保留，新 dump 生成；
  `keep_days=7` 生效。
- `hotmeme-backup.timer`：**active + enabled**，下次 03:20。

## 5. 当前服务状态（服务器，01:22 快照）

| 单元 | 状态 | 备注 |
|---|---|---|
| `hotmeme-app` | active / enabled | 127.0.0.1:8090；读生产 v1；已加载新代码 |
| `hotmeme-worker`（旧） | active / enabled | 写生产 v1；**切流时才 disable，unit/脚本保留** |
| `hotmeme-state-worker.timer` | active / enabled | 每 15 分钟；**影子模式**；下次 01:30 |
| `hotmeme-admin` | active / enabled | 127.0.0.1:8091 |
| `hotmeme-backup.timer` | active / enabled | 每日 03:20 |
| `hotmeme-cutover-watch.timer` | active / enabled | 每 5 分钟自动切流看守；条件不满足只记 wait |
| Nginx / 80 / 8000 / 8080 | 未改动 | `/hotmeme/` → 127.0.0.1:8090；nginx snippet 保持 2026-11-16 原文（md5 `8dde3ea0…`） |
| 8091 | 仅本机监听 | 不暴露公网 |

DB（01:22）：`round`=3、`video_metric`=900、`pool1`=0、`pool2`=0、`family_round_stat`=0、`audit_event`=0。
生产报告：`meme_info_latest.json`（v1，01:19:13，1.4MB）；影子报告：`meme_info_v2_shadow.json`（v2 degraded）。

## 6. 切流前置条件与步骤（已准备，未执行）

前置（`cutover.sh` 内强校验，缺一不可）：

1. 影子报告 `schema_version=meme-info/v2`；
2. 影子报告 `status=ok`；
3. 影子报告 `pool1` 与 `pool2` 均非空；
4. 本机 API 与公网 `/hotmeme/` 均 200。

执行（已按 PM 决策 A 自动化，无需人工守夜）：

```bash
# 自动：hotmeme-cutover-watch.timer 每 5 分钟检查，条件满足自动切流，失败自动回滚
tail -f /home/ubuntu/hotmeme/var/cutover_watch.log
# 手工（排障/验证）：
cd /home/ubuntu/hotmeme
deploy/cutover.sh --check-only   # 先只校验（已实测：degraded 报告会被拦截）
deploy/cutover.sh                # 通过后切流
```

`cutover.sh`：停旧 worker（disable，unit 保留）→ 移除影子 drop-in + daemon-reload →
影子报告原子替换生产报告（同目录 tmp + mv）→ 启用新 timer + 手工触发一轮 → 复核报告/API/公网 200。

回滚：停新 timer（`disable --now`）→ 恢复影子 drop-in（可选）→ `enable --now hotmeme-worker` →
用 `.before-phase-g` / `.last_good.json` 覆盖报告；DB 表保留不影响旧链路。详见 `deploy/README.md` §5。

## 7. 限制与 PM 决策点

0. **PM 决策 A（已确认）**：等白天出现 `status=ok` 且 pool1/pool2 非空的影子轮再切流；
   已由 `hotmeme-cutover-watch` 自动看守（每 5 分钟），失败自动回滚旧报告/旧 worker。
1. **Phase C 夜间采样量低于 degraded 阈值（关键阻塞）**：00:26–00:41 轮 comments=250 / danmaku=72
   （阈值 600/300），01:00/01:15 两轮更低（14/25、0/23，多为评论 `empty_page`；旧 worker 夜间同样
   出现 comments=0，属 §1.3 已知接口不稳定 + 夜间低活跃叠加）。degraded 轮 Phase D 按设计冻结新
   family（`degraded_new_skipped`），pool1/pool2 无法冷启动。需要 PM 决策：a) 只在活跃时段做冷启动
   /观察 OK 轮；b) 按夜间低活跃调低阈值或加时段系数；c) 冷启动允许 degraded 轮新 family 进 pool1
   （改 Phase D 语义）。**Phase G 未擅自改业务阈值。**
2. **冷启动空窗**：即使白天出现 OK 轮，pool2 需 score≥65 + usage>0，可能需数轮才有非空新梗榜；
   `cutover.sh` 已要求 pool1/pool2 均非空，避免切流后空榜。
3. **旧 v1 / 新 v2 内容不互通**：切流瞬间"候选池"tab 内容来自 v2 pool1（旧 v1 memes 不再展示）；
   "发布池"tab 在 v1 下本就为 0（LLM 暂停），切流后取决于 v2 pool2。
4. **timer 边界**：单轮实测 404–431s，<900s；若某轮超时被 systemd 杀，下一轮窗口 >20min 时
   Phase C 会 clamp 并标 degraded（已有机制）。
5. **rsync 兼容性**：macOS 自带 rsync 2.6.9 与服务器 rsync 3.2.7 不兼容，部署用 tar over ssh（已写入 README）。

## 8. 验收对照

| 验收项 | 结果 |
|---|---|
| timer active、admin active、backup timer active | ✅（全部 active+enabled） |
| 旧 worker disabled 但 unit 保留 | ⏸ 影子期旧 worker **保持 active**（写生产 v1）；unit/脚本保留，切流时 disable |
| 新报告 v2 生产路径 + 页面 200 | ⏸ v2 影子路径已产出（degraded）；生产仍 v1，页面/API 200 |
| 至少一轮 round/video_metric/pool1 写入 | ✅ round=3、video_metric=900；⚠️ pool1=0 因 degraded 冻结（见 §7.1） |
| Admin health 200（本机/隧道） | ✅ health `ok:true`、db true、schema 2 |
| pg_dump 文件存在且 7 天保留逻辑 | ✅ dump 存在（600）；/tmp 实测保留期正确 |
| 现有 80/8000/8090 其他服务不受影响 | ✅ Nginx snippet 未改；app/8000 正常；8080 与本阶段前一致 |
| 服务器测试通过（无回归） | ✅ 218 passed，业务表 0 残留 |
| 不 commit/push；不打印密码/token；不改 Nginx/其他服务 | ✅ 工作区改动未提交；token 仅文件写入 |

## 9. 下一步（交给 PM / 主线程）

1. **交由 watcher 自动等待并切流**：`var/cutover_watch.log` 会记录 `wait` →
   `ready` → `success`（或 `rollback`）；切流成功后 watcher 自动停用自身 timer，
   成功日志内含切换时间/生产报告路径/回滚点（`.before-phase-g`、`.last_good.json`）。
   人工复核：`deploy/cutover.sh --check-only`（此时应显示 v2/ok 且 pool 非空）。
2. 若 PM 同意调整 Phase C 夜间阈值或冷启动策略，改 `meme_info/window.py` / Phase D 语义后
   重新跑一轮手工验证（`sudo systemctl stop hotmeme-state-worker.timer`，
   `sudo systemctl start hotmeme-state-worker.service`）。
3. 影子期如需降负载：`sudo systemctl disable --now hotmeme-state-worker.timer`（旧 worker 不受影响，
   随时可 `enable --now` 恢复影子）。
4. 切流后记得验证：报告 v2 `status=ok`、pool2 非空、`/api/meme-info?only=publishable|all` 200、
   公网 200、Admin health、下一轮 timer 正常；旧 worker 已 disable。

## 6. 冷启动第一轮实测（2026-11-22 02:00）

- 手工触发 state worker 一轮（round 342，夜间）：
  - Phase B ok：300 候选，45/10 选片
  - Phase C degraded：comments=5、danmaku=18
  - Phase D **cold_start 例外生效**：pool1=8（5 provisional/3 out），pool2=0
  - 报告 v2 写入影子路径，status=degraded；生产 v1 未动
- 发现冷启动 provisional 里出现用户确认 ❌ 的 `非凡之爱`：
  - 已把 `非凡之爱`/`最怕BLG夺冠之人`/`腋毛攻击` 加入 `knowledge.REVIEW_STOPWORDS`；
  - 修复 stopword 归一化匹配（BLG 大写漏拦）；
  - 清理影子 DB 中这些负例的 pool/stat/alias/transition 行；pool1 8→7，pool2 仍 0。
- 结论：第一轮可以跑通、能建立 pool1；但夜间样本不足以产生 pool2，仍不能切流。


## 7. 2026-11-22 10:00 阻塞更新

- 白天仍 comments=0–14：B站 `/x/v2/reply/main` 对云服务器 IP 返回 412；legacy fallback 只能拿每视频约 3 条旧评论，45 视频 <135，远低于 600。
- 影子仍 degraded、pool2=0，watcher 继续 wait；生产 v1 正常。
- 解除阻塞需要：B站登录态 cookie（SESSDATA）或更换评论源/评论降级策略。
