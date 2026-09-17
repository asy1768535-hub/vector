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

### 原始文件对象存储（MinIO / 本地兼容）

`FileResource` 和结构化 `DocumentRevisionFile` 共用 `ObjectStorageAdapter`。新部署
使用 MinIO 时，推荐设置 `REVISION_FILE_STORAGE_ENABLED=true`、
`ENABLE_EVIDENCE_WRITE_PATH=true`，并安装 `pip install -e ".[object-storage]"`。原始文件
对象只写入配置的 bucket；解析、OCR、向量和图谱仍留在现有处理链路。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DOCUMENT_STORAGE_PROVIDER` | `local` | `local` / `minio` / `oss`；提供 `MINIO_ENDPOINT` 且未显式设置时自动为 `minio`；禁止同时显式设置为非 `minio`，避免源文件误落本地 |
| `DOCUMENT_STORAGE_ENDPOINT_URL` | `""` | 规范 URL；也可用 `MINIO_ENDPOINT` 填主机名，按 `MINIO_SECURE` 补全协议 |
| `DOCUMENT_STORAGE_BUCKET` | `""` | 远端 bucket；也可用 `MINIO_BUCKET`（生产约定 `vector-database-raw`） |
| `DOCUMENT_STORAGE_ACCESS_KEY` | `""` | 运行时凭据；也可用 `MINIO_ACCESS_KEY`，不要使用 MinIO admin |
| `DOCUMENT_STORAGE_SECRET_KEY` | `""` | 运行时 Secret；也可用 `MINIO_SECRET_KEY`，不得提交到仓库或日志 |
| `MINIO_SECURE` | `true` | `MINIO_ENDPOINT` 未带协议时是否使用 HTTPS |
| `DOCUMENT_STORAGE_MAX_READ_BYTES` | `52428800` | Worker 物化/读取原始文件的上限 |
| `DOCUMENT_STORAGE_SIGNED_URL_SECONDS` | `300` | 受控下载 URL 有效期 |

兼容变量映射只在 `Settings` 内完成，业务代码始终读取 `document_storage_*`。读取和删除已有文件时按数据库记录的 `storage_provider` 选择适配器，因此切换 MinIO 后旧 local FileResource 仍可读取。已有
`local` 记录继续按原路径读取，不做历史文件批量迁移。启动和 `/health/ready` 会检查
远端 bucket；网络、TLS、认证或 bucket 权限异常时不会把上传标记为已保存。

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
| helper review | New Jobs use a local helper draft followed by the frozen canonical Qwen provider review; Qwen remains the only canonical output. `GRAPH_EXTRACTION_MINSTRAL_BASE_URL=http://graph-minstral-3b:8000/v1`; `GRAPH_EXTRACTION_MINSTRAL_MODEL=graph-minstral-3b`; `GRAPH_EXTRACTION_MINSTRAL_TIMEOUT_SECONDS=120`; `GRAPH_EXTRACTION_MINSTRAL_MAX_OUTPUT_TOKENS=1000`; `GRAPH_EXTRACTION_QWEN3_DRAFT_MAX_OUTPUT_TOKENS=1000`. Libraries in `GRAPH_EXTRACTION_NUEXTRACT_REVIEW_LIBRARY_IDS=` keep the NuExtract helper with `GRAPH_EXTRACTION_NUEXTRACT_BASE_URL=http://nuextract3-gpu0:8000/v1`; `GRAPH_EXTRACTION_NUEXTRACT_MODEL=nuextract3`; `GRAPH_EXTRACTION_NUEXTRACT_TIMEOUT_SECONDS=120`; `GRAPH_EXTRACTION_NUEXTRACT_MAX_OUTPUT_TOKENS=4000`. Existing frozen Jobs remain unchanged. |
| context/version | `GRAPH_EXTRACTION_MAX_CONTEXT_CHARS=24000`; `GRAPH_EXTRACTION_PREVIOUS_CHUNKS=1`; `GRAPH_EXTRACTION_NEXT_CHUNKS=1`; `GRAPH_SCHEMA_DISCOVERY_MAX_SOURCE_CHUNKS=32`; `GRAPH_SCHEMA_DISCOVERY_CONCEPT_INVENTORY_ENABLED=false`; `GRAPH_SCHEMA_DISCOVERY_TIMEOUT_SECONDS=300`; `GRAPH_SCHEMA_DISCOVERY_CONTEXT_WINDOW_TOKENS=16384`; `GRAPH_SCHEMA_DISCOVERY_MAX_OUTPUT_TOKENS=8000`; `GRAPH_EXTRACTION_PROMPT_VERSION=v1`; `GRAPH_EXTRACTION_EXTRACTOR_VERSION=v1`; `GRAPH_EXTRACTION_OUTPUT_PARSER_VERSION=v1`; `GRAPH_EXTRACTION_CONTEXT_POLICY_VERSION=v1`; `GRAPH_EXTRACTION_POLICY_VERSION=v1`; `GRAPH_EXTRACTION_NORMALIZATION_RULE_VERSION=normalization_v1`; `GRAPH_EXTRACTION_CONFIDENCE_POLICY_VERSION=v1` |
| confidence/evidence | `GRAPH_EXTRACTION_ENTITY_MATERIALIZATION_THRESHOLD=0.85`; `GRAPH_EXTRACTION_RELATION_DRAFT_THRESHOLD=0.85`; `GRAPH_EXTRACTION_WEIGHT_MODEL=0.25`; `GRAPH_EXTRACTION_WEIGHT_EVIDENCE=0.35`; `GRAPH_EXTRACTION_WEIGHT_SCHEMA=0.25`; `GRAPH_EXTRACTION_WEIGHT_NORMALIZATION=0.15`; `GRAPH_EXTRACTION_AUTO_EVIDENCE_TYPES=direct_statement,table_cell`; `GRAPH_EXTRACTION_EVIDENCE_GROUP_POLICY=all_claims_valid` |
| worker/lease | `GRAPH_EXTRACTION_WORKER_POLL_SECONDS=3`; `GRAPH_EXTRACTION_UNIT_LEASE_SECONDS=180`; `GRAPH_EXTRACTION_UNIT_LEASE_RENEW_SECONDS=30`; `GRAPH_EXTRACTION_WORKER_MAX_MODEL_ATTEMPTS=3` |
| retention | `GRAPH_EXTRACTION_CONTEXT_RETENTION_DAYS=30`; `GRAPH_EXTRACTION_RAW_OUTPUT_RETENTION_DAYS=30`; `GRAPH_EXTRACTION_CANDIDATE_RETENTION_DAYS=180` |

