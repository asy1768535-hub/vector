# 04 · 快速开始

10 分钟从零到 Dify 可对接。

## 前置条件

- Python **3.10+**（建议 3.11/3.12/3.13）
- 一个可访问的 **PostgreSQL**（需有权限 `CREATE DATABASE`，本项目用 13+）
- 一个可访问的 **Qdrant**
- 一个可访问的 **bge-m3 embedding** 服务（OpenAI 兼容 `/v1/embeddings`）

> 如果在 `cpwsImportData` 那台机器上跑，PG/Qdrant/bge-m3 直接复用 `.env` 默认值即可。

## 1. 安装依赖

```bash
cd D:/work_space/vectorDatabase
pip install -r requirements-dev.txt
```

## 2. 配置 `.env`

打开根目录 `.env`，关键三组：

```env
DB_HOST=10.0.10.114
DB_PORT=5434
DB_USER=postgres
DB_PASSWORD=<your_pw>
DB_NAME=vector_kb               # ← 用独立数据库，不要复用 cpwsdata

EMBEDDING_BASE_URL=http://10.0.10.2:8111/v1/embeddings
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024

QDRANT_URL=http://10.0.10.2:6333

JWT_SECRET=<32-bytes-random>    # ← 生产必改！用 `openssl rand -hex 32`
COOKIE_SECURE=false              # 上 HTTPS 时改 true
```

完整配置项见 [05 配置说明](./05-configuration.md)。

## 3. 建库 + 迁移

```bash
# 在 PG 上建独立库
createdb -h 10.0.10.114 -p 5434 -U postgres vector_kb

# 应用初始 migration（创建 8 业务表 + casbin_rule）
alembic upgrade head
```

成功标志：`alembic upgrade head` 退出码 0，无报错。

## 4. 创建首位超管

```bash
python scripts/bootstrap_admin.py --email admin@example.com --password CHANGE_ME --username admin
```

输出：`superuser created: id=<uuid> email=admin@example.com username=admin`

## 5. 启动服务

```bash
# 终端 A：API（host/port 读 .env 的 API_HOST / API_PORT）
python -m app.main

# 终端 B：Worker（长跑）
python -m app.workers.embedder --watch
```

> 如果要临时覆盖端口（不改 `.env`）：
> ```bash
> uvicorn app.main:app --host 0.0.0.0 --port 9999
> ```

## 6. 打开后台

浏览器：`http://<host>:8100/console/`

用刚创建的超管登录，按如下顺序操作：

1. 「库管理」→ 新建库（slug=`medical`, name=`医学库`）
2. 「用户管理」→ 新建普通用户（`alice@example.com`）
3. 「权限矩阵」→ 选 alice → 给 `medical` 库勾选 `read` + `insert` → 保存
4. 用 alice 登录 → 「我的 API Key」→ 生成 Key，**立刻复制明文**
5. 「文档」→ 切到 `medical` 库 → 提交一段文本
6. 等几秒（worker 日志会显示处理）→ 「任务监控」状态变 `done`

## 7. 跑一次 Dify 风格检索

```bash
curl -X POST http://localhost:8100/retrieval \
  -H "Authorization: Bearer <alice 的 plaintext_key>" \
  -H "Content-Type: application/json" \
  -d '{"knowledge_id":"medical","query":"高血压怎么办","retrieval_setting":{"top_k":3}}'
```

返回：

```json
{"records":[{"content":"…","score":0.87,"title":"…","metadata":{…}}, …]}
```

## 8. 接 Dify

Dify「外部知识库」配置：

- **API Endpoint**：`http://<host>:8100/retrieval`
- **API Key**：用 alice 生成的明文
- **Knowledge ID**：`medical`

完成。在 Dify 应用里测试问答即可看到本服务返回的片段。

## 常见首次踩坑

| 现象 | 原因 / 解法 |
|---|---|
| `alembic upgrade head` 报 `database "vector_kb" does not exist` | 先 `createdb vector_kb` |
| `/health` 返回 `db: false` | `.env` 里的 DB_* 填错 / PG 没起 |
| `/health` 返回 `qdrant: false` | Qdrant 不可达；检查 `QDRANT_URL` |
| 摄入后文档一直 `pending` | worker 进程没起；启动 `python -m app.workers.embedder --watch` |
| Dify 调 `/retrieval` 报 401 | Bearer Key 失效或写错；在「我的 API Key」重新生成 |
| Dify 调 `/retrieval` 报 403 | 用户在该库无 read 权限；超管在「权限矩阵」勾上 |
| 后台访问 404 | URL 是 `/console/` 不是 `/admin/`；`/admin/*` 是 API |

更多详见 [17 FAQ](./17-faq.md)。
