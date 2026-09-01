# 页面改造索引

按推荐改造顺序排列。每个页面列出当前状态、改造要点、依赖的 API 和风险。

---

## 1. Layout.js — 全局外壳

**当前状态**: 侧栏+顶栏+router-view, 三主题支持, 折叠功能
**改造要点**:
- 顶栏高度从 60px → 64px
- 侧栏宽度明确为 224px
- 菜单排序确认 (普通用户功能在上, 管理员在下)
- Active 指示条精修 (浅绿底 + 左边框)
- 移除 Element Plus dark css-vars 依赖 (由三主题自行管理)
**依赖 API**: `updateMe`, `logout`
**风险**: 改动影响所有 14 页

---

## 2. Chat.js — 智能问答

**当前状态**: 三栏布局, SSE 流式, Markdown, 会话历史
**改造要点**:
- 会话历史面板 300px 固定宽
- 气泡精修 (用户绿底白字 / AI 白底黑字)
- 空状态优化
- 输入栏底部固定
**依赖 API**: `listChatLibraries`, `streamChatMessage`, `listChatConversations`, `getChatConversationMessages`, `archiveChatConversation`, `deleteChatConversation`
**风险**: 流式逻辑复杂, 改动需保持 RAF 队列 + AbortController 功能

---

## 3. KnowledgeCatalog.js — 文档与知识资产管理

**当前状态**: 库选择器 + 统计卡片 + 文档表格 + 编辑弹窗
**改造要点**:
- 统计卡片改为横排指标 (不套卡片)
- 表格样式统一
- 弹窗宽度和表单布局规范化
**依赖 API**: `listLibraries`, `listDocuments`, `libraryStats`, `ingestDocument`, `updateDocument`, `deleteDocument`
**风险**: 低

---

## 4. Search.js — 数据检索

**当前状态**: 库选择器 + 查询输入 + FAQ 标签 + 结果表格
**改造要点**:
- 筛选栏统一为单行布局
- FAQ 标签样式精修
- 结果表格添加空状态和加载骨架
**依赖 API**: `listLibraries`, `listLibraryFaqs`, `queryLibrary`
**风险**: 低

---

## 5. Import.js — 导入数据

**当前状态**: 双模式 (新增/替换) + 队列上传 + 替换文档搜索
**改造要点**:
- 左右栏比例优化
- 替换模式下拉框精修
- 队列状态标签样式统一
**依赖 API**: `listLibraries`, `listDocuments`, `importFile`
**风险**: 替换模式下拉框 CSS 易受 Element Plus 升级影响

---

## 6. ApiKeys.js — API Key 管理

**当前状态**: 表格 + 创建弹窗 + 一次性明文展示
**改造要点**:
- 一次性明文弹窗样式精修
- 复制按钮视觉反馈
**依赖 API**: `listApiKeys`, `createApiKey`, `revokeApiKey`
**风险**: 低

---

## 7. Dashboard.js — 概览

**当前状态**: 用户信息卡片 + 服务健康 + 使用提示
**改造要点**:
- 健康状态用标签而非完整表格
- 嵌入/重排/Qdrant 状态用图标+文字
- 移除使用提示中的 emoji
**依赖 API**: `health`
**风险**: 低

---

## 8. Users.js — 用户管理

**当前状态**: 表格 + 创建弹窗 (含授权) + 重置密码弹窗
**改造要点**:
- 创建弹窗表单布局优化
- 授权多选框宽度统一
- 状态标签颜色规范
**依赖 API**: `listUsers`, `listLibraries`, `createUser`, `updateUser`, `disableUser`, `adminResetUserPassword`, `grantPerms`
**风险**: 授权逻辑较复杂 (逐库循环, 部分失败提示)

---

## 9. Libraries.js — 库管理

**当前状态**: 表格 + 创建/编辑双弹窗 + FAQ 管理弹窗
**改造要点**:
- 库列表表格列简化 (向量参数折叠)
- FAQ 管理弹窗表格精修
- 操作按钮分组
**依赖 API**: 10 个 (最多)
**风险**: FAQ 管理弹窗嵌套层级深

---

## 10. Permissions.js — 权限矩阵

**当前状态**: 用户选择器 + 库×动作矩阵
**改造要点**:
- 矩阵表格样式规范
- 保存按钮位置固定
- 超管用户提示优化
**依赖 API**: `listUsers`, `listLibraries`, `listUserPerms`, `grantPerms`, `revokePerms`
**风险**: diff 计算逻辑较复杂

---

## 11. Jobs.js — 任务监控

**当前状态**: 状态筛选 + 统计标签 + 任务表格 + 重试/重置
**改造要点**:
- 统计标签改为横排指标
- 状态标签颜色规范
- 重试按钮仅图标
**依赖 API**: `listMonitoredTasks`, `monitoredTaskStats`, `retryMonitoredTasks`, `resetFailedJobs`
**风险**: 低

---

## 12. RuntimeStatus.js — 运行状态

**当前状态**: 服务状态表格 + 嵌入/清理统计 + 重建进度
**改造要点**:
- 在线/离线状态标签
- 心跳时间人性化显示
- 重建进度条样式
**依赖 API**: `operationsStatus`
**风险**: 低

---

## 13. Audit.js — 审计日志

**当前状态**: 展开行表格 + 中文摘要 + JSON 展示 + 搜索
**改造要点**:
- 时间格式统一
- JSON 展示区等宽字体
- 动作标签颜色规范
**依赖 API**: `listAudit`
**风险**: 低

---

## 14. ChatLogs.js — 问答日志

**当前状态**: 筛选栏 + 展开行表格
**改造要点**:
- 筛选栏统一为单行布局
- 展开行中的引用来源样式
**依赖 API**: `listLibraries`, `adminListChatLogs`
**风险**: 低

---

## 15. Login.js — 登录页

**当前状态**: 双栏布局 (品牌区+表单区), 装饰圆球已移除
**改造要点**:
- 品牌面板背景使用墨绿纯色 (无渐变)
- 表单区卡片居中
- 窄屏适配保留
**依赖 API**: `login`
**风险**: 低

---

## 16. 全局响应式与回归验收

**当前状态**: 仅 Login.js 有窄屏断点
**改造要点**:
- 补充 `<1200px` 侧栏自动折叠
- 补充 `<768px` 表格水平滚动
- 所有页面回归测试
**风险**: 可能需要调整 Layout.js 折叠逻辑