Disabling graph_extraction_enabled or external_llm_enabled, or changing the normalized security allowlist,
is permanent for in-flight work.
The Admin PATCH transaction first marks pending Attempts abandoned with unit_cancelled, then marks
queued/processing Units cancelled with unit_cancelled and clears worker/claim/lease fields, and finally
marks queued/processing Jobs cancelled with library_opt_out or security_allowlist_changed. Re-enabling or
restoring the old allowlist does not revive cancelled Jobs.

### v0.5 Active Graph Publication（M1 基础）

配置名和默认值与 `.env.example` 的 `GRAPH_PUBLICATION_*` 块一致。M1 只提供配置和数据库发布快照基础，
不执行 planner、activation、rollback 或 API。

- 全局 `GRAPH_PUBLICATION_ENABLED=false`，发布命令默认不可用。
- confidence threshold 必须在 `[0, 1]`。
- `GRAPH_PUBLICATION_MAX_ITEMS_PER_RUN` 必须为正整数。
- policy / manifest version 不能为空。

| 组 | 变量与默认值 |
|---|---|
| switch/policy | `GRAPH_PUBLICATION_ENABLED=false`; `GRAPH_PUBLICATION_REQUIRE_ENTITY_EVIDENCE=true`; `GRAPH_PUBLICATION_EXTRACTED_ENTITY_MIN_CONFIDENCE=0.85`; `GRAPH_PUBLICATION_EXTRACTED_RELATION_MIN_CONFIDENCE=0.85` |
| limit/version | `GRAPH_PUBLICATION_MAX_ITEMS_PER_RUN=10000`; `GRAPH_PUBLICATION_POLICY_VERSION=v1`; `GRAPH_PUBLICATION_MANIFEST_VERSION=v1` |

### v0.6 Published Graph Retrieval（M1 合同）

M1 只冻结 DTO、配置和启动校验，不挂载 `/v06` 路由，也不执行图查询。配置名和默认值与
`.env.example` 的 `GRAPH_RETRIEVAL_*` 块一致。

- `GRAPH_RETRIEVAL_ENABLED=false`，默认 fail closed。
- contract version 只能是 `v1`。
- seeds/hops/nodes/relations/Evidence 上限都必须为正，且不能超过 v1 绝对上限。
- `GRAPH_RETRIEVAL_MAX_NODES` 不能小于 `GRAPH_RETRIEVAL_MAX_SEEDS`。
- timeout 必须是有限正数。
- v0.6 DTO 不包含 `properties`，请求中的 `include_properties` 会被拒绝。

