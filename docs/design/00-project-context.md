# 00 — 项目上下文

## 项目概述

**向量知识库（Vector Knowledge Base）** — 多租户向量检索平台，提供文档语义检索、智能问答（RAG）、Dify 外部知识库集成和管理后台。

## 实际技术栈

| 层 | 技术 |
|---|---|
| 前端框架 | Vue 3 (ESM browser build, 3.4.27) |
| 路由 | Vue Router 4.5.1 (hash mode) |
| UI 组件 | Element Plus 2.7.6 (full bundle) |
| 语言 | JavaScript (ES modules), 无 TypeScript, 无 .vue SFC |
| 模块加载 | importmap (`index.html` 内联) |
| Markdown | marked 12.0.2 + DOMPurify 3.1.6 |
| 构建 | 零构建（浏览器原生 ESM） |
| 依赖来源 | 全部本地 vendor/ 目录, 零公网 CDN 请求 |
| 图标 | 27 个本地 SVG, `<local-icon>` 自定义元素 |
| 后端 | Python FastAPI, PostgreSQL, Qdrant, Casbin |
| 认证 | JWT (cookie-based, fastapi-users) |

## 前端文件结构

```
admin-ui/
├── index.html              # 入口, importmap, 错误兜底
├── style.css               # 全局样式 + 三主题 CSS 变量 + 组件样式
├── vendor/                 # 8 个本地第三方依赖
│   └── README.md           # 版本与许可证清单
└── src/
    ├── app.js              # Vue + Router + Element Plus 装配
    ├── api.js              # 50 个 API 函数, SSE 流式
    ├── store.js            # 全局状态 (user, permissions, ready)
    ├── theme.js            # 三主题管理 (enterprise/ai-dark/government)
    ├── menu_access.js      # 菜单可见性 + 路由权限纯函数
    ├── icons.js            # 27 个本地 SVG + <local-icon> 自定义元素
    ├── stream_queue.js     # 流式 delta 队列 (纯函数)
    ├── validate.js         # 前端表单校验 (纯函数)
    ├── api_errors.js       # API 错误 → 中文提示 (纯函数)
    └── views/
        ├── Layout.js       # 应用外壳 (侧栏 + 顶栏 + router-view)
        ├── Login.js        # 登录页
        ├── Dashboard.js    # 概览 (用户信息 + 服务健康)
        ├── Chat.js         # 智能问答 (三栏 + 流式 SSE + Markdown)
        ├── Documents.js    # 文档管理 (CRUD + 统计卡片)
        ├── Search.js       # 向量检索 (FAQ 快捷标签)
        ├── Import.js       # 文件导入 (队列上传 + 替换模式)
        ├── ApiKeys.js      # API Key 自助管理
        ├── Users.js        # 用户管理 (CRUD + 授权)
        ├── Libraries.js    # 库管理 (CRUD + FAQ + 重建 + 测试)
        ├── Permissions.js  # 权限矩阵 (diff-based save)
        ├── Jobs.js         # 嵌入任务监控
        ├── RuntimeStatus.js # 运行状态 (heartbeat 聚合)
        ├── Audit.js        # 审计日志 (中文摘要 + JSON 展开)
        └── ChatLogs.js     # 问答日志 (筛选 + 引用来源)
```

## 路由与页面映射 (14 页)

| 路由 | 组件 | 权限要求 |
|---|---|---|
| `/login` | Login.js | 游客 (guest) |
| `/dashboard` | Dashboard.js | 登录即可 |
| `/chat` | Chat.js | `read` 权限 |
| `/documents` | Documents.js | `read` 权限 |
| `/search` | Search.js | `read` 权限 |
| `/import` | Import.js | `insert` 权限 |
| `/api-keys` | ApiKeys.js | 登录即可 |
| `/users` | Users.js | 超管 |
| `/libraries` | Libraries.js | 超管 |
| `/permissions` | Permissions.js | 超管 |
| `/jobs` | Jobs.js | 超管 |
| `/operations` | RuntimeStatus.js | 超管 |
| `/audit` | Audit.js | 超管 |
| `/chat-logs` | ChatLogs.js | 超管 |

## 认证与权限控制

1. **登录**: `/auth/jwt/login` (cookie-based JWT)
2. **鉴权**: 后端 Casbin + 前端路由守卫双重检查
3. **路由守卫** (`app.js:60-81`): store.ready → guest check → auth check → admin check → perm check
4. **菜单可见性** (`menu_access.js`): 普通用户按 `permissions` 数组过滤菜单项
5. **401 处理**: 3 秒抖动窗口, 自动跳转 `/login`
6. **开发模式**: `setMockUser()` 在后端不可用时提供超管预览身份

## 当前工作树状态

- Modified (已跟踪): index.html, api.js, app.js, store.js, theme.js, Chat.js, Layout.js, Login.js, Users.js, style.css, chat_answer.py, docs/12-admin-ui.md, check_release_safety.py
- New (未跟踪): api_errors.js, api_errors.test.mjs, icons.js, stream_queue.js, validate.js, markdown.test.mjs, user_validate.test.mjs, v016.test.mjs, theme.test.mjs, vendor/
- 未 commit/tag/push/deploy
