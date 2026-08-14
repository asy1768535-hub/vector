# 技术设计

## 基线与分支

实现分支 `codex/consolidate-admin-ui` 基于 `origin/main` 的完整 v0.9
提交 `3849df8` 创建。安全备份分支
`codex/backup-before-frontend-removal-20260724` 保持在 `d636984`，不在其上
直接实施。

基线核对发现安全备份提交来自旧 v0.7 开发线，缺少 v0.8/v0.9 后端接口和
约 7.3 万行已验收实现，因此不能作为可运行 v0.9 的代码基线。`origin/main`
同时包含用户截图中的原版 no-build 管理前端和完整后端能力。

## 路由结构

使用嵌套 hash 路由表达“一级功能域 + 子功能”，由通用域工作区组件渲染
子功能标签和当前 `<router-view>`。

| 功能域 | 新路由 | 子功能 |
|---|---|---|
| 登录 | `/login` | 登录 |
| 知识使用 | `/knowledge-use/*` | `chat`、`search`、`retrieval-test` |
| 知识资产 | `/knowledge-assets/*` | `documents`、`catalog`、`import` |
| 知识治理 | `/knowledge-governance/*` | `graph`、`schema`、`classification` |
| 用户与权限 | `/users-permissions/*` | `users`、`permissions` |
| 库管理 | `/libraries` | 库管理 |
| 运维中心 | `/operations-center/*` | `overview`、`status`、`jobs` |
| 审计中心 | `/audit-center/*` | `operations`、`chat` |
| 账户设置 | `/account/*` | `profile`、`api-keys` |

域根路由根据当前身份跳到第一个可访问子功能。子路由继续携带现有
`admin`、`perm`、`effectivePerm`、`organizationAdmin` 和
`libraryManagement` 元数据，由全局守卫执行精确判断。

历史地址以显式 redirect route 保留，redirect 返回目标 path 和原
`query`，避免 Catalog、分类审核等深链丢失上下文。

## 组件边界

- 新增一个通用 `DomainWorkspace`，只负责标签导航、当前域标题和嵌套视图，
  不持有任何业务数据。
- 现有 `Chat`、`Search`、`Documents`、`Import`、`Users`、`Permissions`、
  `Dashboard`、`RuntimeStatus`、`Jobs`、`Audit`、`ChatLogs` 和 `ApiKeys`
  继续作为独立子路由组件。
- 复用基线中的 `KnowledgeCatalog`、`RetrievalTest`、
  `ClassificationReview`、`GraphGovernance`、`SchemaLifecycle` 及其
  components/helpers。
- 新增账户资料子组件，复用 `store.user`、`api.updateMe` 和现有密码修改
  逻辑；`ApiKeys` 作为账户设置的第二个子功能。
- 从 `Layout` 移除重复的密码弹窗状态，将“账户设置”和“退出登录”保留在
  用户菜单中。

## 导航与权限投影

扩展 `menu_access.js`，将细粒度能力投影为域访问状态：

- `knowledgeUse`: `chat/search/retrievalTest` 任一可用。
- `knowledgeAssets`: `documents/catalog/import` 任一可用。
- `knowledgeGovernance`: `knowledgeGraph/schemaLifecycle/classificationReview`
  任一可用。
- `usersPermissions`、`libraries`、`operationsCenter`、`auditCenter`:
  平台超级管理员。
- `account`: 任一已登录用户。

域工作区只展示允许的子功能。全局路由守卫仍逐子路由鉴权，因此手工输入地址
不能绕过菜单隐藏。默认路由和域根路由共用同一访问投影，避免出现循环跳转。

平台超级管理员对平台管理功能保持全权，但 `catalog`、`knowledgeGraph`、
`retrievalTest`、`schemaLifecycle`、`classificationReview` 均沿用
`origin/main` 的客户内容权限规则。

## 身份与数据流

`store` 增加 `organizations`。`refreshAuth` 并行加载
`/me/permissions` 和 `/me/organizations`；任一附属投影失败时使用空数组，
不阻断已成功的身份会话。退出、401 和预览身份重置时清空三个身份字段。

每个恢复页面继续自行管理其单调请求序列、范围 identity 和 409 fence。
域工作区不缓存 Library、Organization、Document 或结果数据，避免第二事实源。

## API 与样式合并

- 保持基线中目录、分类、组织检索、图谱治理、图谱遍历和 Schema 生命周期
  API 包装器不变。
- 保留当前请求封装、错误清洗、Cookie 和下载行为。
- 恢复新增图标注册，但不替换当前品牌资源和现有图标。
- 从 `origin/main` 提取缺失模块所需 CSS 选择器并合入当前 `style.css`；
  当前登录页和公共布局样式优先，冲突处按现有视觉基线适配。
- 域标签使用稳定高度和响应式内部滚动；聊天子功能通过 route meta 获得剩余
  高度，不依赖旧 `/chat` 路径判断。

## 兼容与迁移

- 旧路由只作为兼容入口，不在侧栏继续显示。
- 内部链接全部迁移到新路由，兼容 redirect 仅服务外部书签和旧测试。
- API 路径和请求体不迁移；恢复代码必须通过现有契约测试。
- 不修改后端挂载规则，`admin-ui/index.html` 和 vendored dependencies 保持
  no-build 加载。

## 风险与控制

1. **CSS 大范围冲突**：不直接覆盖 `style.css`，按缺失模块选择器合并并做
   双视口截图。
2. **域路由放宽权限**：鉴权放在每个叶子路由，测试平台超管无客户内容权限、
   组织管理员和库管理员的差异。
3. **旧深链丢 query**：为每个历史路由写 redirect 单测。
4. **聊天高度回归**：从路径特判改为 route meta，并做桌面/移动端检查。
5. **误用旧安全备份作为运行基线**：实现分支固定基于 `origin/main`，旧分支
   仅作为前端删除操作的回档点。

## 回滚

安全备份分支和提交保持不变。若整合失败，可直接切回
`codex/backup-before-frontend-removal-20260724`；实现按恢复模块、域路由、
账户设置和样式验证拆分，便于定位并撤销单个提交。
