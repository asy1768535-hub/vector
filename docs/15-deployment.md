# 15 · 部署 / 生产清单

## 一句话总览

API 进程 + Worker 进程都无状态，可水平扩展；状态全在 PostgreSQL + Qdrant + bge-m3 三个外部服务里。

## 生产前必改清单

| 项 | 检查 |
|---|---|
| `JWT_SECRET` | 用 `openssl rand -hex 32` 生成；**绝对不要**用默认值 |
| `DB_PASSWORD` | 强密码；不要在源码 / 公开 git 里 |
| `COOKIE_SECURE` | HTTPS 部署时改 `true`，强制 Secure cookie |
| `APP_DEBUG` | 生产 `false` |
| `/auth/register` | 如果不允许公开注册，在 `app/auth/routes.py` 注释掉 `get_register_router` |
| Bootstrap admin 密码 | 首次部署后立刻让该账号在「我的用户」改密码 |
| Qdrant `QDRANT_API_KEY` | 用 Qdrant 私有部署的话务必设；公网暴露的 Qdrant + 无 key = 灾难 |
| HTTPS 证书 | 用 Nginx / Caddy 终止 TLS |
| 数据库备份 | `pg_dump` 定时；尤其 `sys_users` / `sys_api_keys` / `casbin_rule` |

## 进程托管

### systemd（推荐）

`/etc/systemd/system/vector-kb-api.service`：

```ini
[Unit]
Description=Vector KB API
After=network.target

[Service]
User=vkb
WorkingDirectory=/opt/vector-kb
EnvironmentFile=/opt/vector-kb/.env
ExecStart=/opt/vector-kb/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100 --workers 4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/vector-kb-worker@.service`（带 `@` 支持多副本）：

```ini
[Unit]
Description=Vector KB Embedding Worker (%i)
After=network.target

[Service]
User=vkb
WorkingDirectory=/opt/vector-kb
EnvironmentFile=/opt/vector-kb/.env
ExecStart=/opt/vector-kb/.venv/bin/python -m app.workers.embedder --watch
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/vector-kb-cleanup.service`（#7：Qdrant 清理 outbox 消费）：

```ini
[Unit]
Description=Vector KB Cleanup Worker
After=network.target

[Service]
User=vkb
WorkingDirectory=/opt/vector-kb
EnvironmentFile=/opt/vector-kb/.env
ExecStart=/opt/vector-kb/.venv/bin/python -m app.workers.cleanup --watch
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用（生产需 **三类进程**：API + embedding worker + cleanup worker）：

```bash
systemctl enable --now vector-kb-api
systemctl enable --now vector-kb-worker@1 vector-kb-worker@2 vector-kb-worker@3
systemctl enable --now vector-kb-cleanup
```

### Docker（可选）

未提供 Dockerfile；如果要打包：

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
```

worker 用同一镜像、覆盖 CMD：

```bash
docker run … vector-kb python -m app.workers.embedder --watch
```

## Nginx 反代

```nginx
server {
    listen 443 ssl http2;
    server_name kb.example.com;
    ssl_certificate     /etc/letsencrypt/live/kb.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kb.example.com/privkey.pem;

    client_max_body_size 50M;       # 摄入大文本要适当放宽

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

HTTPS 终止后把 `COOKIE_SECURE=true` 即可。

## 数据库

```bash
# 创建
createdb -h <host> -U postgres vector_kb

# 应用 schema（当前 head 为 0010）
DB_HOST=<host> DB_USER=postgres DB_PASSWORD=… DB_NAME=vector_kb \
  alembic upgrade head
```

> 升级到 #6/#7：`alembic upgrade head`（含 0009 revision/rebuild_operations、0010 qdrant_cleanup_outbox）。
> 注意：0009 的活动唯一索引创建前，若库内已有违反唯一性的历史活动行需先清理（见 docs/20 §11.1）。

建议 PG 配置：

- `max_connections >= 200`（每个 worker / API worker 占连接）
- `shared_buffers = 25% RAM`
- 定期 `VACUUM ANALYZE`，尤其 `embedding_jobs` 表（高频 update）

## 监控建议

| 指标 | 来源 | 报警阈值 |
|---|---|---|
| API 5xx 率 | Nginx access_log / Prometheus | > 1% |
| `/health` 失败 | 主动探测 | 连续 3 次 fail |
| `embedding_jobs` pending 堆积 | DB SQL | 持续 > 1000 |
| `embedding_jobs` failed 增长 | DB SQL | 每 5 分钟新增 > 10 |
| Worker 进程数 | systemd / docker | < 配置值 |
| Qdrant 内存 | Qdrant 自带 metrics | > 80% |
| bge-m3 延迟 | 自己埋 / `httpx` 加 hook | P95 > 500ms |

## 备份 & 恢复

```bash
# 备份 PG
pg_dump -h <host> -U postgres -F c -f vector_kb_$(date +%F).dump vector_kb

# 恢复
pg_restore -h <host> -U postgres -d vector_kb -c vector_kb_2026-05-19.dump
```

Qdrant：用 Qdrant 自带 `snapshots` API。每个 collection 单独。

## 多副本注意

| 组件 | 说明 |
|---|---|
| API 进程 | 多副本时 cookie 走 SameSite + Secure；JWT_SECRET 必须一致 |
| Worker 进程 | SKIP LOCKED 保证不重复抢锁；多机部署直接起 |
| Casbin 策略 | 各 API 副本内存独占；写策略后其他副本要等下次 `load_policy` |
|              | 临时方案：30 秒 TTL 自动刷新；永久方案：加 Casbin Watcher（Redis Pub/Sub） |

## 横向扩展上限估计（参考）

- 单 API 副本：~500 RPS 普通查询
- 单 Worker 副本：~50 chunks/s（取决于 bge-m3 服务）
- bge-m3 服务是检索热路径的瓶颈：把它独立部署 + 横向扩
- Qdrant：单实例可承载几千个 collection、十亿级 points（HNSW 调好）

## 安全建议

- API Key 用户教育：「明文只显示一次，泄漏立刻撤销」
- 定期轮换 superuser 密码
- 「审计日志」页面定期巡检异常操作（半夜建大量库 / 撤大量权限）
- 上 WAF / Rate Limit（Nginx `limit_req` 或 Cloudflare）
- 数据库连接走内网；不要暴露 PG / Qdrant 到公网
