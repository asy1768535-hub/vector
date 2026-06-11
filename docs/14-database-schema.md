# 14 · 数据库 Schema

8 业务表 + Casbin 表，全部由 `alembic/versions/0001_initial_schema.py` 一次性建好。

## ER 关系

```
sys_users (1) ─── (N) sys_api_keys
       │
       │ (1)
       ├─── (N) sys_libraries  (created_by)
       │                │
       │                │ (1)
       │                ├─── (N) documents
       │                │           │
       │                │           │ (1)
       │                │           └── (N) chunks
       │                │
       │                └─── (N) embedding_jobs ── (1) documents
       │
       └─── (N) sys_audit_log  (actor_user_id)

casbin_rule (独立)   ← Casbin SQLAlchemy adapter 维护
```

## 表逐一说明

### `sys_users`（继承 fastapi-users base）

| 字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `id` | UUID PK | fastapi-users | 主键 |
| `email` | str(320) UNIQUE | fastapi-users | 登录名 |
| `hashed_password` | str(1024) | fastapi-users | bcrypt 哈希 |
| `is_active` | bool | fastapi-users | 启用 |
| `is_superuser` | bool | fastapi-users | 超管 |
| `is_verified` | bool | fastapi-users | 邮箱已验证 |
| `username` | str(64) UNIQUE? | 扩展 | 选填 |
| `display_name` | str(128) | 扩展 | 显示名 |
| `created_at` | datetime | 扩展 | |
| `deleted_at` | datetime? | 扩展 | 软删时间 |

### `sys_api_keys`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → sys_users ON DELETE CASCADE | |
| `name` | str(128) | 人类可读标签 |
| `key_prefix` | str(16) INDEX | 明文前 12 字符（vk_xxxxxxxxx），用于 DB 查找 |
| `key_hash` | str(255) | bcrypt(plaintext_key) |
| `expires_at` | datetime? | |
| `last_used_at` | datetime? | |
| `revoked_at` | datetime? | 设非空即撤销 |
| `created_at` | datetime | |

**索引**：
- `(key_prefix)` —— 接收 Bearer 后按 prefix 拿候选
- `(user_id) WHERE revoked_at IS NULL` —— 部分索引，列自己的有效 key

### `sys_libraries`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `slug` | str(80) UNIQUE INDEX | URL safe，= Dify `knowledge_id` |
| `name` | str(160) | |
| `description` | text? | |
| `embedding_model` | str(80) | 例 `bge-m3`，不可改 |
| `embedding_dim` | int | 例 1024，不可改 |
| `vector_distance` | str(16) | `cosine` / `euclid` / `dot` |
| `chunk_size` | int | 默认 1000 |
| `chunk_overlap` | int | 默认 120 |
| `qdrant_collection` | str(128) | `lib_<slug>`，自动赋值（slug 不可变，名字稳定）|
| `created_by` | UUID? FK → sys_users SET NULL | |
| `created_at` | datetime | |
| `deleted_at` | datetime? | 软删 |

### `documents`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `library_id` | UUID FK → sys_libraries ON DELETE CASCADE | |
| `external_id` | str(255)? | 调用方传入，可用于幂等 |
| `title` | str(512)? | |
| `metadata` | jsonb? | 业务自定义字段（会展开进 Qdrant payload） |
| `content_hash` | str(64) | sha256(text)，幂等用 |
| `status` | str(16) | pending / processing / ready / failed / deleted |
| `last_error` | text? | |
| `created_by` | UUID? FK → sys_users SET NULL | |
| `created_at` | datetime | |
| `updated_at` | datetime | onupdate 触发 |
| `deleted_at` | datetime? | |

**索引**：
- `(library_id, content_hash)` —— 幂等查询
- `(library_id, external_id)` —— 外部 ID 反查

