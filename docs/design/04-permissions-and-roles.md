# 04 — 权限与角色

## 当前权限模型

### 用户类型

| 类型 | `is_superuser` | `is_active` | 说明 |
|---|---|---|---|
| 超级管理员 | `true` | `true` | 全部页面可见, 全部 API 可调 |
| 普通激活用户 | `false` | `true` | 按 Casbin 权限矩阵限制 |
| 禁用用户 | `false` | `false` | 无法登录, 前端软删除标记 |

### Casbin 权限动作

| 动作 | 含义 | 前端影响 |
|---|---|---|
| `read` | 读取文档 / 检索 / 问答 | 显示 文档、数据检索、智能问答 |
| `insert` | 上传文档 | 显示 导入数据 |
| `delete` | 删除文档 | 文档页删除按钮可用 |
| `admin` | 管理库配置 | 当前前端未使用 |
| `submit` | 提交 | 当前前端未使用 |
| `review` | 审核 | 当前前端未使用 |

### 权限粒度

- **粒度**: 用户 × 知识库 × 动作
- **存储**: Casbin policy (PostgreSQL adapter)
- **API**: `/admin/permissions` (grant/revoke)

## 前端权限控制

### 路由级 (app.js router guard)

```javascript
// meta.admin: true → 仅超管
// meta.perm: 'read' | 'insert' → 有对应权限或超管
// 无权限时跳 /dashboard + warning 提示
```

### 菜单级 (menu_access.js)

- `menuAccess(user, permissions)` 返回 `{ documents, search, chat, import, apiKeys }` 布尔值
- Layout.js 模板用 `v-if="access.documents"` 等控制显隐
- 超管全部可见

### 页面内操作级

- Documents.js: `canInsert` / `canDelete` computed
- Import.js: 选库逻辑受权限过滤
- Search.js: 库列表受 `readableLibraries()` 过滤

## 不实施的范围

- ❌ 角色管理 (RBAC) 页面
- ❌ 权限申请/审批流程
- ❌ 组织/部门级权限
- ❌ 基于属性的访问控制 (ABAC)
- ❌ 临时授权/过期授权
