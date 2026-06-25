# 12 · 管理后台 UI

## 概述

零构建 Vue 3 SPA。所有依赖（Vue、Vue Router、Element Plus）从 CDN 加载，`<script type="importmap">` + ES Module。无需 npm / node / vite / webpack。

入口：`http://<host>:8100/console/`（根 `/` 自动 302 到这里）

## 技术栈

| 库 | 来源 |
|---|---|
| Vue 3.4 | `https://unpkg.com/vue@3.4.27/dist/vue.esm-browser.prod.js` |
| Vue Router 4 | `https://unpkg.com/vue-router@4.3.2/dist/vue-router.esm-browser.js` |
| Element Plus 2.7 | `https://unpkg.com/element-plus@2.7.6/dist/index.full.min.mjs` |
| 中文语言包 | `https://unpkg.com/element-plus@2.7.6/dist/locale/zh-cn.mjs` |

## 文件树

```
admin-ui/
├── index.html                # bootstrap + import map
├── style.css                 # 全局样式 (登录页、侧栏、卡片样式)
└── src/
    ├── api.js                # fetch 包装 + 401 拦截
    ├── store.js              # reactive 全局状态（user / permissions）
    ├── app.js                # Vue / Router / Element Plus 装配
    └── views/
        ├── Login.js
        ├── Layout.js         # 侧边栏 + 顶栏 + 动态菜单
        ├── Dashboard.js
        ├── Users.js
        ├── Libraries.js
        ├── Permissions.js    # 矩阵：选用户 → 勾选 → diff 保存
        ├── Documents.js
        ├── ApiKeys.js
        ├── Jobs.js
        └── Audit.js
```

## 路由表

| Path（hash） | 组件 | 角色 |
|---|---|---|
| `#/login` | Login | 公开 |
| `#/dashboard` | Dashboard | 任意登录用户 |
| `#/documents` | Documents | 普通 + 超管 |
| `#/api-keys` | ApiKeys | 普通 + 超管 |
| `#/users` | Users | superuser |
| `#/libraries` | Libraries | superuser |
| `#/permissions` | Permissions | superuser |
| `#/jobs` | Jobs | superuser |
| `#/operations` | RuntimeStatus | superuser |
| `#/audit` | Audit | superuser |

`app.js` 里 `router.beforeEach` 守卫：
1. 未登录 → 跳 `/login`
2. 已登录访问 `/login` → 跳 `/dashboard`
3. 非超管访问 `meta.admin` 路由 → 跳 `/dashboard`

## 状态管理

`src/store.js`：

```js
export const store = reactive({
    user: null,         // 当前用户 (或 null)
    permissions: [],    // [{ library_slug, actions: [] }]
    ready: false,       // 首次加载完成
});

export async function refreshAuth() {
    store.user = await api.me();         // GET /users/me
    store.permissions = await api.myPermissions(); // GET /me/permissions
}
```

登录后调一次 `refreshAuth()`，组件用 `store.user` 渲染。

## 动态菜单

`Layout.js` 根据 `store.user.is_superuser` 决定显示哪些 menu-item：

```html
<el-menu-item index="/dashboard">概览</el-menu-item>
<el-menu-item index="/documents">文档</el-menu-item>
<el-menu-item index="/api-keys">我的 API Key</el-menu-item>

<template v-if="isSuper">
    <el-menu-item-group title="管理员">
        <el-menu-item index="/users">用户管理</el-menu-item>
        <el-menu-item index="/libraries">库管理</el-menu-item>
        ...
    </el-menu-item-group>
</template>
```

## API 客户端

`src/api.js`：

- `request()` 封装 fetch，自动带 `credentials: 'include'`（cookie 同源）
- 401 触发 `onUnauthorized()` → 跳登录
- 业务函数：`login` / `logout` / `me` / `listUsers` / `createLibrary` / ...

## 401 拦截

`app.js` 启动时注册：

```js
setUnauthorizedHandler(() => {
    store.user = null;
    if (currentPath !== '/login') {
        router.replace({ path: '/login', query: { redirect: currentPath } });
    }
});
```

cookie 过期后，下次 fetch 401 → 自动跳登录 → 登录后跳回原页面。

## 页面要点

### Login

