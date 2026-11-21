# 云服务器部署（Ubuntu 24.04）— Phase G

- 代码：`/home/ubuntu/hotmeme`
- 公开只读 app：systemd `hotmeme-app`，`127.0.0.1:8090`，Nginx `/hotmeme/` 反代（**本阶段不改 Nginx**）
- **新** state worker：systemd `hotmeme-state-worker.service`（oneshot）+ `hotmeme-state-worker.timer`（每 15 分钟）
- **新** 审核 Admin：systemd `hotmeme-admin`，只监听 `127.0.0.1:8091`，SSH 隧道访问
- **新** 每日备份：systemd `hotmeme-backup.service` + `hotmeme-backup.timer`（03:20）
- Postgres 16：`127.0.0.1:5432`，`schema_version=2`；连接串在 `/etc/hotmeme/env`（600 root）
- 报告：`/home/ubuntu/hotmeme/var/meme_info_latest.json`
  - 当前生产：**v1**（旧 `hotmeme-worker` 写，Phase 0 管线）
  - 影子：`var/meme_info_v2_shadow.json`（新 state worker 写 v2，**不覆盖生产**）
  - 上一份 ok：`var/meme_info_latest.last_good.json`（degraded 轮覆盖前自动备份）
- 旧 worker：systemd `hotmeme-worker`（`tools/meme_info_worker.sh`，报告 v1）——切流前保持运行，
  切流时 `disable` 但**保留 unit 和脚本**作为回滚路径
- 公网：`http://124.220.189.74/hotmeme/`
- 同机 80 根路径 / 8000 / 8080 其他服务**不受影响**

## 0. 当前部署状态（2026-11-22 00:52）

| 服务 | 状态 | 说明 |
|---|---|---|
| `hotmeme-app` | active | 已加载 Phase E/F 新代码（v1/v2 双适配），生产仍读 v1 报告 |
| `hotmeme-worker` | active | 旧 Phase 0 管线，写生产 v1 报告（回滚路径保留） |
| `hotmeme-state-worker.timer` | active | 每 15 分钟；**影子模式**：报告写 `meme_info_v2_shadow.json` |
| `hotmeme-admin` | active | 127.0.0.1:8091，`/etc/hotmeme/env` 带 `HOTMEME_ADMIN_TOKEN` |
| `hotmeme-backup.timer` | active | 每日 03:20；手工已跑过一次，dump 正常 |
| `hotmeme-cutover-watch.timer` | active | 每 5 分钟检查影子报告；条件满足则自动 `cutover.sh`，失败自动回滚 |
| Nginx / 80 / 8000 / 8080 | 未改动 | `/hotmeme/` 仍反代 127.0.0.1:8090 |

**未切流**：冷启动期 Phase C 夜间采样量低于 degraded 阈值（如 2026-11-22 00:26–00:41 轮
comments=250/danmaku=72，阈值 600/300）→ round/report `degraded` → Phase D 冻结新 family，
pool1/pool2 为空。此时切流会让公开页新梗榜变空，因此采用影子运行，等出现 `status=ok` 且
pool1/pool2 非空的影子轮次后再执行 `deploy/cutover.sh`（见 §4 / §5）。

## 1. 文件清单（本阶段新增/修改）

| 文件 | 作用 |
|---|---|
| `tools/state_worker.py` | 单轮编排 B→C→D→E（`MEME_INFO_VERIFY=1` 时 +F）；文件锁防重叠；degraded 备份 last_good |
| `tests/test_state_worker.py` | 编排器 mock/内存测试（不跑 B 站、不连库） |
| `deploy/hotmeme-state-worker.service` | oneshot：`.venv/bin/python -m tools.state_worker --once` |
| `deploy/hotmeme-state-worker.timer` | `OnCalendar=*:0/15`，`Persistent=false` |
| `deploy/hotmeme-admin.service` | `python -m admin.app`，bind `127.0.0.1:8091` |
| `deploy/hotmeme-backup.service` / `.timer` | 每日 `pg_dump -Fc` |
| `deploy/backup.sh` | 备份脚本；输出 `/home/ubuntu/backups/hotmeme-YYYYMMDD.dump`，保留 7 天 |
| `deploy/cutover.sh` | 切流（默认不执行；先 `--check-only`）：校验影子报告 → 停旧 worker → 去 drop-in → 原子替换报告 → 启 timer → 复核 |
| `deploy/cutover-watch.sh` | 自动切流看守：每 5 分钟检查；`flock` 防重叠；预检通过才切流；**切流失败自动回滚**（恢复旧报告/旧 worker、停新 timer、停 watcher） |
| `deploy/hotmeme-cutover-watch.service` + `.timer` | watcher：oneshot，`OnCalendar=*:0/5`，`Persistent=false`，root（需 systemctl） |
| `deploy/shadow-dropin.conf` | 影子模式 systemd drop-in（当前已装到服务器） |
| `deploy/README.md` | 本文 |