### `chunks`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 同时作为 Qdrant point id |
| `document_id` | UUID FK → documents ON DELETE CASCADE | |
| `library_id` | UUID FK → sys_libraries ON DELETE CASCADE | 冗余便于过滤 |
| `seq` | int | 文档内序号 |
| `text` | text | chunk 文本（同时也写入 Qdrant payload.text） |
| `token_count` | int | 当前用字符数；接 tiktoken 后换 |
| `metadata` | jsonb? | chunk 维度 metadata（含 title / external_id） |

**索引**：
- `(library_id, document_id, seq)` —— 按文档拉 chunk 列表

### `embedding_jobs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `library_id` | UUID FK → sys_libraries ON DELETE CASCADE | |
| `document_id` | UUID FK → documents ON DELETE CASCADE | |
| `status` | str(16) | pending / processing / done / failed |
| `worker_id` | str(128)? | 抢到锁的 worker 标识（hostname-pid） |
| `attempt_count` | int | 已尝试次数 |
| `last_error` | text? | |
| `created_at` | datetime | |
| `claimed_at` | datetime? | worker 抢锁时间，stale 检测用 |
| `finished_at` | datetime? | |

**索引**：
- `(status, library_id) WHERE status IN ('pending','processing')` —— 部分索引，worker 抢锁扫描快

### `sys_audit_log`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `actor_user_id` | UUID? FK → sys_users SET NULL INDEX | 操作者；NULL 表示系统 |
| `action` | str(64) INDEX | 如 `library.create` / `permission.grant` |
| `target` | jsonb? | 操作目标的额外信息 |
| `at` | datetime | |

### `casbin_rule`（Casbin SQLAlchemy adapter 标准 schema）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK autoincr | |
| `ptype` | str(255)? | `p` (policy) / `g` (grouping/role) |
| `v0` | str(255)? | sub（user_id 或 role） |
| `v1` | str(255)? | obj（`library:<slug>` 或 `system`） |
| `v2` | str(255)? | act（read/insert/delete/admin） |
| `v3`–`v5` | str(255)? | 暂未使用 |

**索引**：`(ptype, v0)` —— enforcer 加载时按 sub 过滤

## 软删 vs 物理删

| 表 | 软删字段 | 物理删触发 |
|---|---|---|
| `sys_users` | `deleted_at` + `is_active=false` | 走 `/users/{id}` DELETE（fastapi-users 内置） |
| `sys_libraries` | `deleted_at` | 当前无；可加 cron 清理超过 N 天的 |
| `documents` | `deleted_at` + `status='deleted'` | DB 不自动；Qdrant points 异步删 |
| 其他 | 不软删 |  |

软删的好处：审计可追溯；坏处：表会膨胀，需要定期 archive。

## 关键 SQL 速查

```sql
-- 队列堆积情况
SELECT library_id, status, COUNT(*)
FROM embedding_jobs
GROUP BY library_id, status
ORDER BY 1, 2;

-- 最近失败任务
SELECT id, document_id, last_error, attempt_count
FROM embedding_jobs
WHERE status='failed'
ORDER BY finished_at DESC LIMIT 20;

-- 某用户的所有 Casbin 策略
SELECT v1, v2 FROM casbin_rule
WHERE ptype='p' AND v0 = '<user_id>'
ORDER BY v1, v2;

-- 某库的文档列表
SELECT id, title, status, content_hash, created_at
FROM documents
WHERE library_id = (SELECT id FROM sys_libraries WHERE slug='medical')
  AND deleted_at IS NULL
ORDER BY created_at DESC LIMIT 100;

-- 找一把 API Key 属于谁
SELECT u.email, k.name, k.last_used_at
FROM sys_api_keys k JOIN sys_users u ON k.user_id = u.id
WHERE k.key_prefix = 'vk_xxxxxxx';
```

## 迁移

新增表 / 改字段：

```bash
# 改 app/models/*.py 之后
alembic revision --autogenerate -m "add some field"

# 检查生成的 alembic/versions/xxxx.py，必要时手改
alembic upgrade head
```

回滚一步：

```bash
alembic downgrade -1
```