| 组 | 变量与默认值 |
|---|---|
| switch/version | `GRAPH_RETRIEVAL_ENABLED=false`; `GRAPH_RETRIEVAL_CONTRACT_VERSION=v1` |
| traversal limits | `GRAPH_RETRIEVAL_MAX_SEEDS=10`; `GRAPH_RETRIEVAL_MAX_HOPS=3`; `GRAPH_RETRIEVAL_MAX_NODES=100`; `GRAPH_RETRIEVAL_MAX_RELATIONS=200` |
| evidence/timeout | `GRAPH_RETRIEVAL_MAX_EVIDENCE_PER_FACT=20`; `GRAPH_RETRIEVAL_TIMEOUT_SECONDS=3.0` |

对话图谱增强先使用问答请求的 `top_k`（范围 1–20）完成向量/混合检索，再从排名最靠前的最多
10 个命中切片中读取 `chunk_id` 和 Evidence 锚点。系统只在当前已发布图谱中围绕这些种子扩展
最多 3 跳，并只把带有效原文证据的关系送入回答模型；不会把整个知识库图谱加入上下文。
知识库的图谱辅助问答模式为 `enabled` 时，图谱证据才会实际参与回答；`shadow` 只记录候选
结果，`off` 不执行对话图谱检索。问答请求可通过 `use_graph` 控制本次是否启用图谱辅助，默认
为 `true`；设为 `false` 时不查询图谱。该请求字段不能绕过知识库模式，
`GRAPH_RETRIEVAL_ENABLED=false` 也仍会全局关闭这条链路。

### 切分默认值

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEFAULT_CHUNK_SIZE` | `1000` | 字符数（建库未指定时用） |
| `DEFAULT_CHUNK_OVERLAP` | `120` | 字符数 |

库级 chunk_size / chunk_overlap 优先于这两个默认值。

### PDF 扫描页与独立图片 OCR（仅当库 `ocr_enabled` 开启时生效）

文字版 PDF 不受影响；只有低文字页才渲染 + OCR。独立图片上传支持 BMP/JPEG/JPG/PNG/TIF/TIFF/WEBP，文件名进入检索文本，图片内容交给 RapidOCR；无可识别文字时仍可按文件名检索。需 `pip install -e ".[ocr]"`（含 `pypdfium2`、`Pillow`、RapidOCR）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PDF_OCR_MIN_TEXT_CHARS` | `20` | 单页非空白字符数 < 此值 → 视为图片页走 OCR（否则用文字层） |
| `PDF_OCR_RENDER_DPI` | `200` | 渲染扫描页的 DPI（建议 150~300，越高越清晰也越慢） |
| `PDF_OCR_MAX_PAGES` | `50` | 单份 PDF 最多 OCR 多少页（只计真正进 OCR 的页）；超过即 400 快速失败 |
| `OCR_INTRA_OP_NUM_THREADS` | `8` | RapidOCR 每个 ONNX 模型的计算线程上限 |
| `OCR_INTER_OP_NUM_THREADS` | `1` | RapidOCR ONNX 模型间调度线程上限 |
| `IMAGE_OCR_MAX_INPUT_BYTES` | `134217728` | 独立图片解析上限（默认 128 MiB，可配置 1–500 MiB）；在读入内存和 OCR 前检查，不改变上传总额度 |

OCR 在 Import Worker 中执行；同一进程内 OCR 调用串行，普通文本/Office 文件仍可并发解析。

### 视频转文字检索

视频原文件照常保存到 MinIO；Importer 读取该对象到短生命周期临时文件，用本机 `ffmpeg` 转成单声道 MP3，调用 FunASR 的 `POST /transcribe` 或 OpenAI 兼容的 `POST /audio/transcriptions`，只将转写文本进入已有切块、Embedding 与检索链路。转写结束后视频和音频临时副本都会删除，PG 保存的是 MinIO 定位信息和文档/检索数据，不保存视频副本。

默认关闭。开启开关并配置服务地址后，上传页才会允许 `.mp4`、`.mov`、`.mkv`、`.avi`、`.webm`；选择 `openai_compatible` 时还必须配置模型名。

