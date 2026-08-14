# 02 · 架构总览

## 全景图

```
            ┌─────────────────────────────────────────┐
            │             浏览器（Web 后台）           │
            │   /console/ → Vue 3 + Element Plus SPA  │
            └─────────────────┬───────────────────────┘
                              │ JWT cookie
                              ▼
   Dify / 第三方应用 ─── Bearer Key ──▶ ┌──────────────────────────┐
                                       │   FastAPI (uvicorn)      │
                                       │ ─────────────────────    │
                                       │  fastapi-users           │ 认证（cookie + API Key）
                                       │  Casbin (in-process)     │ 权限校验
                                       │  /retrieval /admin/* …   │ 业务路由
                                       └────┬─────────┬───────────┘
                                            │         │
                                  search    │         │  write/CRUD
                                            ▼         ▼
                                   ┌──────────┐  ┌─────────────┐
                                   │  Qdrant  │  │ PostgreSQL  │
                                   │lib_<slug>│  │ 核心表 +     │
                                   │  ……      │  │ 能力表       │
                                   └────▲─────┘  └─────▲───────┘
                                        │              │
                                        │      poll    │ FOR UPDATE
                                        │   ┌──────────┴───────────┐
                                        └───┤ enabled workers       │ 独立进程
                                            │ embed / cleanup       │ 可水平扩展
                                            │ import / graph        │
                                            │ artifact / class      │
                                            └───────────────────────┘
```

## 进程模型

| 进程 | 启动命令 | 状态 | 可水平扩展？ |
|---|---|---|---|
| API | `python -m app.main`（读 `.env`）/ `uvicorn app.main:app` | 无状态 | 按部署 profile 与权限同步策略扩展 |
| Embedding Worker | `python -m app.workers.embedder --watch` | 无状态 | ✅ `SKIP LOCKED` 保证不重复抢锁 |
| Cleanup Worker | `python -m app.workers.cleanup --watch` | 无状态 | ✅ 消费 Qdrant cleanup outbox |
| Import Worker | `python -m app.workers.importer --watch` | 无状态 | 使用文件导入链路时启用 |
| Graph Worker | `python -m app.workers.graph_extractor --watch` | 无状态 | `GRAPH_EXTRACTION_ENABLED=true` 时启用 |
| Knowledge Artifact Worker | `python -m app.workers.knowledge_artifacts --watch` | 无状态 | `KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED=true` 时启用 |
| Classification Worker | `python -m app.workers.classifications --watch` | 无状态 | `CLASSIFICATION_RUNTIME_ENABLED=true` 时启用 |
| PostgreSQL | 外部 | 有状态 | 主从 / 读写分离按需 |
| Qdrant | 外部 | 有状态 | 当前用单节点 |
| Embedding / Rerank / OCR / Model 服务 | 外部或本地 | 无状态 | 按 provider 与 feature gate 配置 |

基础部署通常需要 API、Embedding Worker 和 Cleanup Worker。文件导入、图谱提取、知识产物和分类分别由对应入口消费；未启用的能力不需要启动对应进程。`graph_claim_shadow_enabled` 属于图谱/claim shadow 处理链路，不能从“迁移存在”推断为已对外启用。

## 数据流（写）

```
浏览器 / API 调用
  │
  ▼
POST /libraries/{slug}/documents     ← Casbin 校验 insert
  │
  ▼
services/ingest.ingest_text()
  │  1) sha256(text) 查重 → 命中直接返回（幂等）
  │  2) splitter 切分 → N 个 Chunk 行
  │  3) 插入 Document / Chunks / EmbeddingJob (status=pending)
  │
  ▼
返回 {document_id, status:"pending", job_id, chunk_count}

   ─── 后台异步 ───

worker.run_loop()
  │  CLAIM:  UPDATE embedding_jobs SET status=processing
  │           FOR UPDATE SKIP LOCKED LIMIT N
  │
  │  EMBED:  POST bge-m3 /v1/embeddings  (批量 N)
  │
  │  UPSERT: PUT qdrant /collections/lib_<slug>/points
  │            payload={text, title, document_id, ...}
  │
  ▼
mark done; documents.status = ready
```

## 数据流（读）

```
Dify / 外部应用
  │  Authorization: Bearer <api_key>
  ▼
POST /retrieval  body={knowledge_id, query, retrieval_setting, metadata_condition?}
  │
  ▼
fastapi-users APIKeyStrategy
  │  prefix=key[:12]
  │  SELECT FROM sys_api_keys WHERE key_prefix=… AND revoked_at IS NULL
  │  bcrypt.checkpw(key, candidate.key_hash) → 命中 → User
  ▼
Casbin enforce(user_id, "library:<slug>", "read")
  │
  ▼
services/retrieval.run_retrieval()
  │  embed_one(query) → 1024 维向量
  │  qdrant.search(collection=lib.qdrant_collection, vector=…, limit=top_k,
  │                score_threshold=…, payload_filter=metadata_condition)
  ▼
返回 {records:[{content,score,title,metadata}, …]}（严格按 Dify spec）
```

## 关键设计决策

1. **per-library Qdrant collection 而不是单 collection + filter**
   - 物理隔离，权限漏判 = 0 数据泄漏
   - 不同领域可独立调 HNSW 参数 / 量化策略
   - 代价：collection 数 = 库数；几千个以内 Qdrant 没问题

2. **embedding 走 DB 队列而不是 Redis/Celery**
   - PG 自带 `FOR UPDATE SKIP LOCKED`，分布式抢锁零额外依赖
   - 与 cpwsImportData 同款，运维一份组件
   - 失败兜底：`claimed_at` 超时重置为 pending

3. **认证走 fastapi-users，权限走 Casbin**
   - fastapi-users 处理用户/JWT/密码哈希/重置流，开箱即用
   - Casbin 把权限策略外化到表（`casbin_rule`），未来加角色/继承不改代码
   - 自定义 `APIKeyStrategy` 把 Bearer key 接到 fastapi-users 同一 User 模型

4. **Chunk 文本直接进 Qdrant payload**
   - 检索热路径不需要回查 PG（省 1 次 SQL）
   - PG `chunks` 表只留元数据；真正源数据 = payload.text

5. **后台 SPA 零构建**
   - Vue 3 + Element Plus 走 CDN + ES Module + import map
   - 不需要 npm / node / vite / webpack
   - 想换成全功能模板（vue-vben-admin 等）只需替换 `admin-ui/` 目录

详见 [05 配置](./05-configuration.md) 与 [11 Worker](./11-worker.md)。
