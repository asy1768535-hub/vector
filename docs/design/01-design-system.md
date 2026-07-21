# 01 — 设计系统（Design Tokens）

## 颜色变量

以 CSS 自定义属性实现，定义在 `:root` 或 `[data-theme="enterprise"]` 下。

### 品牌色

| Token | 值 | 用途 |
|---|---|---|
| `--color-primary` | `#176B57` | 主按钮、链接、选中态、侧栏激活指示条 |
| `--color-primary-hover` | `#125846` | 主按钮 hover |
| `--color-primary-soft` | `#EAF4F0` | 浅绿底（侧栏激活项、成功状态背景） |

### 功能色

| Token | 值 | 用途 |
|---|---|---|
| `--color-info` | `#3976C5` | 信息提示、引用链接、处理中状态 |
| `--color-info-soft` | `#EEF5FF` | 信息提示背景 |
| `--color-success` | `#21875B` | 成功状态 |
| `--color-warning` | `#D98218` | 警告状态 |
| `--color-danger` | `#D94848` | 危险按钮、删除操作、失败状态 |

### 文字色

| Token | 值 | 用途 |
|---|---|---|
| `--color-text-primary` | `#17212B` | 主文字（标题、正文） |
| `--color-text-regular` | `#3F4A56` | 常规文字（表格内容、表单项） |
| `--color-text-secondary` | `#66717D` | 次要文字（描述、辅助信息、placeholder） |

### 边框与背景

| Token | 值 | 用途 |
|---|---|---|
| `--color-border` | `#DCE3E8` | 主边框（卡片、表格、输入框） |
| `--color-border-light` | `#E9EEF2` | 细分隔线（表内分隔、列表间） |
| `--color-page-bg` | `#F5F7F8` | 页面背景（内容区底色） |
| `--color-surface` | `#FFFFFF` | 面板/卡片/表格/输入框背景 |

## 尺寸变量

| Token | 值 | 用途 |
|---|---|---|
| `--header-height` | `64px` | 顶栏高度（固定） |
| `--sidebar-width` | `224px` | 侧栏展开宽度 |
| `--sidebar-collapsed` | `64px` | 侧栏收起宽度 |
| `--chat-history-width` | `300px` | Chat 会话历史面板宽度 |
| `--page-padding-x` | `20px` ~ `24px` | 页面内容水平外边距 |
| `--section-gap` | `16px` | 区域间距 |
| `--control-height` | `36px` ~ `40px` | 表单控件高度 |
| `--btn-height` | `36px` | 常规按钮高度 |

## 圆角

| Token | 值 | 用途 |
|---|---|---|
| `--radius-sm` | `4px` | 标签、小徽章 |
| `--radius-md` | `6px` | 输入框、下拉框、侧栏菜单项 |
| `--radius-lg` | `8px` | 卡片、表格、弹窗、气泡 |

**禁止超过 8px 圆角。**

## 字体

### 字号

| 用途 | 大小 |
|---|---|
| 页面标题 | `22px` |
| 区域标题 | `16px` ~ `18px` |
| 正文 | `14px` |
| 表格内容 | `13px` ~ `14px` |
| 辅助信息 | `12px` |

### 字重

以 `400`（常规）、`500`（中等）、`600`（半粗）为主。
**禁止**使用随视口宽度缩放的字体（如 `vw` 单位）。

### 字体族

`Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`

## 阴影

- 卡片/面板：`0 1px 3px rgba(0,0,0,0.04)` — 极轻阴影，仅在需要区分层级时使用
- 弹窗：使用 Element Plus 默认
- **禁止**大阴影、彩色阴影、发光效果

## 图标

- 全部 27 个图标本地 SVG，`<local-icon icon="prefix:name">`
- **禁止** Iconify CDN、Font Awesome、其他图标服务
- 新增图标需在 `src/icons.js` 中注册 SVG path

## 间距

- 表单标签与控件之间：`8px` ~ `12px`
- 表格行高：`52px` ~ `60px`
- 卡片内边距：`16px` ~ `20px`
- 弹窗宽度：`480px` ~ `640px`（视内容复杂度）

## 表格

- 表头：浅灰背景 (`--color-page-bg` 或 `#F5F7F8`)
- 行分隔：`1px solid var(--color-border-light)`
- 长文本：`text-overflow: ellipsis` + `el-tooltip`
- 操作列：右对齐, 按钮紧凑排列 (`size="small"`)
