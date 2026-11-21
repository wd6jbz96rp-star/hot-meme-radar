# Phase F 交付报告：LLM/人工审核与 Admin

> 日期：2026-11-22  
> 状态：**完成**（未部署 Admin/未切流量）

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `meme_info/audit.py` | 审核规则、evidence_hash、队列、audit_event/pool 同步、LLM 包装 |
| `tools/audit_round.py` | CLI：`list` / `run`（LLM 可选）/ `show` |
| `admin/app.py` | 独立 FastAPI Admin（默认 127.0.0.1:8091） |
| `admin/static/index.html` | 无 CDN 审核页（textContent 渲染） |
| `tests/test_audit.py` | 24 单元 + 11 Postgres 集成 |
| `tests/test_admin.py` | 14 单元 + 6 Postgres 集成 |

## 2. 审核规则

- LLM：pass +8 / fail -15 / unclear 0
- 人工：pass +15 / fail -25 / need_more 0 / merge 重算
- **一次性事件**：`score = clamp(score - old_adjustment + new_adjustment, 0, 100)`；Phase D 每轮不再重复加 `audit_adjustment`
- 同一 `(family,evidence_hash,kind)` 唯一；人工覆盖 LLM；证据变化才重审
- 审核后同步 P1/P2（40/32、65/55）；fail 撤出 pool2；explainer-only 沿用 Phase D
- LLM 默认关闭：`MEME_INFO_VERIFY=1` 且配置 key 才调用

## 3. Admin

```bash
HOTMEME_ADMIN_TOKEN=... ADMIN_DATABASE_URL=... python -m admin.app
# 或
python -m admin.app --host 127.0.0.1 --port 8091
```

- 未配置 token：管理端点 503；未授权 401；登录后 SameSite=Strict HttpOnly cookie 或 `X-Admin-Token`
- `GET /admin/`、`POST /admin/login`、`GET /admin/queue`、`POST /admin/action`、`GET /admin/health`
- 通过 SSH 隧道访问；不暴露公网、不 import 公开 app
- 操作：approve/reject/merge/need_more；reject/merge 必填 reason/operator

## 4. 验证

- 本地全量：**255 passed, 41 skipped**
- 云上真实 Postgres：`test_audit/test_admin` **55 passed**；全量 296 passed/0 skipped；事务回滚 0 残留
- schema_version=2；8 张业务表 0 行；现有服务 active
- LLM 未真实调用；测试用 fake verifier

## 5. 限制

- `audit_event UNIQUE` 使同证据人工改判覆盖原结论，不保留每次改判历史；如需完整流水需迁移调整。
- merge 按 `count_now+1` 加权重算，不是真正重算。
- 审核同步 P2 用 `pool1.count_now>0` 作 usage 代理（窗口视频必来自窗口评论/弹幕，当前自洽）。
- LLM 证据包只用 pool1.evidence_json 前 5 条窗口证据，效果需 shadow 验证。
- Admin 未接 Nginx/systemd，无多操作员账号体系；Phase G 处理。
