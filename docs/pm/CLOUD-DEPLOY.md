# 云服务器部署（腾讯云 Ubuntu 24.04）

## 公网地址

**http://124.220.189.74/hotmeme/**

- 只读看板；HTTP（当前无域名/证书，暂无 HTTPS）
- 现有 `/`（Next.js）、`:8080`（Caddy）、`:8000` 服务均未改动
- 通过 Nginx 子路径反代，未新开端口/改安全组

## 运行组件

| 组件 | 位置/端口 | 管理 |
|---|---|---|
| app | `127.0.0.1:8090` | `systemctl status hotmeme-app` |
| worker | 600s 循环 | `systemctl status hotmeme-worker` |
| 报告 | `/home/ubuntu/hotmeme/var/meme_info_latest.json` | worker 原子写 |
| 代码 | `/home/ubuntu/hotmeme` | venv `.venv` |
| LLM key | `/home/ubuntu/.hotmeme_llm_key` | 600 |

## Nginx

- 配置片段：`/etc/nginx/snippets/hotmeme.conf`
- 已 `include` 到 `/etc/nginx/sites-available/default` 的 server 块
- 备份：`/etc/nginx/sites-available/default.bak-hotmeme-20261116`
- `/hotmeme` 301 → `/hotmeme/`；`/hotmeme/api/*` 反代到 `127.0.0.1:8090/api/*`

## 更新部署

```bash
tar -czf /tmp/hotmeme-deploy.tgz app meme_info tools data requirements.txt run.py
scp -i ~/.ssh/id_ed25519 /tmp/hotmeme-deploy.tgz ubuntu@124.220.189.74:/tmp/
ssh ubuntu@124.220.189.74 'cd /home/ubuntu/hotmeme && tar -xzf /tmp/hotmeme-deploy.tgz && .venv/bin/pip install -q -r requirements.txt'
ssh ubuntu@124.220.189.74 'sudo systemctl restart hotmeme-app hotmeme-worker'
```

## 验证

```bash
curl -sS http://124.220.189.74/hotmeme/ -o /dev/null -w '%{http_code}\n'
curl -sS http://124.220.189.74/hotmeme/api/health
curl -sS 'http://124.220.189.74/hotmeme/api/meme-info?limit=2&only=publishable'
ssh ubuntu@124.220.189.74 'tail -5 /home/ubuntu/hotmeme/var/worker.log'
```

## 限制

- 无域名/HTTPS；IP 变化需改 Nginx。
- 无鉴权，公开只读；如需私有加 Basic Auth。
- worker 首轮约 200–250s，之后间隔 600s（实际约 800s）。
- 报告在服务器本地；机器故障丢失，需备份。
