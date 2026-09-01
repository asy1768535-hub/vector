# 15 · 部署 / 生产清单

## 一句话总览

API 和已启用的 Worker 进程都无状态，可按能力扩展；权威状态主要在 PostgreSQL，向量在 Qdrant，文档 revision 文件可在本地或配置的 object storage，Embedding / Rerank / OCR / Model provider 按部署配置提供。

> **部署方式**：本项目目前**不提供官方 Dockerfile**，推荐用 **Python 虚拟环境（`.venv`）+ systemd** 托管（见下「进程托管」）。如需容器化，可自行基于该 venv 流程编写 Dockerfile，但非内部试运行的必需项。

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
ExecStart=/opt/vector-kb/.venv/bin/python -m app.main
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

基础启用（至少 **API + Embedding Worker + Cleanup Worker**）：

```bash
systemctl enable --now vector-kb-api
systemctl enable --now vector-kb-worker@1
systemctl enable --now vector-kb-cleanup
```

文件上传/导入链路还需单独托管：

```bash
/opt/vector-kb/.venv/bin/python -m app.workers.doc_converter --watch
/opt/vector-kb/.venv/bin/python -m app.workers.importer --watch
```

只接收 PDF/DOCX 等原生格式时可不启动 DOC Converter；一旦 API 公布 `.doc`，必须先
确保 Converter 已运行。两个进程与 API 必须使用同一 `IMPORT_STAGING_DIR`。Converter
初始只启动一个进程并保持 `DOC_CONVERTER_CONCURRENCY=2`，根据 CPU/磁盘监控再调整。

图谱、知识产物、分类不是同一个通用 worker；按对应 feature gate / rollout 分别托管：

```bash
/opt/vector-kb/.venv/bin/python -m app.workers.graph_extractor --watch
/opt/vector-kb/.venv/bin/python -m app.workers.knowledge_artifacts --watch
/opt/vector-kb/.venv/bin/python -m app.workers.classifications --watch
```

上面三个能力 worker 只在对应能力启用时启动；DOC Converter、Importer、feature workers 应使用与 API/基础 worker 相同的 `WorkingDirectory`、`EnvironmentFile`、重启和日志策略建立独立 systemd unit，不要并入 Embedding Worker unit。

### Docker（可选）

未提供 Dockerfile；如果要打包：

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["python", "-m", "app.main"]
```

worker 用同一镜像、覆盖 CMD：

```bash
docker run … vector-kb python -m app.workers.embedder --watch
```

DOC Converter 使用单独镜像层安装 LibreOffice Writer，API/Importer/Embedder 镜像不安装：

```dockerfile
FROM vector-kb AS doc-converter
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer \
 && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "app.workers.doc_converter", "--watch"]
```

Converter 容器与 API/Importer 共享 staging volume，但不需要对外端口。发布顺序为：
数据库备份 → additive migration → 新 API/Importer → DOC Converter → 少量真实 DOC 灰度。
回滚先停止 `.doc` 接收和 Converter，再停新 Importer，避免旧 Importer claim 待转换任务。

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

# 应用 schema（当前代码 head 为 0062；目标库 current 需另行核对）
DB_HOST=<host> DB_USER=postgres DB_PASSWORD=… DB_NAME=vector_kb \
  alembic upgrade head
alembic heads
alembic current
```

> 当前代码 head 为 **`0062`**（2026-08-29 核对）。这里不再使用旧的 v0.1.4 / 0015 部署说明；旧阶段文档保留其历史时点，不能覆盖当前 head。
> 注意：0009 的活动唯一索引创建前，若库内已有违反唯一性的历史活动行需先清理（见 docs/20 §11.1）。

建议 PG 配置：

- `max_connections` 按实际 API/worker 副本数和连接池配置核算；不要把固定副本数当作默认值
- `shared_buffers = 25% RAM`
- 定期 `VACUUM ANALYZE`，尤其 `embedding_jobs` 表（高频 update）

## 监控建议

| 指标 | 来源 | 报警阈值 |
|---|---|---|
| API 5xx 率 | Nginx access_log / Prometheus | > 1% |
| `/health` 失败 | 主动探测 | 连续 3 次 fail |
| `embedding_jobs` pending 堆积 | DB SQL | 持续 > 1000 |
| `embedding_jobs` failed 增长 | DB SQL | 每 5 分钟新增 > 10 |
| 已启用 Worker 进程 | systemd / docker | 与部署 profile 和队列积压阈值一致 |
| Qdrant 内存 | Qdrant 自带 metrics | > 80% |
| Embedding provider 延迟 | 自己埋 / `httpx` 加 hook | P95 > 500ms |

## 备份 & 恢复

> **完整手册见 [docs/27 · 备份恢复与部署演练](27-backup-restore-runbook.md)**，含：PG 备份/恢复、
> Qdrant snapshot、`.env` 安全保存、「Qdrant 丢失走 rebuild」「PostgreSQL 丢失不可恢复项」、
> 以及**恢复后验收 checklist**（按启用能力启动 API/worker + `/health` + 运行状态页 + 临时文档生命周期冒烟）。
>
> 脚本模板：`scripts/backup_pg.ps1`、`scripts/restore_pg.ps1`、`scripts/backup_qdrant.md`
> （参数走环境变量/命令行，不含真实密钥/路径）。

快速参考（下面是 Linux 风格命令；**Windows 部署请优先用 `scripts/backup_pg.ps1` / `scripts/restore_pg.ps1`**）：

```bash
# 备份 PG（custom 格式）
pg_dump -h <host> -U postgres -F c -f vector_kb_$(date +%F).dump vector_kb

# 恢复
pg_restore -h <host> -U postgres -d vector_kb -c vector_kb_<date>.dump
```

Qdrant：用 Qdrant 自带 `snapshots` API，每个 collection 单独；或干脆不备份，靠 `rebuild` 从 PG 重算（见 docs/27 §七）。

## 多副本注意

| 组件 | 说明 |
|---|---|
| API 进程 | 多副本时 cookie 走 SameSite + Secure；JWT_SECRET 必须一致 |
| Worker 进程 | SKIP LOCKED 保证不重复抢锁；多机部署直接起 |
| Casbin 策略 | 各 API 副本内存独占；写策略后其他副本要等下次 `load_policy` |
|              | 临时方案：30 秒 TTL 自动刷新；永久方案：加 Casbin Watcher（Redis Pub/Sub） |

## 横向扩展上限估计（参考）

- 单 API 副本：~500 RPS 普通查询
- 单 Worker 副本：~50 chunks/s（取决于 embedding provider）
- Embedding provider 是检索热路径的瓶颈：把它独立部署 + 横向扩
- Qdrant：单实例可承载几千个 collection、十亿级 points（HNSW 调好）

## 安全建议

- API Key 用户教育：「明文只显示一次，泄漏立刻撤销」
- 定期轮换 superuser 密码
- 「审计日志」页面定期巡检异常操作（半夜建大量库 / 撤大量权限）
- 上 WAF / Rate Limit（Nginx `limit_req` 或 Cloudflare）
- 数据库连接走内网；不要暴露 PG / Qdrant 到公网
