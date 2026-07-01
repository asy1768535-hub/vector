# 文档文件类型图标设计

日期：2026-07-01

## 目标

将文档列表文件名前现有的 `WORD/PDF` 文字徽标替换为与
`D:\AI_work\job\向量库图片参考\文档.png` 一致的折角文件类型图标。

## 视觉规则

- 图标尺寸约为 `26px × 30px`，位于文件名左侧。
- PDF：红色文件图标，内含 `PDF`。
- Word：蓝色文件图标，内含 `W`。
- Excel：绿色文件图标，内含 `XLS`。
- Markdown：深灰文件图标，内含 `MD`。
- TXT：青色文件图标，内含 `TXT`。
- 其他类型：灰色通用文件图标。
- 删除当前宽大的 `WORD/PDF` 圆角文字徽标，不同时保留两套类型标识。
- 文件名继续作为可访问文本，图标自身使用 `aria-hidden`。

## 技术方案

- 复用现有 `admin-ui/src/icons.js` 和 `<local-icon>` 自定义元素。
- 新增本地内联 SVG 图标，不新增 PNG、WebP、字体图标、CDN 或网络请求。
- 在 `admin-ui/src/documents_ui.js` 增加纯函数，将现有 `documentType(row)`
  的结果映射到对应本地图标名。
- `Documents.js` 只替换文件名单元格中的类型徽标，不改变列表、权限、筛选、
  分页、详情和 API 调用。
- `style.css` 只调整文件名单元格中的图标尺寸、对齐和收缩行为。

## 测试

- 覆盖六种文档类型到图标名的映射。
- 验证每个新增图标能返回有效 SVG，包含正确 viewBox，且没有公网 URL。
- 验证文档表格使用 `<local-icon>`，不再渲染 `.documents-file-type` 文字徽标。
- 运行完整 `node --test`、JS/MJS 语法检查和发布安全检查。
- 在 1920px、1199px、899px 下验证图标不变形、不挤压文件名，表格横向滚动行为不变。

## 范围边界

- 不修改后端、API、store、数据库或文档数据结构。
- 不增加外部图标依赖和图片素材。
- 不修改其他页面图标。
- 顺手修正 `documents_redesign.test.mjs` 中重复的 `canInsert` 断言，使第二条真正检查 `canDelete`。
