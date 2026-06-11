# Vector Knowledge Base

通用多租户向量知识库服务，提供 **Dify 外部知识库兼容**的检索接口。

> 📚 **完整功能文档**：见 [`docs/`](./docs/README.md) —— 按功能拆分的 18 篇文档，按需查阅。本 README 是项目入口速览。

## 技术栈

- **后端**：FastAPI + SQLAlchemy 2.0 (async) + Alembic
- **认证**：[fastapi-users](https://fastapi-users.github.io/) —— JWT cookie + 自定义 API Key 双通道
- **权限**：[Casbin](https://casbin.org/) RBAC，`(user, library, action)` 三元组
- **向量库**：Qdrant（每个库一个独立 collection，物理隔离）
- **Embedding**：bge-m3（OpenAI 兼容 `/v1/embeddings` 端点）
- **队列**：PostgreSQL `FOR UPDATE SKIP LOCKED`，独立 worker 进程
- **管理后台**：vue-vben-admin（独立子工程，见 [admin-ui](#管理后台-ui)）

## 目录结构

```
vectorDatabase/
├── app/
│   ├── main.py            # FastAPI 入口
│   ├── config.py          # pydantic-settings
│   ├── db.py              # async engine / session
│   ├── deps.py            # require_lib(action) 等通用 Depends
│   ├── models/            # ORM（sys_users / sys_api_keys / sys_libraries / documents / chunks / embedding_jobs / sys_audit_log）
│   ├── schemas/           # Pydantic v2（dify / admin / documents / api_keys / users）
│   ├── auth/              # fastapi-users 装配 + APIKeyStrategy
│   ├── casbin/            # enforcer + model.conf + grant/revoke 服务
│   ├── api/               # 路由（health / retrieval / documents / api_keys / admin_*）
│   ├── services/          # qdrant / embedding / splitter / ingest / retrieval / audit_log
│   └── workers/embedder.py
├── alembic/versions/0001_initial_schema.py
├── scripts/bootstrap_admin.py
├── tests/                 # 契约 / 单元测试
├── admin-ui/              # vue-vben-admin 二开（需自行 clone 模板）
├── .env
├── alembic.ini
└── pyproject.toml
```

## 快速开始

### 1. 依赖

```bash
pip install -e ".[dev]"
```

### 2. 配置 `.env`

确保以下变量正确（默认值已对接 cpwsImportData 同环境的 PG + Qdrant + bge-m3）：

```env
DB_HOST=10.0.10.114
DB_PORT=5434
DB_USER=postgres
DB_PASSWORD=<your_pw>
DB_NAME=vector_kb            # 用独立库，不要复用 cpwsdata

EMBEDDING_BASE_URL=http://10.0.10.2:7997/v1/embeddings
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024

QDRANT_URL=http://10.0.10.2:6333

JWT_SECRET=<change-me-32-bytes-random>
COOKIE_SECURE=false          # 生产置 true
```

### 3. 建库 + 迁移

```bash
# 先用 psql 创建独立数据库
createdb -h $DB_HOST -p $DB_PORT -U $DB_USER vector_kb

# 应用迁移（创建 8 业务表 + casbin_rule）
alembic upgrade head
```

### 4. 创建首位超级管理员

```bash
python scripts/bootstrap_admin.py --email admin@example.com --password CHANGE_ME --username admin
```

### 5. 启动服务

```bash
# API（host/port 读 .env 的 API_HOST / API_PORT）
python -m app.main

# Worker（另一终端，长跑模式）
python -m app.workers.embedder --watch
```

### 6. 打开管理后台

浏览器访问：

```
http://<host>:8100/console/
```

（根路径 `/` 会自动 302 到 `/console/`）

用 bootstrap 出来的 superuser 登录 → 进入「用户管理 / 库管理 / 权限矩阵 / 文档 / API Key / 任务监控 / 审计日志」全套面板。

> 后台是**零构建**的 Vue 3 + Element Plus SPA（CDN 加载），无需 npm/node 环境。代码在 `admin-ui/`，直接编辑即生效。

## API 路线图

| 路径 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/health` | GET | 公开 | DB / Qdrant 探活 |
| `/auth/jwt/login` | POST | 公开 | 表单登录，签发 cookie |
| `/auth/jwt/logout` | POST | cookie | 注销 |
| `/users/me` | GET | cookie/key | 当前用户 |
| `/me/api-keys` | GET/POST | cookie/key | 自助 API Key 管理 |
| `/me/api-keys/{id}` | DELETE | cookie/key | 撤销 Key |
| `/admin/users` | POST/GET | superuser | 用户 CRUD |
| `/admin/users/{id}` | GET/PATCH/DELETE | superuser | 详情 / 改禁用 |
| `/admin/libraries` | POST/GET | superuser | 库 CRUD（同步建/删 Qdrant collection） |
| `/admin/libraries/{slug}` | GET/PATCH/DELETE | superuser | 详情 / 改 chunk 参数 / 软删 |
| `/admin/permissions` | PUT/DELETE/GET | superuser | Casbin 策略授予/撤销/反查 |
| `/admin/permissions/library/{slug}` | GET | superuser | 按库反查授权用户 |
| `/admin/audit-log` | GET | superuser | 审计查询 |
| `/admin/jobs` | GET | superuser | 任务监控 |
| `/admin/jobs/{id}/retry` | POST | superuser | 失败重试 |
| `/libraries/{slug}/documents` | POST | `insert` | 提交文本入库（异步 embed） |
| `/libraries/{slug}/documents` | GET | `read` | 文档列表 |
| `/libraries/{slug}/documents/{id}` | GET | `read` | 文档详情 |
| `/libraries/{slug}/documents/{id}` | DELETE | `delete` | 软删 + 清 Qdrant points |
| `/libraries/{slug}/stats` | GET | `read` | 文档/分片/队列计数 |
| `/retrieval` | POST | API Key | **Dify 兼容**检索 |

## Dify 对接

在 Dify 「外部知识库」配置：

- **API Endpoint**：`http://<your-host>:8100/retrieval`
- **API Key**：使用 superuser 登录后台 → 给目标用户授权 `read` 权限 → 用户登录 → `/me/api-keys` 签发 → 复制明文
- **Knowledge ID**：库的 slug（例如 `medical`、`legal`）

请求体严格按 Dify spec：

```json
{
  "knowledge_id": "medical",
  "query": "...",
  "retrieval_setting": {"top_k": 5, "score_threshold": 0.3},
  "metadata_condition": {
    "logical_operator": "and",
    "conditions": [
      {"name": ["title"], "comparison_operator": "contains", "value": "合同"}
    ]
  }
}
```

响应：

```json
{ "records": [ {"content": "...", "score": 0.87, "title": "...", "metadata": {...}} ] }
```

## 端到端验证

```bash
# 0. 启动 API + worker（见上）

# 1. 超管登录拿 cookie
curl -c jar.txt -X POST http://localhost:8100/auth/jwt/login \
  -d "username=admin@example.com&password=CHANGE_ME"

# 2. 建库
curl -b jar.txt -X POST http://localhost:8100/admin/libraries \
  -H "Content-Type: application/json" \
  -d '{"slug":"medical","name":"医学库"}'

# 3. 建用户并授权 read+insert
curl -b jar.txt -X POST http://localhost:8100/admin/users \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"AlicePw123","is_superuser":false}'
# → 记录返回的 user_id

curl -b jar.txt -X PUT http://localhost:8100/admin/permissions \
  -H "Content-Type: application/json" \
  -d '{"user_id":"<user_id>","library_slug":"medical","actions":["read","insert"]}'

# 4. 用 alice 登录并签 API Key
curl -c alice.txt -X POST http://localhost:8100/auth/jwt/login \
  -d "username=alice@example.com&password=AlicePw123"

curl -b alice.txt -X POST http://localhost:8100/me/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name":"dify-prod"}'
# → 记录 plaintext_key（仅此一次）

# 5. 摄入文本
curl -X POST http://localhost:8100/libraries/medical/documents \
  -H "Authorization: Bearer <plaintext_key>" \
  -H "Content-Type: application/json" \
  -d '{"title":"高血压指南","text":"高血压是慢性病..."}'

# 6. 等 worker 处理（看终端日志），然后检索
curl -X POST http://localhost:8100/retrieval \
  -H "Authorization: Bearer <plaintext_key>" \
  -H "Content-Type: application/json" \
  -d '{"knowledge_id":"medical","query":"高血压怎么治","retrieval_setting":{"top_k":3}}'
```

## 测试

```bash
pytest tests/
```

覆盖：
- `test_dify_contract.py` —— Dify 请求/响应 schema 契约
- `test_casbin_model.py` —— RBAC 模型（直接策略 / 角色继承 / 库隔离）
- `test_retrieval_filter.py` —— metadata_condition → Qdrant filter 映射
- `test_splitter.py` —— text/markdown/none 切分
- `test_api_key_strategy.py` —— Key 生成 / 哈希 / 校验

不需要 DB 或 Qdrant 真实存在；纯单元测试。

## 管理后台 UI（admin-ui/）

**零构建** Vue 3 + Element Plus SPA，所有依赖 CDN 加载，无需 npm/node。已含 8 个页面：

| 路径 | 页面 | 角色 |
|---|---|---|
| `/console/#/login` | 登录 | 公开 |
| `/console/#/dashboard` | 概览（用户信息 + 服务探活） | 所有登录用户 |
| `/console/#/documents` | 文档管理（按库切换；上传/列表/删除） | 普通 + 超管 |
| `/console/#/api-keys` | 自己的 API Key 签发/撤销 | 普通 + 超管 |
| `/console/#/users` | 用户 CRUD / 启停 / 设超管 | superuser |
| `/console/#/libraries` | 库 CRUD（同步建/删 Qdrant collection） | superuser |
| `/console/#/permissions` | 权限矩阵（按用户）勾选 read/insert/delete | superuser |
| `/console/#/jobs` | embedding_jobs 监控 + 失败重试 | superuser |
| `/console/#/audit` | 审计日志 | superuser |

技术：
- Vue 3 + Vue Router + Element Plus 全部走 CDN（`unpkg.com`）+ ES Module + import map
- Cookie 同源自动带，无需手动管理 token
- 动态菜单：登录后调 `/users/me` 取 `is_superuser` + `/me/permissions` 推导可见库
- Hash 路由，无服务端配置；刷新页面不会 404

如果未来想换成全功能模板（vue-vben-admin 等），直接替换 `admin-ui/` 目录、保持 API 兼容即可。

## 性能 / 扩展性要点

- `/retrieval` 热路径：1 次 Casbin in-memory enforce + 1 次 embedding HTTP + 1 次 Qdrant search。chunk 文本直接放在 Qdrant payload，省 1 次 PG 查询
- 多 worker 并发：DB `SKIP LOCKED` 保证不重复处理；worker 进程是无状态，可水平扩
- 多副本 API：Casbin policy 内存缓存默认进程内单例；多副本可挂 Casbin watcher（暂未启用，初版用 30s TTL `load_policy` 兜底，按需打开）
- 每库独立 Qdrant collection，可针对领域调 HNSW 参数 / 量化策略

## 完整文档

按功能拆分在 `docs/`：

| # | 文档 | 内容 |
|---|---|---|
| 01 | [项目介绍](./docs/01-introduction.md) | 它是什么、解决什么问题 |
| 02 | [架构总览](./docs/02-architecture.md) | 全景图、数据流、关键设计 |
| 03 | [项目结构](./docs/03-project-structure.md) | 目录树 + 每个文件的职责 |
| 04 | [快速开始](./docs/04-quickstart.md) | 10 分钟跑起来 |
| 05 | [配置说明](./docs/05-configuration.md) | 全部 `.env` 项 |
| 06 | [认证系统](./docs/06-authentication.md) | fastapi-users + API Key 双通道 |
| 07 | [权限系统](./docs/07-permissions.md) | Casbin RBAC，(user, library, action) |
| 08 | [库管理](./docs/08-libraries.md) | per-library Qdrant collection |
| 09 | [文档摄入](./docs/09-document-ingest.md) | 切分 / 入队 / 幂等 |
| 10 | [检索接口](./docs/10-retrieval-api.md) | Dify spec 完整说明 |
| 11 | [Worker](./docs/11-worker.md) | DB 队列消费 |
| 12 | [管理后台 UI](./docs/12-admin-ui.md) | SPA 架构 + 8 个页面 |
| 13 | [API 参考](./docs/13-api-reference.md) | 全部 endpoint 速查 |
| 14 | [数据库 Schema](./docs/14-database-schema.md) | 8 表 + casbin_rule |
| 15 | [部署](./docs/15-deployment.md) | 生产清单 + systemd + Nginx |
| 16 | [测试](./docs/16-testing.md) | pytest 跑 + 加 |
| 17 | [FAQ](./docs/17-faq.md) | 踩坑速查 |
| **18** | **[使用说明（操作手册）](./docs/18-usage-guide.md)** | **日常怎么用：建库 → 授权 → 摄入 → 检索** |

详细架构与设计决策记录：`C:\Users\Administrator\.claude\plans\dify-stateless-muffin.md`
