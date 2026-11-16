# 云服务器部署（Ubuntu 24.04）

- 代码：`/home/ubuntu/hotmeme`
- app：systemd `hotmeme-app`，127.0.0.1:8090，只读
- worker：systemd `hotmeme-worker`，600s 刷新报告
- 报告：`/home/ubuntu/hotmeme/var/meme_info_latest.json`
- LLM key：`/home/ubuntu/.hotmeme_llm_key`（600）
- 公网：Nginx `location /hotmeme/` 反代到 8090
- 现有 80/8080/8000 服务不受影响

安装：

```bash
sudo cp deploy/hotmeme-app.service deploy/hotmeme-worker.service /etc/systemd/system/
sudo cp deploy/nginx-hotmeme.conf /etc/nginx/snippets/hotmeme.conf
# 在 /etc/nginx/sites-available/default 的 server{} 内加：
# include /etc/nginx/snippets/hotmeme.conf;
sudo systemctl daemon-reload
sudo systemctl enable --now hotmeme-app hotmeme-worker
sudo nginx -t && sudo systemctl reload nginx
```

访问：http://124.220.189.74/hotmeme/