| 变量 | 说明 |
|---|---|
| `VIDEO_TRANSCRIPTION_ENABLED` | `true` 开启入口 |
| `VIDEO_TRANSCRIPTION_PROVIDER` | `funasr`（现有 `/transcribe` 纯文本响应）或 `openai_compatible`（`/audio/transcriptions`，可返回时间点） |
| `VIDEO_TRANSCRIPTION_BASE_URL` | 转写服务根地址；按 provider 自动补 `/transcribe` 或 `/audio/transcriptions` |
| `VIDEO_TRANSCRIPTION_MODEL` | OpenAI 兼容服务的转写模型名；FunASR 可留空 |
| `VIDEO_TRANSCRIPTION_API_KEY` | 可选 Bearer 密钥，绝不写入 Git |
| `VIDEO_TRANSCRIPTION_FFMPEG_BINARY` | ffmpeg 可执行文件，默认 `ffmpeg` |
| `VIDEO_TRANSCRIPTION_TIMEOUT_SECONDS` | 调用转写服务的最长秒数，默认 1800 |
| `VIDEO_TRANSCRIPTION_EXTRACT_TIMEOUT_SECONDS` | 视频抽音频的最长秒数，默认 1800 |
| `VIDEO_TRANSCRIPTION_MAX_INPUT_BYTES` | 单视频上限，默认 500 MiB |
| `VIDEO_TRANSCRIPTION_MAX_AUDIO_BYTES` | 抽取后 MP3 上限，默认 50 MiB |
| `VIDEO_TRANSCRIPTION_MAX_TRANSCRIPT_CHARS` | 转写文本上限，默认 1000 万字符 |

有效的视频上限取知识库上传上限、`VIDEO_TRANSCRIPTION_MAX_INPUT_BYTES`、`DOCUMENT_STORAGE_MAX_READ_BYTES` 三者最小值。若视频包含分段时间点，检索证据会保留起止秒数；点击原视频时可用该时间点定位播放。

### 文件夹上传吞吐

| 变量 | 默认 | 说明 |
|---|---|---|
| `IMPORT_UPLOAD_CHUNK_BYTES` | `33554432` | 浏览器分块大小（32 MiB） |
| `IMPORT_UPLOAD_FILE_CONCURRENCY` | `1` | 服务端下发给浏览器的单用户文件上传调度并发，上限 16 |
| `IMPORT_UPLOAD_USER_INFLIGHT_LIMIT` | `1` | 服务端允许同一用户同时执行的分块或 Complete 请求数 |
| `IMPORT_UPLOAD_GLOBAL_INFLIGHT_LIMIT` | `10` | 服务端允许全部用户同时执行的分块或 Complete 请求数 |
| `IMPORT_UPLOAD_CLAIM_HEARTBEAT_SECONDS` | `30` | 慢传输、`fsync` 或哈希校验期间续约上传 owner claim 的间隔 |
| `IMPORT_UPLOAD_CLAIM_STALE_SECONDS` | `300` | 上传 owner claim 的过期窗口；必须至少覆盖 3 次心跳 |
| `IMPORT_UPLOAD_RETRY_AFTER_SECONDS` | `2` | 上传繁忙或额度已满时通过 `Retry-After` 返回的建议重试秒数 |
| `IMPORT_WORKER_BATCH_SIZE` | `4` | Import Worker 每轮领取任务数，上限 16；当前按领取顺序逐个处理，不代表解析并发数 |
| `DOC_CONVERSION_MAX_BYTES` | `209715200` | 旧版 `.doc` 单文件转换上限（200 MiB） |
| `DOC_CONVERTER_BINARY` | `soffice` | DOC Converter 使用的 LibreOffice CLI 路径 |
| `DOC_CONVERTER_CONCURRENCY` | `2` | 同时转换 DOC 数，配置范围 1-4；生产初始保持 2 |
| `DOC_CONVERSION_TIMEOUT_SECONDS` | `180` | 单个 DOC 的 LibreOffice 转换超时 |
| `DOC_CONVERTER_POLL_SECONDS` | `1` | Converter 空队列轮询间隔 |
| `DOC_CONVERTER_STALE_SECONDS` | `600` | Converter 租约回收时间；应大于转换超时并预留文件复制时间 |
| `DOC_CONVERTER_MAX_ATTEMPTS` | `3` | DOC 自动转换最多尝试次数；失败后可由任务页人工重试 |
| `XLS_MAX_INPUT_BYTES` | `67108864` | 旧版 `.xls` 单文件解析上限（64 MiB，避免整文件读入耗尽内存） |

`.doc` 只走新的异步新建/文件夹导入协议。临时 DOCX 位于同一 staging 根目录，
只用于解析；正式文件、下载和证据 SHA 仍绑定原 DOC。API、DOC Converter 和
Importer 必须挂载同一个 `IMPORT_STAGING_DIR`。

每用户额度不能高于全局额度。服务端在短事务内原子认领请求，额度已满时返回
`429`；同一上传会话正被其他请求持有时返回可重试的 `409`。两种响应都携带
`Upload-Offset`（存在已提交 offset 时）和 `Retry-After`，客户端必须从服务端已提交
offset 继续，不能盲目重复追加字节。

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
