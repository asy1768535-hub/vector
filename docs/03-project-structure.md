# 03 · 项目结构

```
vectorDatabase/
├── .env                              # 运行配置（DB / Qdrant / embedding URL / JWT secret …）
├── alembic.ini                       # Alembic 配置入口
├── pyproject.toml                    # 依赖（与 requirements*.txt 同步）
├── requirements.txt                  # 运行时依赖
├── requirements-dev.txt              # 测试 / 开发额外依赖
├── README.md                         # 项目主入口（链到 docs/）
│
├── alembic/
│   ├── env.py                        # 迁移引导，从 app.config 读 DB DSN
│   ├── script.py.mako                # 新 migration 模板
│   └── versions/
│       └── 0001_initial_schema.py    # 初始 8 业务表 + casbin_rule
│
├── app/                              # 后端代码
│   ├── main.py                       # FastAPI 入口；挂全部 router + 静态 UI
│   ├── config.py                     # pydantic-settings BaseSettings
│   ├── db.py                         # async engine / session
│   ├── deps.py                       # current_active_user / require_lib(action)
│   │
│   ├── models/                       # SQLAlchemy ORM
│   │   ├── user.py                   #   sys_users（继承 fastapi-users base）
│   │   ├── api_key.py                #   sys_api_keys
│   │   ├── library.py                #   sys_libraries
│   │   ├── document.py               #   documents
│   │   ├── chunk.py                  #   chunks
│   │   ├── embedding_job.py          #   embedding_jobs
│   │   └── audit.py                  #   sys_audit_log
│   │
│   ├── schemas/                      # Pydantic v2 请求/响应模型
│   │   ├── users.py                  #   UserRead/Create/Update（fastapi-users）
│   │   ├── api_keys.py               #   ApiKeyRead/Created
│   │   ├── admin.py                  #   AdminUserCreate / LibraryCreate / PermissionGrant…
│   │   ├── documents.py              #   DocumentIngestRequest / Read / LibraryStats
│   │   └── dify.py                   #   DifyRetrievalRequest / Response
│   │
│   ├── auth/                         # fastapi-users 装配
│   │   ├── user_manager.py           #   UserManager（密码哈希 / 注册回调）
│   │   ├── backend.py                #   cookie_backend + api_key_backend 双通道
│   │   ├── api_key.py                #   自定义 APIKeyStrategy
│   │   └── routes.py                 #   挂 fastapi-users 内置 router
│   │
│   ├── casbin/                       # Casbin 鉴权
│   │   ├── model.conf                #   RBAC 模型
│   │   ├── enforcer.py               #   单例 Enforcer + SQLAlchemy adapter
│   │   └── service.py                #   grant / revoke / list_user_permissions
│   │
│   ├── api/                          # 业务路由
│   │   ├── health.py                 #   GET /health
│   │   ├── me.py                     #   GET /me/permissions（前端动态菜单用）
│   │   ├── api_keys.py               #   GET/POST/DELETE /me/api-keys
│   │   ├── documents.py              #   /libraries/{slug}/documents/* + /stats
│   │   ├── retrieval.py              #   POST /retrieval（Dify）
│   │   ├── admin_users.py            #   /admin/users/*
│   │   ├── admin_libraries.py        #   /admin/libraries/*
│   │   ├── admin_permissions.py      #   /admin/permissions/*
│   │   ├── admin_jobs.py             #   /admin/jobs/*
│   │   └── admin_audit.py            #   /admin/audit-log
│   │
│   ├── services/                     # 业务逻辑
│   │   ├── qdrant.py                 #   async httpx：ensure_collection / search / upsert / delete
│   │   ├── embedding.py              #   bge-m3 /v1/embeddings async 客户端
│   │   ├── splitter.py               #   text / markdown / none 三种切分
│   │   ├── ingest.py                 #   sha256 幂等 + chunk + 入队
│   │   ├── retrieval.py              #   embed → search → DifyRecord 映射
│   │   └── audit_log.py              #   写 sys_audit_log
│   │
│   └── workers/
│       └── embedder.py               # 独立进程：FOR UPDATE SKIP LOCKED 抢锁 + embed + upsert
│
├── admin-ui/                         # 零构建 Vue 3 SPA（CDN 加载）
│   ├── index.html                    # 入口 + import map
│   ├── style.css
│   └── src/
│       ├── api.js                    #   fetch 包装 + 401 拦截
│       ├── store.js                  #   全局状态（user / permissions）
│       ├── app.js                    #   Vue + Router + ElementPlus 装配
│       └── views/
│           ├── Login.js
│           ├── Layout.js             #   侧边栏 + 顶部 + 动态菜单
│           ├── Dashboard.js
│           ├── Users.js
│           ├── Libraries.js
│           ├── Permissions.js        #   矩阵：选用户 → 勾 read/insert/delete → diff 保存
│           ├── Documents.js
│           ├── ApiKeys.js
│           ├── Jobs.js
│           └── Audit.js
│
├── scripts/
│   └── bootstrap_admin.py            # 首次部署创建第一个 superuser
│
├── tests/                            # pytest（无 DB / 无 Qdrant 依赖）
│   ├── test_dify_contract.py         #   Dify spec 字段名严格匹配
│   ├── test_casbin_model.py          #   RBAC 模型矩阵
│   ├── test_retrieval_filter.py      #   metadata_condition → Qdrant filter 映射
│   ├── test_splitter.py              #   text / markdown / none
│   └── test_api_key_strategy.py      #   bcrypt key 生成 / 校验
│
└── docs/                             # ← 你在这
    ├── README.md                     #   本目录索引
    ├── 01-introduction.md
    ├── 02-architecture.md
    ├── 03-project-structure.md       #   ← 当前文档
    └── …
```

## 命名约定

- **`sys_*` 表**：系统级，普通用户接口绝不直接写入；只能通过 `/admin/*` API
- **`lib_<slug>` Qdrant collection**：每库一个，命名直接用库的 slug（如 `lib_medical`），由 `services/qdrant.py` 自动建/删
- **`library:<slug>` Casbin obj**：权限策略统一前缀，便于将来加 `system:*` 等元资源
- **`/admin/*` API**：要求 `is_superuser=True`
- **`/me/*` API**：当前登录用户自助（API Key / Permissions）
- **`/libraries/{slug}/*`**：库维度操作，走 Casbin `require_lib(action)` Depends

## 在哪里改什么

| 想改 | 改哪里 |
|---|---|
| 加新的库级 action（比如 `summarize`） | `app/casbin/service.py:VALID_ACTIONS` |
| 加新的切分器（如 LaTeX） | `app/services/splitter.py` |
| 改 chunk 默认值 | `app/config.py:default_chunk_size/overlap` |
| 加新的管理员动作 | 新建 `app/api/admin_xxx.py` + 在 `app/main.py` include |
| 改后台菜单 / 加页面 | `admin-ui/src/views/*.js` + `admin-ui/src/app.js` 的路由表 |
| 改数据库表 | `app/models/*.py` 改 ORM → `alembic revision --autogenerate` |
