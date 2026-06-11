# 08 · 库管理

## 核心概念

一个**库（library）**对应一个独立的知识域，物理上：

- PostgreSQL `sys_libraries` 表中一行元数据
- Qdrant 中一个独立的 collection（命名 `lib_<slug>`，例如 slug=`medical` → collection=`lib_medical`）

库与库之间**完全隔离**：
- Qdrant 是物理隔离（不同 collection）
- API 层每次都强制走 Casbin 校验 `library:<slug>` × `action`
- 权限漏判 = 0 数据泄漏（不像 single-collection + filter 那种"漏 filter 就漏数据"的模式）

## 字段

### `sys_libraries` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `slug` | str(80) UNIQUE | URL-safe 标识；**也是 Dify 的 `knowledge_id`** |
| `name` | str(160) | 显示名 |
| `description` | text? | 描述 |
| `embedding_model` | str(80) | 例如 `bge-m3`；建库时定，不可改 |
| `embedding_dim` | int | 例如 `1024`；建库时定，不可改 |
| `vector_distance` | str(16) | `cosine` / `euclid` / `dot` |
| `chunk_size` | int | 字符数，默认 1000；可改 |
| `chunk_overlap` | int | 字符数，默认 120；可改 |
| `qdrant_collection` | str(128) | 自动生成 `lib_<slug>`，冗余存便于排查 |
| `created_by` | UUID? | 创建者 user_id |
| `created_at` | datetime | |
| `deleted_at` | datetime? | 软删时间戳 |

### slug 规则

正则 `^[a-z0-9][a-z0-9_-]{1,79}$`：
- 小写字母 / 数字 / `_` / `-`
- 首字符必须是字母数字
- 长度 2-80

✅ 合法：`medical`、`legal-cn`、`team_42`、`hr_2024`
❌ 不合法：`Medical`（大写）、`-foo`（首字符）、`a` (太短)、`a` * 100（太长）

## 建库

### 管理员接口

```http
POST /admin/libraries
Cookie: vk_session=...

{
  "slug": "medical",
  "name": "医学知识库",
  "description": "内科 / 外科指南",
  "chunk_size": 1200,
  "chunk_overlap": 150
}
```

可选字段：
- `embedding_model` / `embedding_dim`：不指定则用全局默认（`settings.embedding_model` / `settings.embedding_dim`）
- `vector_distance`：默认 `cosine`

### 服务端动作

```python
# app/api/admin_libraries.py
1. INSERT sys_libraries (...)               # PG 写入
2. FLUSH → 得到 library.id
3. library.qdrant_collection = f"lib_{library.id}"
4. await qdrant.ensure_collection(
       collection=library.qdrant_collection,
       dim=library.embedding_dim,
       distance=library.vector_distance,
   )                                          # Qdrant 同步建 collection
5. 写审计 sys_audit_log action="library.create"
6. COMMIT
```

**如果 Qdrant 建 collection 失败 → 整个事务回滚，PG 也不会留垃圾记录。**

### Qdrant collection 参数

`services/qdrant.py:ensure_collection()` 用以下默认参数：

```python
{
  "vectors": {"size": dim, "distance": "Cosine"},
  "shard_number": 1,
  "hnsw_config": {"m": 16, "ef_construct": 128},
  "quantization_config": {
      "scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}
  }
}
```

不同领域需要不同参数？修改 `services/qdrant.py:ensure_collection` 函数签名（已经接收关键字参数），在 admin endpoint 透传即可。

## 改库

```http
PATCH /admin/libraries/{slug}
{
  "name": "新名字",
  "description": "新描述",
  "chunk_size": 1500,
  "chunk_overlap": 200
}
```

**可改**：name / description / chunk_size / chunk_overlap

**不可改**：embedding_model / embedding_dim / vector_distance / slug / qdrant_collection

> 改 chunk_size/overlap 只影响**之后**摄入的文档；已入库的不会重切。

## 删库

```http
DELETE /admin/libraries/{slug}
```

操作：

1. `UPDATE sys_libraries SET deleted_at=now() WHERE slug=…`（软删 PG）
2. `BackgroundTasks` 异步调 `qdrant.delete_collection(lib_<slug>)`（清向量）
3. 审计：`library.delete`

> 注：当前实现**不**自动清理 `documents` / `chunks` / `embedding_jobs`，仅软删库。如果库要长期保留 PG 元数据用于审计，这是预期行为；如果要彻底清，可写一个 cron 任务定期物理删除 `deleted_at` 超过 N 天的库。

## 列表 / 详情

```http
GET /admin/libraries?include_deleted=false&limit=50&offset=0
GET /admin/libraries/{slug}
```

普通用户没有 `/admin/libraries` 权限。普通用户可见库列表的获取方式：

```http
GET /me/permissions
→ [{"library_slug":"medical","actions":["read","insert"]}, …]
```

前端用 slug 直接渲染（见 `admin-ui/src/views/Documents.js:loadLibs()`）。

## 库统计

```http
GET /libraries/{slug}/stats        ← 需要 read 权限
```

```json
{
  "library_slug": "medical",
  "document_count": 42,
  "chunk_count": 358,
  "pending_jobs": 0,
  "processing_jobs": 0,
  "failed_jobs": 0
}
```

## 多领域参数建议

| 领域 | chunk_size | chunk_overlap | HNSW m | 备注 |
|---|---|---|---|---|
| 短问答 / FAQ | 300-500 | 50 | 16 | 短文本，原始内容直接放 chunk |
| 长篇报告 / 论文 | 1500-2000 | 150-200 | 16-24 | 上下文需要保留较多 |
| 代码 / 结构化 | 800-1200 | 100 | 16 | 配合 metadata 字段过滤 |
| 法律条文 / 合同 | 600-1000 | 80 | 16 | 段落语义较独立，overlap 不必大 |

> 没有银弹，建议先用默认值跑一轮，对照 Dify 检索效果再调。

## 限制

- **每库一 collection 上限**：Qdrant 单实例几千个 collection 没问题；上万个建议切到 single-collection + library_id filter（在 `services/qdrant.py` 留了抽象，未来重构容易）
- **embedding_model 不可改**：因为 Qdrant collection 维度定了改不了；要换模型 → 新建库重导数据
- **chunk_size/overlap 改了不重切已有文档**：需要重切就先删文档再重新摄入
