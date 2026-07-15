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

### v0.4 Graph Extraction（M1 基础）

配置名和默认值与 `.env.example` 的 `GRAPH_EXTRACTION_*` 块完全一致。M1 只提供
配置、数据库基础、安全开关和 Heartbeat，不执行模型抽取。

- 全局 `GRAPH_EXTRACTION_ENABLED=false`，Library 的两个 opt-in 也默认 `false`；
  security allowlist 默认 `[]`，任一条件不满足均拒绝向外部模型发送数据。
- `GRAPH_EXTRACTION_API_KEY` 必须显式设置，绝不回退到 `CHAT_API_KEY`，也不得进入
  日志、配置快照、错误详情或 API 响应。
- 四项 confidence 权重各自在 `[0, 1]` 且总和为 `1.0`。
- lease renew 必须小于 lease 的一半；Provider timeout 必须小于 Unit lease。
- 三项 retention days 必须为正整数。
- 自动触发开启时，`GRAPH_EXTRACTION_API_KEY` 必须非空。

| 组 | 变量与默认值 |
|---|---|
| switch/provider | `GRAPH_EXTRACTION_ENABLED=false`; `GRAPH_EXTRACTION_AUTO_TRIGGER_ENABLED=false`; `GRAPH_EXTRACTION_BASE_URL=https://api.deepseek.com/v1`; `GRAPH_EXTRACTION_MODEL=deepseek-v4-pro`; `GRAPH_EXTRACTION_API_KEY=`; `GRAPH_EXTRACTION_TIMEOUT_SECONDS=120`; `GRAPH_EXTRACTION_TEMPERATURE=0`; `GRAPH_EXTRACTION_RESPONSE_FORMAT=json_object` |
| context/version | `GRAPH_EXTRACTION_MAX_CONTEXT_CHARS=24000`; `GRAPH_EXTRACTION_PREVIOUS_CHUNKS=1`; `GRAPH_EXTRACTION_NEXT_CHUNKS=1`; `GRAPH_EXTRACTION_PROMPT_VERSION=v1`; `GRAPH_EXTRACTION_EXTRACTOR_VERSION=v1`; `GRAPH_EXTRACTION_OUTPUT_PARSER_VERSION=v1`; `GRAPH_EXTRACTION_CONTEXT_POLICY_VERSION=v1`; `GRAPH_EXTRACTION_POLICY_VERSION=v1`; `GRAPH_EXTRACTION_NORMALIZATION_RULE_VERSION=normalization_v1`; `GRAPH_EXTRACTION_CONFIDENCE_POLICY_VERSION=v1` |
| confidence/evidence | `GRAPH_EXTRACTION_ENTITY_MATERIALIZATION_THRESHOLD=0.85`; `GRAPH_EXTRACTION_RELATION_DRAFT_THRESHOLD=0.85`; `GRAPH_EXTRACTION_WEIGHT_MODEL=0.25`; `GRAPH_EXTRACTION_WEIGHT_EVIDENCE=0.35`; `GRAPH_EXTRACTION_WEIGHT_SCHEMA=0.25`; `GRAPH_EXTRACTION_WEIGHT_NORMALIZATION=0.15`; `GRAPH_EXTRACTION_AUTO_EVIDENCE_TYPES=direct_statement,table_cell`; `GRAPH_EXTRACTION_EVIDENCE_GROUP_POLICY=all_claims_valid` |
| worker/lease | `GRAPH_EXTRACTION_WORKER_POLL_SECONDS=3`; `GRAPH_EXTRACTION_UNIT_LEASE_SECONDS=180`; `GRAPH_EXTRACTION_UNIT_LEASE_RENEW_SECONDS=30`; `GRAPH_EXTRACTION_WORKER_MAX_MODEL_ATTEMPTS=3` |
| retention | `GRAPH_EXTRACTION_CONTEXT_RETENTION_DAYS=30`; `GRAPH_EXTRACTION_RAW_OUTPUT_RETENTION_DAYS=30`; `GRAPH_EXTRACTION_CANDIDATE_RETENTION_DAYS=180` |

Disabling graph_extraction_enabled or external_llm_enabled, or changing the normalized security allowlist,
is permanent for in-flight work.
The Admin PATCH transaction first marks pending Attempts abandoned with unit_cancelled, then marks
queued/processing Units cancelled with unit_cancelled and clears worker/claim/lease fields, and finally
marks queued/processing Jobs cancelled with library_opt_out or security_allowlist_changed. Re-enabling or
restoring the old allowlist does not revive cancelled Jobs.

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
