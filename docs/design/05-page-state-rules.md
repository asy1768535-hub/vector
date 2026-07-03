# 05 — 页面状态规则

每个页面必须处理的 10 种状态：

## 1. 正常数据

- 数据加载完成后正常渲染
- 表格、表单、卡片等组件按设计规范展示

## 2. 首次加载

- 页面挂载后、API 返回前显示加载状态
- 表格: `v-loading="loading"` 指令
- 非表格: 条件渲染 loading 占位或 `el-skeleton`
- 禁止完全白屏

## 3. 空数据

- 使用 `el-empty` 组件
- 提供明确的空数据描述（如 "暂无文档，请先上传"）
- 空数据时操作按钮仍可用（如 "新建"、"上传"）

## 4. 请求失败

- 使用 `ElMessage.error()` 显示中文错误提示
- 错误信息通过 `humanizeApiError()` / `humanizeFetchError()` 统一处理
- 422 数组转为字段级中文提示
- 不显示 `[object Object]` 或英文错误码
- 失败后保留上一次成功的数据（如有）

## 5. 无权限

- 路由守卫拦截并跳转 `/dashboard` + warning 提示
- 页面内操作（删除、编辑等）按钮 `disabled` + tooltip
- 不允许仅用 CSS 隐藏（需配合 `v-if` 移除 DOM）

## 6. 控件禁用

- 按钮: `disabled` 属性 + 灰色样式
- 输入框: `disabled` 属性
- 复选框/单选框: `disabled` 属性
- 禁用状态需有明确的视觉区别（降低透明度或灰色）

## 7. 长文本

- 表格列: `text-overflow: ellipsis` + `show-overflow-tooltip`
- 卡片/列表标题: 单行省略
- 弹窗内容: 允许换行, 必要时 `word-break: break-word`
- Markdown 区域: 自动换行

## 8. 窄屏 (< 1200px)

- 侧栏自动折叠
- 表格水平滚动
- 筛选栏换行
- 弹窗宽度自适应 (`width: 90%` 或 `max-width`)
- **当前状态**: 登录页有 `@media (max-width: 860px)` 断点, 其余页面待补充

## 9. 登录失效

- 401 响应触发 `onUnauthorized` handler
- 3 秒抖动窗口内只跳转一次
- 跳转至 `/login?redirect=<原路径>`
- 登录成功后恢复到原路径

## 10. 重复点击与请求进行中

- 提交按钮: `loading` 属性 + `disabled` 隐式防抖
- Chat 发送: `loading.value` 守卫
- 导入: `uploading` 状态守卫
- 不额外引入 debounce/throttle 库（用原生 `loading` / `disabled`）

## 实现检查表

| 页面 | 加载 | 空数据 | 失败 | 无权限 | 长文本 | 窄屏 | 防抖 |
|---|---|---|---|---|---|---|---|
| Login | ✅ | - | ✅ | - | - | ✅ | ✅ |
| Dashboard | ✅ | - | ✅ | ✅ | - | ❌ | - |
| Chat | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| Documents | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ❌ | ✅ |
| Search | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | ✅ |
| Import | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| ApiKeys | ✅ | ✅ | ⚠️ | - | ✅ | ❌ | ✅ |
| Users | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| Libraries | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | ✅ |
| Permissions | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | ✅ |
| Jobs | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | ✅ |
| RuntimeStatus | ✅ | - | ⚠️ | ✅ | ✅ | ❌ | ✅ |
| Audit | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | - |
| ChatLogs | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | - |

图例: ✅ 已处理 | ⚠️ 部分处理 | ❌ 未处理 | - 不适用
