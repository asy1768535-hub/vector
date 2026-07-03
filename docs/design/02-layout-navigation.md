# 02 — 布局与导航

## 全局布局结构

```
┌─────────────────────────────────────────────────┐
│ 顶栏 (64px fixed)              主题 | 头像 ▼     │
├────────┬────────────────────────────────────────┤
│ 侧栏   │                                        │
│ 224px  │  内容区 (router-view)                   │
│        │  页面背景 #F5F7F8                       │
│  Logo  │                                        │
│  ────  │  页面标题                               │
│  ● 智   │  ┌─ 筛选栏 ───────────────────────┐    │
│    能   │  │ ...                              │    │
│    问   │  └─────────────────────────────────┘    │
│    答   │  ┌─ 表格 / 卡片区 ────────────────┐    │
│  ● 文   │  │ ...                              │    │
│    档   │  └─────────────────────────────────┘    │
│  ● 检   │                                        │
│    索   │                                        │
│  ● 导   │                                        │
│    入   │                                        │
│  ● API  │                                        │
│  ────  │                                        │
│  管理员 │                                        │
│  ● 概   │                                        │
│    览   │                                        │
│  ...   │                                        │
└────────┴────────────────────────────────────────┘
```

## 顶栏 (Layout.js, `.layout-header`)

- **高度**: `64px` (固定)
- **背景**: `#FFFFFF`
- **底边框**: `1px solid #DCE3E8`
- **内容**:
  - 左: 折叠按钮 (`<local-icon icon="mdi:menu/mdi:backburger">`) + 当前页面标题 (`route.meta.title`)
  - 右: 主题切换下拉 (`el-dropdown` + palette 图标) + 用户头像 + 用户名 + 下拉菜单 (修改密码 / 退出登录)
- **禁止**: 消息中心、帮助中心、通知铃铛等无后端支持的功能按钮

## 侧栏 (Layout.js, `.layout-aside`)

- **展开宽度**: `224px`
- **收起宽度**: `64px`
- **背景**: `#FFFFFF`
- **右边框**: `1px solid #DCE3E8`
- **Logo 区域**: 高 60px, 底部细线分隔

### 菜单结构

**上部 — 普通用户功能** (按 `menuAccess()` 结果动态显示):

| 序号 | 路由 | 标题 | 权限要求 | 图标 |
|---|---|---|---|---|
| 1 | `/chat` | 智能问答 | `read` | `mdi:chat-question-outline` |
| 2 | `/documents` | 文档 | `read` | `mdi:file-document-outline` |
| 3 | `/search` | 数据检索 | `read` | `mdi:text-search` |
| 4 | `/import` | 导入数据 | `insert` | `mdi:database-import-outline` |
| 5 | `/api-keys` | 我的 API Key | 登录即可 | `mdi:key-variant` |

**下部 — 管理员功能** (`v-if="isSuper"`, `el-menu-item-group title="管理员"`):

| 序号 | 路由 | 标题 | 图标 |
|---|---|---|---|
| 6 | `/dashboard` | 概览 | `mdi:view-dashboard-outline` |
| 7 | `/users` | 用户管理 | `mdi:account-group-outline` |
| 8 | `/libraries` | 库管理 | `mdi:bookshelf` |
| 9 | `/permissions` | 权限矩阵 | `mdi:shield-key-outline` |
| 10 | `/jobs` | 任务监控 | `mdi:cog-sync-outline` |
| 11 | `/operations` | 运行状态 | `mdi:heart-pulse` |
| 12 | `/audit` | 审计日志 | `mdi:history` |
| 13 | `/chat-logs` | 问答日志 | `mdi:comment-text-multiple-outline` |

### 菜单项样式

- 默认: 13px, `#3C4652`, 透明左边框 3px
- Hover: 浅灰背景 `rgba(128,128,128,0.06)`
- Active: 浅绿背景 `#EAF4F0` + 左边框 `3px solid #176B57` + 文字 `#176B57` 600 字重
- 分组标题: 11px, `#97A3B0`, uppercase

### 权限逻辑

- 侧栏菜单项通过 `menuAccess(store.user, store.permissions)` 计算可见性
- 超管看到全部菜单
- 普通用户只看到有对应权限的菜单
- 前端隐藏 ≠ 后端鉴权（后端 Casbin 仍然检查）

## Chat 页三栏布局

Chat 页面 (`Chat.js`) 在侧栏右侧的内容区采用独立三栏:

```
[侧栏 224px] [会话历史 300px] [问答区 剩余宽度]
```

- **会话历史面板** (`.chat-history-panel`): 白色背景, 右边框, 300px 固定宽
  - 顶部 "会话" 标题
  - 库选择器 + 新建会话按钮
  - 会话列表 (标题省略 + hover 显示操作图标)
  - Active 项: 浅绿背景 + 左边框绿色指示条
- **问答主区** (`.chat-main`): 页面背景色
  - 消息区 (可滚动)
  - 输入栏 (底部固定)
