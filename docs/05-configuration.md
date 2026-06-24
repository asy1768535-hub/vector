# 05 · 配置说明

所有运行配置走 `.env` + `app/config.py`（pydantic-settings）。启动时会做类型校验，错配立即报错。

## 完整配置列表

### 数据库（PostgreSQL）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DB_HOST` | `localhost` | 主机 |
| `DB_PORT` | `5432` | 端口 |
| `DB_USER` | `postgres` | 用户名 |
| `DB_PASSWORD` | `""` | 密码（务必改） |
| `DB_NAME` | `vector_kb` | 数据库名；**用独立库，别复用其他** |

派生：
- `db_dsn_async` → `postgresql+asyncpg://…`（API / worker 用）
- `db_dsn_sync` → `postgresql+psycopg2://…`（Alembic / Casbin adapter 用）

### Embedding 服务（OpenAI 兼容）

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMBEDDING_BASE_URL` | `http://10.0.10.2:8111/v1/embeddings` | 完整端点 URL（含 `/v1/embeddings`） |
| `EMBEDDING_MODEL` | `bge-m3` | 模型名（须与服务端加载一致） |
| `EMBEDDING_DIM` | `1024` | 向量维度（必须与模型输出一致，否则 worker 报错） |

> `embedding_model` 也支持库级覆盖：建库时可指定 `embedding_model` / `embedding_dim`。

### Qdrant

| 变量 | 默认 | 说明 |
|---|---|---|
| `QDRANT_URL` | `http://10.0.10.2:6333` | HTTP 端口 |
| `QDRANT_API_KEY` | `""` | 可选；私有 Qdrant 用 |

### API 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_HOST` | `0.0.0.0` | uvicorn 监听 |
| `API_PORT` | `8100` | uvicorn 端口 |
| `APP_DEBUG` | `false` | true 时 SQLAlchemy echo + FastAPI debug |

### 认证（JWT cookie）

| 变量 | 默认 | 说明 |
|---|---|---|
| `JWT_SECRET` | `please-change-me-in-env` | **生产必改**；建议 `openssl rand -hex 32` |
| `JWT_LIFETIME_SECONDS` | `43200`（12h） | cookie 有效期 |
| `COOKIE_SECURE` | `false` | 上 HTTPS 后改 `true` |
| `COOKIE_NAME` | `vk_session` | cookie 名 |

### Embedding Worker

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMBED_BATCH_SIZE` | `32` | 单次 HTTP 批大小 |
| `EMBED_WORKER_BATCH_DOCS` | `8` | 单轮抢锁文档数（≠ chunks 数） |
| `EMBED_WORKER_POLL_SECONDS` | `1.0` | watch 模式空闲 sleep |
| `EMBED_WORKER_STALE_SECONDS` | `3600` | `processing` 超时秒数 → 重置为 `pending` |
| `EMBED_WORKER_MAX_ATTEMPTS` | `5` | 失败重试上限；超过标 `failed` |

### 切分默认值

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEFAULT_CHUNK_SIZE` | `1000` | 字符数（建库未指定时用） |
| `DEFAULT_CHUNK_OVERLAP` | `120` | 字符数 |

库级 chunk_size / chunk_overlap 优先于这两个默认值。

### PDF 扫描页 OCR（仅当库 `ocr_enabled` 开启时生效）

文字版 PDF 不受影响；只有低文字页才渲染 + OCR。需 `pip install -e ".[ocr]"`（含 `pypdfium2`、`Pillow`、RapidOCR）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PDF_OCR_MIN_TEXT_CHARS` | `20` | 单页非空白字符数 < 此值 → 视为图片页走 OCR（否则用文字层） |
| `PDF_OCR_RENDER_DPI` | `200` | 渲染扫描页的 DPI（建议 150~300，越高越清晰也越慢） |
| `PDF_OCR_MAX_PAGES` | `50` | 单份 PDF 最多 OCR 多少页（只计真正进 OCR 的页）；超过即 400 快速失败 |

> OCR 在上传请求内**同步**执行，大批扫描页会较慢；靠上面两个上限兜底。详见 [09 文档摄入](./09-document-ingest.md)。

## 加新配置项

1. 在 `app/config.py` 的 `Settings` 类加字段，pydantic 自动校验类型
2. 加默认值（建议有合理默认，方便测试）
3. 在 `.env` 加注释，便于运维
4. 引用：`from app.config import settings; settings.<your_field>`

## 配置文件加载顺序

1. `.env`（项目根目录）
2. 进程环境变量（覆盖 `.env`）
3. 代码默认值（兜底）

> CI / Docker 部署时建议把敏感项放进程环境变量，`.env` 只放非敏感默认。

## 健康检查

```bash
curl http://localhost:8100/health
```

```json
{
  "status": "ok",
  "db": true,
  "qdrant": true,
  "embedding_model": "bge-m3",
  "embedding_dim": 1024
}
```

任一项 `false` → 该项配置或外部服务有问题。
