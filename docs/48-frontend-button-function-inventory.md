# 前端功能与 Owner 清单

> 更新日期：2026-09-20。此表从 `admin-ui/src/app.js` 静态读取路由、组件和访问条件，用于让 AI 快速定位修改入口。它不是浏览器验收，也不替代后端权限或真实 API 测试。

所有页面使用 `admin-ui/src/api.js` 的同源 Cookie 请求；`app/main.py` 将后台挂载到 `/console/`。实际访问仍由前端 guard 和后端 Casbin/路由权限共同决定。

| 规范路由 | 页面 owner | 访问条件 | 后端 owner 区域 |
| --- | --- | --- | --- |
| `#/knowledge-use/chat` | `views/Chat.js` | Library `read` | `app/api/chat.py`、`services/chat_answer.py` |
| `#/knowledge-use/search` | `views/Search.js` | Library `read` | `app/api/documents.py`、检索服务 |
| `#/knowledge-use/retrieval-test` | `views/RetrievalTest.js` | Organization admin | `app/api/federated_retrieval.py` |
| `#/knowledge-assets/catalog` | `views/KnowledgeCatalog.js` | 有效 Library `read` | `app/api/knowledge_catalog.py` |
| `#/knowledge-assets/my-files` | `views/MyFiles.js` | Library `insert` | `app/api/me.py`、文件资源服务 |
| `#/knowledge-assets/my-tasks` | `views/MyTasks.js` | Library `insert` | `app/api/me.py`、导入任务投影 |
| `#/knowledge-assets/import` | `views/Import.js` | Library `insert` | `app/api/import_uploads.py`、`app/api/documents.py` |
| `#/knowledge-governance/graph` | `views/GraphGovernance.js` | 有效 Library `read` | 图谱 API 与 `app/services/graph_*` |
| `#/knowledge-governance/schema` | `views/SchemaLifecycle.js` | Library management | `app/api/schema_lifecycle.py` |
| `#/users-permissions/users` | `views/Users.js` | Platform superuser | `app/api/admin_users.py` |
| `#/users-permissions/permissions` | `views/Permissions.js` | Platform superuser | `app/api/admin_permissions.py`、Casbin |
| `#/libraries` | `views/Libraries.js` | Platform superuser | `app/api/admin_libraries.py` |
| `#/operations-center/overview` | `views/Dashboard.js` | Platform superuser | 管理状态 API |
| `#/operations-center/status` | `views/RuntimeStatus.js` | Platform superuser | `app/api/admin_operations.py` |
| `#/operations-center/jobs` | `views/Jobs.js` | Platform superuser | `app/api/admin_jobs.py` |
| `#/audit-center/operations` | `views/Audit.js` | Platform superuser | `app/api/admin_audit.py` |
| `#/audit-center/chat` | `views/ChatLogs.js` | Platform superuser | `app/api/admin_chat_logs.py` |
| `#/account/profile` | `views/AccountProfile.js` | 已登录 | `app/api/me.py`、认证 owner |
| `#/account/api-keys` | `views/ApiKeys.js` | 已登录 | `app/api/api_keys.py` |

## 修改 UI 前的检查

1. 先确认规范路由、旧地址兼容重定向和前端 guard 在 `app.js` / `domain_navigation.js` 中的定义；
2. 再读对应 view、`api.js` 和上表后端 owner；不得为了页面展示创建虚假 API、假按钮或绕过权限；
3. 至少运行相关 `*.test.mjs`，涉及真实交互时按 [`quality/verification-matrix.md`](./quality/verification-matrix.md) 补浏览器和后端证据；
4. UI 静态代码或 Node 测试通过不证明目标 API、Cookie、权限或窄屏布局已验收。