## 2. 部署步骤（已执行，可复现）

```bash
# 0) 服务器备份（先备份再改；不备份密钥）
sudo cp -a /home/ubuntu/hotmeme /home/ubuntu/hotmeme.bak-phase-g-$(date +%Y%m%d-%H%M%S)   # 可删 .venv
sudo cp -a /home/ubuntu/hotmeme/var/meme_info_latest.json \
           /home/ubuntu/hotmeme/var/meme_info_latest.json.before-phase-g

# 1) 上传代码（排除 .git/.venv/var/data/archive/.archify/__pycache__；不覆盖 var/ 与 /etc/hotmeme/env）
#    本次因 macOS rsync 与服务器 rsync 协议不兼容，改用 tar over ssh：
tar -czf /tmp/hotmeme-code.tgz --exclude='__pycache__' --exclude='*.pyc' \
    meme_info app admin tools tests migrations deploy requirements.txt requirements-dev.txt run.py README.md
scp /tmp/hotmeme-code.tgz ubuntu@124.220.189.74:/tmp/
ssh ubuntu@124.220.189.74 'cd /home/ubuntu/hotmeme && tar -xzf /tmp/hotmeme-code.tgz'
# 注意：macOS tar 会带 ._* AppleDouble 文件，解压后清理：
find /home/ubuntu/hotmeme -maxdepth 3 -name '._*' -delete

# 2) venv 依赖（psycopg；测试另需 requirements-dev）
cd /home/ubuntu/hotmeme && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# 3) schema 检查（schema_version=2、ok=true）
sudo cp /etc/hotmeme/env /home/ubuntu/.hotmeme_env.tmp && sudo chown ubuntu:ubuntu "$_" && chmod 600 "$_"
set -a; . /home/ubuntu/.hotmeme_env.tmp; set +a
.venv/bin/python -m tools.migrate_state check
rm -f /home/ubuntu/.hotmeme_env.tmp

# 4) 服务器跑测试（DB 测试用事务回滚；实测业务表前后行数 0，无污染）
TEST_DATABASE_URL="$DATABASE_URL" TEST_MIGRATE_DATABASE_URL="$MIGRATE_DATABASE_URL" \
  .venv/bin/python -m pytest -q tests/test_state.py tests/test_video_route.py tests/test_window.py \
  tests/test_pools.py tests/test_report.py tests/test_audit.py tests/test_admin.py tests/test_state_worker.py

# 5) Admin token（只写文件，不打印）并写入 env
openssl rand -hex 32 | sudo tee /etc/hotmeme/admin_token >/dev/null
sudo chmod 600 /etc/hotmeme/admin_token
sudo sh -c 'printf "HOTMEME_ADMIN_TOKEN=%s\n" "$(cat /etc/hotmeme/admin_token)" >> /etc/hotmeme/env'

# 6) 安装 systemd unit + 重启 app 加载新代码（v1/v2 双适配）
sudo cp deploy/hotmeme-state-worker.service deploy/hotmeme-state-worker.timer \
        deploy/hotmeme-admin.service deploy/hotmeme-backup.service deploy/hotmeme-backup.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart hotmeme-app
```

## 3. 影子运行（当前状态）

