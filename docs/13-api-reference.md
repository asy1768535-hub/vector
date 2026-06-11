# 13 · API 完整参考

按命名空间分组。可直接在 `http://<host>:8100/docs` 查看 Swagger UI（FastAPI 自动生成）。

## 健康 / 监控

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | DB / Qdrant / 模型信息 |
| GET | `/` | 公开 | 302 → `/console/` |

## 认证（fastapi-users 内置 router）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/jwt/login` | 表单登录，发 cookie；body=`username=email&password=xxx` |
| POST | `/auth/jwt/logout` | 注销 |
| POST | `/auth/register` | 注册（生产可关；编辑 `app/auth/routes.py`） |
| POST | `/auth/forgot-password` | 申请密码重置 token |
| POST | `/auth/reset-password` | 用 token 重置密码 |
| GET | `/users/me` | 当前用户 |
| PATCH | `/users/me` | 改自己信息 |
| GET | `/users/{id}` | superuser 查 |
| PATCH | `/users/{id}` | superuser 改 |
| DELETE | `/users/{id}` | superuser 物理删 |

## 我（登录用户自助）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/me/permissions` | 当前用户在哪些库上有哪些动作 |
| GET | `/me/api-keys` | 自己的 API Key 列表 |
| POST | `/me/api-keys` | 签发新 Key（明文仅本次返回） |
| DELETE | `/me/api-keys/{id}` | 撤销 |

### 示例：签发 Key

```http
POST /me/api-keys
Content-Type: application/json
Cookie: vk_session=...

{"name": "dify-prod"}
```

```json
{
  "id": "0c1f…",
  "name": "dify-prod",
  "key_prefix": "vk_AbcDeF0123",
  "plaintext_key": "vk_AbcDeF0123456789abcdef0123456789abcdef…",
  "created_at": "2026-05-20T10:00:00Z",
  ...
}
```

## 库级（按 slug，权限走 Casbin）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/libraries/{slug}/documents` | `insert` | 摄入文本 |
| GET | `/libraries/{slug}/documents` | `read` | 列表，可按 `status` / `external_id` 过滤 |
| GET | `/libraries/{slug}/documents/{doc_id}` | `read` | 详情 |
| DELETE | `/libraries/{slug}/documents/{doc_id}` | `delete` | 软删 + Qdrant 异步清 |
| GET | `/libraries/{slug}/stats` | `read` | 文档 / 分片 / 队列计数 |

### 摄入

详见 [09 文档摄入](./09-document-ingest.md)。

## Dify 兼容检索

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/retrieval` | API Key → 该库 `read` | Dify external KB spec |

详见 [10 检索接口](./10-retrieval-api.md)。

## 管理员（superuser 直通）

### 用户

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/users` | 创建用户（含 `is_superuser` 字段） |
| GET | `/admin/users?limit=&offset=&include_deleted=` | 列表 |
| GET | `/admin/users/{id}` | 详情 |
| PATCH | `/admin/users/{id}` | 启停 / 改昵称 / 设超管 |
| DELETE | `/admin/users/{id}` | 软删（is_active=false + deleted_at） |
| GET | `/admin/users/_stats/count` | 总数 / 活跃数 |

### 库

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/libraries` | 创建库 + 同步建 Qdrant collection |
| GET | `/admin/libraries?include_deleted=` | 列表 |
| GET | `/admin/libraries/{slug}` | 详情 |
| PATCH | `/admin/libraries/{slug}` | 改 name / description / chunk 参数 |
| DELETE | `/admin/libraries/{slug}` | 软删 + 后台清 Qdrant collection |

### 权限

| 方法 | 路径 | 说明 |
|---|---|---|
| PUT | `/admin/permissions` | 授权：`{user_id, library_slug, actions:["read",…]}` |
| DELETE | `/admin/permissions` | 撤权（`actions` 不传 = 全清） |
| GET | `/admin/permissions?user_id=<uuid>` | 反查用户权限 |
| GET | `/admin/permissions/library/{slug}` | 反查授权了某库的所有用户 |

### embedding_jobs

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/jobs?status=&library_id=&limit=&offset=` | 列表 |
| POST | `/admin/jobs/{job_id}/retry` | 重置 failed / processing 为 pending |

### 审计日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/audit-log?actor_user_id=&action=` | 列表（最新在前） |

## 通用响应惯例

- **成功响应** body 是资源对象或 `null`（204）
- **错误响应** body：`{"detail": "human readable"}`，HTTP code 表达大类
- **uuid** 字段全部小写带横杠
- **datetime** ISO 8601 + UTC
- **分页** 用 `limit` + `offset`，无 `total` 字段（数 row 太贵；前端按返回长度判断「下一页」）

## 错误码

| 状态 | 含义 |
|---|---|
| 200 | OK（含空数组） |
| 201 | Created |
| 204 | No Content（DELETE 成功） |
| 400 | 请求语义错误（比如 text 为空） |
| 401 | 未登录 / API Key 无效 |
| 403 | 已登录但无权限；或库不存在（统一 403 避免存在性枚举） |
| 404 | 资源不存在（用户 / 文档 / job） |
| 409 | 冲突（如 email / slug 已存在） |
| 422 | 请求 body 不合 schema |
| 500 | 内部错误 |
| 502 | 依赖服务挂了（Qdrant 建 collection 失败） |

## Swagger UI

启动后访问 `http://<host>:8100/docs`，可在浏览器里交互式调每个接口（先在「Authorize」里贴 Bearer Key 或浏览器登录后 cookie 自动带）。

OpenAPI JSON：`/openapi.json`。