- form 字段：`email` + `password`
- 调 `POST /auth/jwt/login`（注意：**form-urlencoded** 不是 JSON，body=`username=email&password=xxx`）
- 成功后 `refreshAuth()` → 跳 `redirect` 或 `/dashboard`

### Dashboard

- 卡片 1：当前用户信息（邮箱 / 角色 / 已授权库列表）
- 卡片 2：服务探活（`GET /health`：DB / Qdrant / 模型）
- 卡片 3：使用提示

### Users

- 表格：邮箱 / 用户名 / 角色 / 状态 / 创建时间
- 操作：启停 / 设超管 / 软删
- 新建用户对话框

### Libraries

- 表格：slug / name / embedding 参数 / chunk 参数 / Qdrant collection / 状态
- 新建对话框（slug 校验、chunk_size/overlap 数字 input）
- 编辑对话框（chunk_size/overlap 可改；embedding 不可改）
- 删除 → 确认弹窗 → 软删 + 后台异步清 Qdrant

### Permissions（矩阵）

最复杂的页面：

1. 顶部 select 选用户
2. watch selectedUser → 调 `GET /admin/permissions?user_id=...` 拉当前权限
3. 在内存里维护 `matrix[slug] = {read, insert, delete}` + `initial` 快照
4. 表格按库列出，每行 3 个 checkbox
5. 「保存变更」按钮：diff 当前 vs 快照，分别调 PUT / DELETE
6. 保存成功后再次 reload

### Documents

- 顶部 select 选库（超管看全部；普通用户从 `store.permissions` 推导）
- 表格：文档列表（id / title / external_id / status / hash / 时间）
- 统计卡片：文档数 / 分片数 / 排队 / 失败
- 「提交文档」对话框：title / external_id / splitter（text/markdown/none）/ metadata JSON / text

### ApiKeys

- 表格：自己的 Key（名称 / 前缀 / 状态 / 最近使用 / 创建时间）
- 「生成新 Key」对话框 → 服务端返回明文 → 弹窗显示（**仅此一次**）+ 复制按钮
- 撤销 → DELETE

### Jobs

- 表格：embedding_jobs 列表
- 过滤：status（pending / processing / done / failed）
- 重试按钮：失败 / 卡死的 job

### RuntimeStatus（运行状态，docs/26 / 批次 C2）

- 数据源：`GET /admin/operations/status`（`api.operationsStatus()`），右上角「刷新」手动拉取，无自动轮询/WebSocket
- **服务状态表**：API / Embedding Worker / Cleanup Worker 三类恒定一行，标签 在线(success) / 降级(warning) / 离线(info)；列「在线/已知」= `online_instances/known_instances`，「最后心跳」相对时间（自报降级附注「（降级）」），「主机/PID」
- **Embedding 任务 / Cleanup Outbox** 两张统计卡（含死信 = failed）
- **重建**卡：进行中 operation 进度条 `done/expected (pct%)` + 失败库数
- 与 `/health`（此刻能否连通）互补：此页看「某进程是否在线 + 上次心跳」

### Audit

- 表格：审计日志
- 过滤：action 字符串

## 二次开发

加新页面：

1. 在 `src/views/` 新建 `MyPage.js`，export 一个 Vue 组件
2. 在 `src/app.js` 的 `routes` 数组加路由
3. 在 `Layout.js` 的菜单里加入口

加新 API 调用：

1. 在 `src/api.js` 加 export 函数
2. 在 view 里 import 用

## 兼容性

- 现代浏览器（Chrome 90+ / Firefox 90+ / Edge 90+ / Safari 14+）
- 不支持 IE / 老 Edge（用 import map / 顶层 await 等现代特性）

## 想换成全功能模板？

把 `admin-ui/` 替换成 vue-vben-admin / Ant Design Pro 等编译后的 `dist/` 即可。

约束：
- 必须能用 cookie + JSON REST 直接调本服务，不要再加 token / state
- 路由根用 hash 或 history mode（用 history 的话 FastAPI 要加 fallback 到 `index.html`）
- 调用 `/auth/jwt/login` 注意是 form-urlencoded，不是 JSON

API 契约稳定（除新增字段外不会破坏），换前端只是 UI 工作。