```bash
# 3.1 装影子 drop-in：新 worker 报告写影子文件，不覆盖生产 v1
sudo mkdir -p /etc/systemd/system/hotmeme-state-worker.service.d
sudo cp /home/ubuntu/hotmeme/deploy/shadow-dropin.conf \
        /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
sudo systemctl daemon-reload

# 3.2 旧 worker 保持运行（生产 v1）；启用新 timer + admin + backup timer
sudo systemctl enable --now hotmeme-state-worker.timer hotmeme-admin hotmeme-backup.timer
systemctl list-timers 'hotmeme*' --no-pager

# 3.3 观察影子轮次（每 15 分钟）
sudo journalctl -u hotmeme-state-worker -n 80 --no-pager
tail -f /home/ubuntu/hotmeme/var/state_worker.log
cat /home/ubuntu/hotmeme/var/meme_info_v2_shadow.json | python3 -c \
  'import json,sys; r=json.load(sys.stdin); print(r["schema_version"], r["status"], r["counts"])'
# 影子轮次也会写 DB：round / video_metric；degraded 轮 Phase D 冻结新 family → pool1 可能为 0
sudo -u postgres psql -d hotmeme -Atc \
  "select id,status,comments_sampled,danmaku_sampled from round order by id desc limit 3"
```

## 4. 自动切流（watcher，PM 决策 A：等白天 ok 非空轮）

`hotmeme-cutover-watch.timer` 每 5 分钟触发一次 `cutover-watch.sh`：

- `flock var/cutover_watch.lock` 防 timer 重叠；拿不到锁 exit 0；
- 生产报告已是 **v2/ok** → 认为切流完成，退出并 `systemctl disable --now hotmeme-cutover-watch.timer`；
- 先跑 `deploy/cutover.sh --check-only`（条件：影子报告 `schema=meme-info/v2`、`status=ok`、
  pool1/pool2 非空、app 200）；**失败只记一行 wait 日志后 exit 0**，等 5 分钟后再查；
- 预检通过才执行 `deploy/cutover.sh`；`cutover.sh` 返回 3（安全中止，生产未切换）时记 abort
  日志并继续等待；成功后记 success 日志（时间/报告/回滚点）并停用 watcher timer；
- **切流失败自动回滚**（安全优先）：
  1. `systemctl disable --now hotmeme-state-worker.timer` + `stop hotmeme-state-worker.service`
     **并等待退出（60s 超时 SIGKILL）**——必须确认已停，否则它可能在恢复旧报告后继续写生产路径；
  2. 原子恢复 `var/meme_info_latest.json.before-phase-g` → 生产报告；
  3. `systemctl enable --now hotmeme-worker`（旧 worker 立即恢复写 v1）；
  4. 停用 `hotmeme-cutover-watch.timer`、写 `var/cutover_watch.failed`，把 cutover 输出尾部写日志，exit 1；
- 日志：`var/cutover_watch.log`（不含 token/密码）。

```bash
sudo systemctl status hotmeme-cutover-watch.timer hotmeme-cutover-watch.service
tail -f /home/ubuntu/hotmeme/var/cutover_watch.log
sudo systemctl start hotmeme-cutover-watch.service   # 手工立即检查一次（安全：预检不过不会切）
```

回滚后想**恢复等待**（人工确认 B站 评论接口/阈值问题已解决后）：

```bash
cd /home/ubuntu/hotmeme
sudo cp deploy/shadow-dropin.conf /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
sudo systemctl daemon-reload
rm -f var/cutover_watch.failed
sudo systemctl enable --now hotmeme-state-worker.timer hotmeme-cutover-watch.timer
# 若旧 worker 仍在写生产 v1，可暂保持 active（watcher 只在影子 ok 时才切；切流脚本会停它）
```

手工切流（不依赖 watcher，条件满足时）：

```bash
cd /home/ubuntu/hotmeme
deploy/cutover.sh --check-only   # 只校验，不改变状态
deploy/cutover.sh                # 全部通过后执行切流
```

前置条件（缺一不可）：影子报告 `schema_version=meme-info/v2`；`status=ok`；
`pool1` 与 `pool2` 都非空；本机 API 与公网 `/hotmeme/` 均 200。

**竞态保护**：`cutover.sh` 在复制影子报告前会先 `disable --now hotmeme-state-worker.timer` +
`stop hotmeme-state-worker.service`（等待退出/超时 SIGKILL），冻结写入后**再次校验**影子报告；
若此时已不可用则**安全中止（退出码 3）**：恢复旧 worker + shadow timer，生产报告不动。
因此即使 watcher 预检通过与 15 分钟 timer 点重合，也不会复制到半成品/旧报告。

