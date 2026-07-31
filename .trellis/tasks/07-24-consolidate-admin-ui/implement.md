# 实施计划

## 1. 建立安全实现分支

- 确认 `origin/main` 当前 v0.9 提交为 `3849df8` 且 `admin-ui/` 无修改。
- 从 `origin/main` 创建并切换到 `codex/consolidate-admin-ui`。
- 将任务 branch 设为新分支，base branch 设为 `main`。
- 不纳入现有无关未跟踪文件。

## 2. 核对已验收 v0.9 模块

- 确认目录、检索诊断、分类审核、图谱、Schema 的 views、components、
  helpers 和对应测试都存在于基线。
- 确认 `api.js`、`store.js`、`menu_access.js` 和本地图标已包含 v0.9 契约。
- 先运行基线契约测试，确认能力在整合前可用。

## 3. 建立 9 域路由和导航

- 新增通用 `DomainWorkspace` 和纯导航配置/投影。
- 将业务组件挂到 9 域对应的嵌套子路由。
- 为每个域实现“第一个可访问子功能”的默认跳转。
- 为 19 个历史地址增加保留 query 的兼容 redirect。
- 更新全局守卫、401 清理、默认首页和聊天工作区 meta。
- 将侧栏收敛为 7 个按权限显示的业务域。

## 4. 整合账户与跨页面入口

- 新增账户资料和密码维护子功能。
- 将 API Key 页面挂到账户设置。
- 顶部用户菜单提供账户设置和退出，移除布局中的重复密码弹窗。
- 更新 Dashboard、分类审核、目录、权限和库管理等内部链接到新路由。

## 5. 合并样式

- 提取恢复模块缺失样式，不覆盖当前登录和主题基线。
- 增加域标签、嵌套工作区、账户资料的稳定尺寸和响应式规则。
- 修正聊天、表格、抽屉和治理页面在嵌套工作区中的高度/溢出。
- 扫描颜色和选择器，避免全局规则意外覆盖。

## 6. 自动化验证

依次执行：

```powershell
node --test (Get-ChildItem admin-ui -Recurse -Filter *.test.mjs |
  ForEach-Object FullName)
.\.venv\Scripts\python.exe -m pytest tests/test_console_ui_selection.py
git diff --check
```

新增或更新测试覆盖：

- 9 域菜单和细粒度权限矩阵。
- 域根默认跳转及 19 个历史地址 query 保留。
- 组织身份初始化、退出和 401 清理。
- 账户资料、密码和 API Key 入口。
- 已恢复 v0.9 模块原有 API、安全、状态 fence 和 stale response 契约。

## 7. 浏览器验收

- 启动本地可访问的前端/应用服务器，不占用已有端口。
- 使用 Playwright 检查 1440x900 和 390x844。
- 检查登录页、每个可见功能域、所有子功能标签、账户设置和旧地址跳转。
- 检查截图、页面级横向溢出、重叠、空白画布、404 资源和控制台错误。
- 对图谱 canvas 做非空像素检查并确认基本交互可用。

## 8. 最终质量门

- 按 `trellis-check` 对 `admin-ui` 和后端挂载边界做完整检查。
- 对照 PRD 逐项确认 19 项能力无遗漏、权限无放宽、旧路由可回退。
- 仅提交本任务修改；无关未跟踪文件保持不动。

## 风险文件与回滚点

- `admin-ui/src/app.js`：路由和全局守卫，先用纯路由测试保护。
- `admin-ui/src/menu_access.js`：权限投影，必须先完成矩阵测试。
- `admin-ui/src/api.js`：只做已验收 API 段落合并，不改公共 request 语义。
- `admin-ui/src/views/Layout.js`：只改域菜单和账户入口。
- `admin-ui/style.css`：最后合并，视觉检查失败时可独立回滚样式步骤。