## 5. 回滚（切流后出问题）

```bash
# 1) 停新链路（报告/DB 保留，不删数据）
sudo systemctl disable --now hotmeme-state-worker.timer
sudo systemctl stop hotmeme-state-worker.service
sudo systemctl disable --now hotmeme-admin hotmeme-backup.timer

# 2) 恢复影子 drop-in（若要回到影子模式）或直接恢复旧 worker
sudo cp /home/ubuntu/hotmeme/deploy/shadow-dropin.conf \
        /etc/systemd/system/hotmeme-state-worker.service.d/10-shadow.conf
sudo systemctl daemon-reload

# 3) 恢复旧 worker（unit 保留未删）
sudo systemctl enable --now hotmeme-worker

# 4) 报告回滚：用备份覆盖生产报告（旧 worker 下一轮也会重写 v1；立即生效打这份）
sudo cp -a /home/ubuntu/hotmeme/var/meme_info_latest.json.before-phase-g \
           /home/ubuntu/hotmeme/var/meme_info_latest.json
# 或只恢复上一份 ok（v2）：
# sudo cp -a /home/ubuntu/hotmeme/var/meme_info_latest.last_good.json \
#            /home/ubuntu/hotmeme/var/meme_info_latest.json
# app 每请求重读报告，一般无需重启；必要时 sudo systemctl restart hotmeme-app

# 5) DB 表保留（旧 worker 不读 Postgres，不影响旧链路）；需要时可整库从 dump 恢复：
#    pg_restore -d hotmeme /home/ubuntu/backups/hotmeme-YYYYMMDD.dump
```

回滚注意：

- 旧 worker 只写 v1 报告，不碰 Postgres；新 worker 的 round/pool 表数据保留即可，无需清理。
- 新 app 代码（v1/v2 双适配）可继续跑旧 v1 报告，页面不会 500。
- 不修改 Nginx；`/hotmeme/` 反代始终指向 127.0.0.1:8090。

## 6. 运维命令

```bash
sudo systemctl status hotmeme-state-worker.timer hotmeme-state-worker hotmeme-admin hotmeme-backup.timer
sudo systemctl list-timers 'hotmeme*' --no-pager
journalctl -u hotmeme-state-worker -n 100 --no-pager
journalctl -u hotmeme-cutover-watch -n 50 --no-pager
tail -f /home/ubuntu/hotmeme/var/cutover_watch.log   # watcher 日志（wait/success/rollback）
sudo systemctl start hotmeme-state-worker.service        # 手工跑一轮（影子模式写影子报告）
tail -f /home/ubuntu/hotmeme/var/state_worker.log        # worker 日志（不含密码）
tail -f /home/ubuntu/hotmeme/var/backup.log              # 备份日志
sudo systemctl start hotmeme-backup.service              # 手工备份
ls -lh /home/ubuntu/backups/hotmeme-*.dump
# Admin（本机 8091 不暴露公网；SSH 隧道访问）
ssh -L 8091:127.0.0.1:8091 ubuntu@124.220.189.74
# 浏览器打开 http://127.0.0.1:8091/admin/，用 /etc/hotmeme/admin_token 登录
```

## 7. 首次安装（全新机器）

```bash
sudo cp deploy/hotmeme-app.service deploy/hotmeme-admin.service \
        deploy/hotmeme-state-worker.service deploy/hotmeme-state-worker.timer \
        deploy/hotmeme-backup.service deploy/hotmeme-backup.timer /etc/systemd/system/
sudo cp deploy/nginx-hotmeme.conf /etc/nginx/snippets/hotmeme.conf
# 在 /etc/nginx/sites-available/default 的 server{} 内加：
# include /etc/nginx/snippets/hotmeme.conf;
sudo systemctl daemon-reload
sudo systemctl enable --now hotmeme-app hotmeme-state-worker.timer hotmeme-admin hotmeme-backup.timer
sudo nginx -t && sudo systemctl reload nginx
```

访问：<http://124.220.189.74/hotmeme/>
