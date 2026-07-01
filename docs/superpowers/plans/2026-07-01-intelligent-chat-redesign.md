# Intelligent Chat Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将登录后的管理后台改为固定绿色企业主题，并把智能问答页重构为 224px 导航、290px 会话历史和自适应问答区组成的三栏工作台。

**Architecture:** 保留现有零构建 Vue 3、Vue Router、Element Plus、API 和权限结构，只修改 `Layout.js`、`Chat.js`、`style.css` 及本地资源。先用静态结构测试锁定单主题、布局类名和禁止新增的伪功能，再逐步清理主题代码、重构 DOM、补齐响应式样式。

**Tech Stack:** Vue 3 ESM、Vue Router、Element Plus、本地 SVG 图标、原生 CSS、Node.js `node:test`/`assert`

---

## 开始前

- 设计说明：`docs/superpowers/specs/2026-07-01-intelligent-chat-redesign-design.md`
- 视觉参考：`D:\AI_work\job\向量库图片参考\智能问答.png`
- 应用标识源文件：`D:\AI_work\job\向量库图片参考\9cdef80a-f621-497d-b7f9-bcf8c30a81f0-transparent.png`
- 工作区当前已有未提交改动。不得回滚、覆盖或格式化与本任务无关的文件。
- 不修改 `app/`、数据库、API 或聊天后端。
- 基线命令：在 `admin-ui` 目录运行 `node --test`。
- 当前基线：9 个测试文件通过。

## 文件职责

- `admin-ui/src/views/Layout.js`：全局顶栏、侧栏、权限菜单和用户菜单。
- `admin-ui/src/views/Chat.js`：会话历史、知识库选择、消息流、引用来源和输入区。
- `admin-ui/style.css`：唯一企业主题、全局框架、聊天页及响应式样式。
- `admin-ui/src/app.js`：应用入口；移除主题初始化。
- `admin-ui/src/icons.js`：本地 SVG 图标；移除主题专用图标。
- `admin-ui/assets/app-brand-mark.png`：新应用方形标识。
- `admin-ui/single_theme.test.mjs`：单主题和主题死代码回归测试。
- `admin-ui/layout_redesign.test.mjs`：全局框架结构与素材引用测试。
- `admin-ui/chat_redesign.test.mjs`：聊天页三栏结构、功能保留和禁止项测试。
- `admin-ui/ui1_1.test.mjs`：保留窄屏与预览登录检查，删除旧三主题假断言。

### Task 1: 固定企业主题并删除主题切换

**Files:**
- Create: `admin-ui/single_theme.test.mjs`
- Modify: `admin-ui/src/app.js`
- Modify: `admin-ui/index.html`
- Modify: `admin-ui/src/views/Layout.js`
- Modify: `admin-ui/src/icons.js`
- Modify: `admin-ui/style.css`
- Modify: `admin-ui/ui1_1.test.mjs`
- Delete: `admin-ui/src/theme.js`
- Delete: `admin-ui/theme.test.mjs`
- Delete: `admin-ui/vendor/element-plus.dark-css-vars.css`

- [ ] **Step 1: 写单主题失败测试**

创建 `admin-ui/single_theme.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const index = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const icons = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');

assert.doesNotMatch(app, /theme\.js/);
assert.doesNotMatch(index, /element-plus\.dark-css-vars\.css/);
assert.doesNotMatch(layout, /\bTHEMES\b|\bcurrentTheme\b|\bapplyTheme\b|onThemeCommand|themeOptions/);
assert.doesNotMatch(layout, /mdi:palette-outline|theme-switcher-item/);
assert.match(css, /:root\s*\{/);
assert.doesNotMatch(css, /\[data-theme=/);
assert.doesNotMatch(css, /ai-dark|government|theme-switcher/);
assert.doesNotMatch(icons, /mdi:palette-outline|mdi:check|mdi:weather-night|mdi:white-balance-sunny/);

console.log('single theme cleanup test passed');
```

- [ ] **Step 2: 运行测试并确认失败**

在 `admin-ui` 目录运行：

```powershell
node --test single_theme.test.mjs
```

预期：失败，首先命中 `app.js` 的 `theme.js` 引用或 `Layout.js` 的主题导入。

- [ ] **Step 3: 删除运行时主题逻辑**

在 `admin-ui/src/app.js` 删除：

```js
import './theme.js';
```

在 `admin-ui/index.html` 删除：

```html
<link rel="stylesheet" href="./vendor/element-plus.dark-css-vars.css" />
```

在 `admin-ui/src/views/Layout.js` 删除主题导入：

```js
import { THEMES, currentTheme, applyTheme } from '../theme.js';
```

删除 `setup()` 中：

```js
const themeOptions = Object.entries(THEMES).map(([key, t]) => ({ key, ...t }));
function onThemeCommand(key) { applyTheme(key); }
```

从 `return` 对象删除：

```js
currentTheme, themeOptions, onThemeCommand,
```

删除顶栏中包含 `mdi:palette-outline` 的整个主题 `el-dropdown`。不得删除用户菜单。

- [ ] **Step 4: 将 CSS 固化为唯一企业主题**

在 `admin-ui/style.css` 执行以下精确清理：

1. 将开头选择器从：

```css
:root, [data-theme="enterprise"] {
```

改为：

```css
:root {
```

2. 完整删除 `[data-theme="ai-dark"] { ... }` 和 `[data-theme="government"] { ... }` 两个变量块。
3. 完整删除 `.theme-switcher-item`、`.theme-swatch`、`.theme-label`。
4. 将企业侧栏选择器移除主题前缀，例如：

```css
.layout-aside {
    background: #FFFFFF;
    border-right: 1px solid var(--color-border);
    box-shadow: none;
}

.layout-aside .logo {
    color: var(--color-text-primary);
    border-bottom: 1px solid var(--color-border);
}

.layout-aside .logo local-icon {
    color: var(--color-primary);
}
```

5. 对该企业侧栏块中的其余 `[data-theme="enterprise"] ` 前缀执行同样处理。
6. 完整删除 AI Dark 和 Government 的专属精修块。
7. 保留 `.layout-aside .el-menu-item` 的通用过渡样式。

- [ ] **Step 5: 删除主题模块、测试和专用图标**

删除：

```text
admin-ui/src/theme.js
admin-ui/theme.test.mjs
admin-ui/vendor/element-plus.dark-css-vars.css
```

从 `admin-ui/src/icons.js` 的 `ICONS` 对象删除以下四项及其 path：

```text
mdi:palette-outline
mdi:check
mdi:weather-night
mdi:white-balance-sunny
```

- [ ] **Step 6: 让旧综合测试检查真实代码**

在 `admin-ui/ui1_1.test.mjs` 顶部增加：

```js
import { readFileSync } from 'node:fs';
```

将第 4 节替换为：

```js
// 4. 固定企业主题，不保留运行时主题切换
const appSource = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const styleSource = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

ok('入口不加载主题模块', !appSource.includes('theme.js'));
ok('顶栏不含主题切换', !layoutSource.includes('themeOptions'));
ok('CSS 不含暗色主题', !styleSource.includes('ai-dark'));
ok('CSS 不含政务主题', !styleSource.includes('government'));
ok('企业主色保留', styleSource.includes('--color-primary: #176B57'));
```

- [ ] **Step 7: 运行主题相关测试**

在 `admin-ui` 目录运行：

```powershell
node --test single_theme.test.mjs ui1_1.test.mjs
```

预期：两个测试文件全部通过。

- [ ] **Step 8: 提交单主题清理**

```powershell
git add admin-ui/index.html admin-ui/src/app.js admin-ui/src/views/Layout.js admin-ui/src/icons.js admin-ui/style.css admin-ui/ui1_1.test.mjs admin-ui/single_theme.test.mjs admin-ui/src/theme.js
git commit -m "refactor(ui): remove runtime theme switching"
```

`admin-ui/theme.test.mjs` 和 `admin-ui/vendor/element-plus.dark-css-vars.css` 在计划编写时尚未被 Git 跟踪，删除后不要把不存在的路径传给 `git add`。

### Task 2: 接入品牌标识并重构全局框架

**Files:**
- Create: `admin-ui/assets/app-brand-mark.png`
- Create: `admin-ui/layout_redesign.test.mjs`
- Modify: `admin-ui/src/views/Layout.js`
- Modify: `admin-ui/style.css`

- [ ] **Step 1: 写全局框架失败测试**

创建 `admin-ui/layout_redesign.test.mjs`：

```js
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const asset = new URL('./assets/app-brand-mark.png', import.meta.url);

assert.ok(existsSync(asset), 'expected app brand mark');
assert.match(layout, /app-brand-mark\.png/);
assert.match(layout, /class="app-brand-mark"/);
assert.match(layout, /class="header-breadcrumb"/);
assert.match(layout, /layout-main--chat/);
assert.match(layout, /currentPath === '\/chat'/);
assert.match(css, /\.app-brand-mark\s*\{/);
assert.match(css, /\.header-breadcrumb\s*\{/);
assert.match(css, /\.layout-main--chat\s*\{/);
assert.match(css, /\.layout-header\s*\{[\s\S]*height:\s*64px/);

console.log('global layout redesign test passed');
```

- [ ] **Step 2: 运行测试并确认失败**

在 `admin-ui` 目录运行：

```powershell
node --test layout_redesign.test.mjs
```

预期：失败，提示缺少 `app-brand-mark.png`。

- [ ] **Step 3: 复制已批准的品牌素材**

在仓库根目录运行：

```powershell
Copy-Item -LiteralPath 'D:\AI_work\job\向量库图片参考\9cdef80a-f621-497d-b7f9-bcf8c30a81f0-transparent.png' -Destination '.\admin-ui\assets\app-brand-mark.png'
```

不要修改、压缩或重新导出源图。

- [ ] **Step 4: 修改侧栏品牌和顶栏面包屑**

在 `Layout.js` 中将原 `.logo` 内容替换为：

```html
<div class="logo">
    <img class="app-brand-mark" src="./assets/app-brand-mark.png" alt="" />
    <span v-show="!effectiveCollapsed">向量知识库</span>
</div>
```

保留顶栏折叠按钮，将原 `header-title` 替换为：

```html
<div class="header-breadcrumb">
    <span class="header-title">{{ pageTitle }}</span>
    <span class="header-breadcrumb-separator">/</span>
    <span class="header-product-name">向量知识库</span>
</div>
```

将主内容节点改为：

```html
<el-main class="layout-main" :class="{ 'layout-main--chat': currentPath === '/chat' }">
    <router-view />
</el-main>
```

- [ ] **Step 5: 添加全局框架样式**

在 `style.css` 的布局区域确保最终包含：

```css
.layout-header {
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    background: var(--app-header-bg);
    border-bottom: 1px solid var(--app-header-border);
}

.logo {
    height: 64px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 18px;
    font-size: 18px;
    font-weight: 700;
}

.app-brand-mark {
    width: 34px;
    height: 34px;
    object-fit: contain;
    flex: none;
}

.header-breadcrumb {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
}

.header-title {
    color: var(--app-text);
    font-size: 18px;
    font-weight: 650;
}

.header-breadcrumb-separator,
.header-product-name {
    color: var(--app-text-secondary);
    font-size: 14px;
}

.layout-main--chat {
    padding: 20px;
    overflow: hidden;
}
```

保留现有用户头像、用户下拉、修改密码、退出登录和侧栏折叠样式。

- [ ] **Step 6: 运行全局框架测试**

```powershell
node --test layout_redesign.test.mjs single_theme.test.mjs
```

预期：两个测试文件全部通过。

- [ ] **Step 7: 提交全局框架**

```powershell
git add admin-ui/assets/app-brand-mark.png admin-ui/layout_redesign.test.mjs admin-ui/src/views/Layout.js admin-ui/style.css
git commit -m "feat(ui): align global shell with knowledge workspace"
```

### Task 3: 重构智能问答 DOM，保持现有行为

**Files:**
- Create: `admin-ui/chat_redesign.test.mjs`
- Modify: `admin-ui/src/views/Chat.js`

- [ ] **Step 1: 写聊天结构失败测试**

创建 `admin-ui/chat_redesign.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');

assert.match(chat, /chat-history-title-row/);
assert.match(chat, /chat-new-button/);
assert.match(chat, /chat-toolbar/);
assert.match(chat, /chat-toolbar-label/);
assert.match(chat, /chat-message-content/);
assert.match(chat, /chat-avatar--ai/);
assert.match(chat, /chat-avatar--user/);
assert.match(chat, /chat-input-shell/);

for (const required of [
    'newChat', 'selectConversation', 'archiveConv', 'deleteConv', 'send',
    'streamChatMessage', 'renderMarkdown', 'chat-sources',
]) {
    assert.ok(chat.includes(required), `expected existing behavior: ${required}`);
}

for (const forbidden of [
    '上传附件', '检索设置', '点赞', '点踩', '查看原文', '消息中心', '帮助中心',
]) {
    assert.ok(!chat.includes(forbidden), `unexpected fake feature: ${forbidden}`);
}

console.log('chat structure redesign test passed');
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
node --test chat_redesign.test.mjs
```

预期：失败，提示缺少 `chat-history-title-row`。

- [ ] **Step 3: 重组会话历史栏**

保留 `convsForLib`、归档、删除和会话循环。将历史栏顶部改为：

```html
<div class="chat-history-title-row">
    <div>
        <div class="chat-history-panel-title">会话历史</div>
        <div class="chat-history-panel-subtitle">最近的知识问答记录</div>
    </div>
    <el-button class="chat-new-button" circle :disabled="!currentSlug"
               aria-label="新建会话" title="新建会话" @click="newChat">
        <local-icon icon="mdi:plus"></local-icon>
    </el-button>
</div>
```

从历史栏删除知识库选择器和原整行“新建会话”按钮。历史列表、空状态、会话标题、归档和删除按钮保持原有事件绑定。

- [ ] **Step 4: 在问答主区添加知识库工具条**

在 `.chat-main` 内、警告和消息区之前加入：

```html
<div class="chat-toolbar">
    <span class="chat-toolbar-label">知识库</span>
    <el-select v-model="currentSlug" placeholder="选择知识库" size="default"
               class="chat-library-select" @change="onLibChange">
        <el-option v-for="l in libs" :key="l.slug" :label="l.name" :value="l.slug" />
    </el-select>
</div>
```

不得改变 `onLibChange()`、`loadLibs()` 或任何 API 调用。

- [ ] **Step 5: 重组消息行但保留内容与引用**

将每条消息组织为头像加内容列：

```html
<div v-for="(m, i) in messages" :key="i" class="chat-message-row"
     :class="m.role === 'user' ? 'chat-message--user' : 'chat-message--ai'">
    <div v-if="m.role === 'ai'" class="chat-avatar chat-avatar--ai">AI</div>
    <div class="chat-message-content">
        <div class="chat-bubble" :class="{
            'chat-bubble--user': m.role === 'user',
            'chat-bubble--ai': m.role === 'ai',
            'chat-bubble--error': m.error,
        }">
            <div v-if="m.statusText" class="chat-status">{{ m.statusText }}</div>
            <div v-if="m.role === 'user'" class="chat-user-text">{{ m.text }}</div>
            <div v-if="m.role === 'ai'" class="chat-ai-label">智能助手</div>
            <div v-if="m.role === 'ai'" class="chat-markdown"
                 v-html="renderMarkdown(m.text) + (m.cursor ? '<span class=\\'chat-cursor\\'>|</span>' : '')"></div>
        </div>
        <el-collapse v-if="m.role === 'ai' && m.sources && m.sources.length" class="chat-sources">
            <el-collapse-item :title="'引用来源（' + m.sources.length + '）'">
                <div v-for="(s, si) in m.sources" :key="si" class="chat-source-item">
                    <div class="chat-source-header">
                        <span class="chat-source-num">[{{ si + 1 }}]</span>
                        <span class="chat-source-title">{{ s.title || '(无标题)' }}</span>
                        <el-tag size="small" :type="s.score >= 0.7 ? 'success' : s.score >= 0.5 ? 'warning' : 'info'">
                            {{ fmtScore(s.score) }}
                        </el-tag>
                    </div>
                    <div class="chat-source-content">{{ s.content }}</div>
                </div>
            </el-collapse-item>
        </el-collapse>
    </div>
    <div v-if="m.role === 'user'" class="chat-avatar chat-avatar--user">我</div>
</div>
```

- [ ] **Step 6: 给输入区增加结构类，不改变发送行为**

将输入区外层类改为：

```html
<div class="chat-input-bar">
    <div class="chat-input-shell">
        <el-input v-model="input" type="textarea" :rows="2" resize="none"
                  placeholder="继续提问，或输入问题..."
                  @keydown.enter.exact.prevent="send"
                  class="chat-input" />
        <el-button type="primary" :loading="loading" :disabled="!currentSlug"
                   @click="send" class="chat-send-btn">
            发送
        </el-button>
    </div>
</div>
```

不得新增附件、设置、评价或查看原文按钮。

- [ ] **Step 7: 运行聊天结构测试**

```powershell
node --test chat_redesign.test.mjs markdown.test.mjs
```

预期：结构测试通过，Markdown 的 30 项测试继续通过。

- [ ] **Step 8: 提交聊天结构**

```powershell
git add admin-ui/chat_redesign.test.mjs admin-ui/src/views/Chat.js
git commit -m "feat(ui): restructure intelligent chat workspace"
```

### Task 4: 实现三栏视觉和响应式布局

**Files:**
- Modify: `admin-ui/chat_redesign.test.mjs`
- Modify: `admin-ui/style.css`

- [ ] **Step 1: 增加布局失败断言**

在 `chat_redesign.test.mjs` 读取 CSS：

```js
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
```

在输出前增加：

```js
assert.match(css, /\.chat-history-panel\s*\{[\s\S]*width:\s*290px/);
assert.match(css, /\.chat-toolbar\s*\{/);
assert.match(css, /\.chat-message-content\s*\{/);
assert.match(css, /\.chat-avatar--ai\s*\{/);
assert.match(css, /\.chat-input-shell\s*\{/);
assert.match(css, /@media\s*\(max-width:\s*899px\)/);
assert.match(css, /@media\s*\(max-width:\s*899px\)[\s\S]*flex-direction:\s*column/);
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
node --test chat_redesign.test.mjs
```

预期：失败，提示会话栏不是 290px 或缺少工具条样式。

- [ ] **Step 3: 替换聊天布局样式**

在 `style.css` 中保留 Markdown 规则，替换从“Chat 三栏布局”到“Chat Markdown”之前的聊天布局样式。最终至少包含：

```css
.chat-wrap {
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    background: var(--app-panel-bg);
    border: 1px solid var(--app-border);
    border-radius: 8px;
}

.chat-history-panel {
    width: 290px;
    flex: none;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: #fff;
    border-right: 1px solid var(--app-border);
}

.chat-history-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 20px 18px 16px;
    border-bottom: 1px solid var(--app-border-light);
}

.chat-history-panel-title {
    padding: 0;
    color: var(--app-text);
    font-size: 17px;
    font-weight: 700;
}

.chat-history-panel-subtitle {
    margin-top: 4px;
    color: var(--app-text-muted);
    font-size: 12px;
}

.chat-new-button {
    flex: none;
    color: var(--app-primary);
    border-color: var(--app-border);
}

.chat-history-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 10px;
}

.chat-history-item {
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 52px;
    margin-bottom: 4px;
    padding: 10px 11px;
    border-left: 3px solid transparent;
    border-radius: 0 6px 6px 0;
    cursor: pointer;
}

.chat-history-item.is-active {
    padding-left: 11px;
    color: var(--app-primary);
    background: var(--app-primary-soft);
    border-left-color: var(--app-primary);
}

.chat-main {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: #fff;
}

.chat-toolbar {
    min-height: 58px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--app-border-light);
}

.chat-toolbar-label {
    color: var(--app-text);
    font-size: 14px;
    font-weight: 650;
}

.chat-library-select {
    width: min(320px, 55%);
}

.chat-messages {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 24px 28px;
    background: #fbfcfc;
}

.chat-message-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 22px;
}

.chat-message--user {
    justify-content: flex-end;
}

.chat-message-content {
    width: min(760px, calc(100% - 56px));
}

.chat-avatar {
    width: 38px;
    height: 38px;
    flex: none;
    display: grid;
    place-items: center;
    border-radius: 50%;
    font-size: 12px;
    font-weight: 700;
}

.chat-avatar--ai {
    color: #fff;
    background: var(--app-primary);
}

.chat-avatar--user {
    color: var(--app-primary);
    background: var(--app-primary-soft);
    border: 1px solid #cfe3dc;
}

.chat-bubble {
    max-width: none;
    min-width: 120px;
}

.chat-bubble--user {
    margin-left: auto;
    padding: 12px 16px;
    color: var(--app-text);
    background: var(--app-primary-soft);
    border: 1px solid #dcebe6;
    border-radius: 8px;
}

.chat-bubble--ai {
    padding: 16px 18px;
    color: var(--app-text);
    background: #fff;
    border: 1px solid var(--app-border);
    border-radius: 8px;
}

.chat-sources {
    width: 100%;
    max-width: none;
    margin-top: 10px;
}

.chat-input-bar {
    padding: 14px 18px 18px;
    background: #fff;
    border-top: 1px solid var(--app-border-light);
}

.chat-input-shell {
    display: flex;
    align-items: flex-end;
    gap: 12px;
    padding: 8px;
    background: #fff;
    border: 1px solid var(--app-primary);
    border-radius: 8px;
}

.chat-input {
    flex: 1;
}

.chat-input .el-textarea__inner {
    min-height: 54px;
    padding: 10px 12px;
    border: 0;
    box-shadow: none;
}

.chat-send-btn {
    min-width: 82px;
    height: 42px;
}
```

- [ ] **Step 4: 添加无新增按钮的响应式规则**

在 `style.css` 末尾加入：

```css
@media (max-width: 1199px) {
    .layout-main--chat {
        padding: 14px;
    }

    .chat-history-panel {
        width: 250px;
    }

    .chat-messages {
        padding: 20px;
    }
}

@media (max-width: 899px) {
    .layout-main--chat {
        overflow: auto;
    }

    .chat-wrap {
        height: auto;
        min-height: 100%;
        flex-direction: column;
        overflow: visible;
    }

    .chat-history-panel {
        width: 100%;
        max-height: 230px;
        border-right: 0;
        border-bottom: 1px solid var(--app-border);
    }

    .chat-history-title-row {
        padding: 14px 16px 10px;
    }

    .chat-main {
        min-height: 620px;
    }

    .chat-message-content {
        width: calc(100% - 50px);
    }
}
```

- [ ] **Step 5: 运行聊天和全局布局测试**

```powershell
node --test chat_redesign.test.mjs layout_redesign.test.mjs
```

预期：两个测试文件全部通过。

- [ ] **Step 6: 提交视觉与响应式样式**

```powershell
git add admin-ui/chat_redesign.test.mjs admin-ui/style.css
git commit -m "style(ui): implement responsive three-column chat layout"
```

### Task 5: 删除已确认的冗余素材并执行完整验证

**Files:**
- Delete: `admin-ui/assets/company-logo-clean-v3.png`
- Delete: `admin-ui/assets/company-logo-clean-v4-preview.png`
- Delete: `admin-ui/assets/company-logo-clean-v4.png`
- Delete: `admin-ui/assets/company-logo-clean.png`
- Verify: `admin-ui/assets/company-logo.png`
- Verify: `admin-ui/assets/login-bg.png`
- Verify: all files changed in Tasks 1–4

- [ ] **Step 1: 删除未被引用的 Logo 中间稿**

只删除以下四个文件：

```text
admin-ui/assets/company-logo-clean-v3.png
admin-ui/assets/company-logo-clean-v4-preview.png
admin-ui/assets/company-logo-clean-v4.png
admin-ui/assets/company-logo-clean.png
```

必须保留：

```text
admin-ui/assets/company-logo.png
admin-ui/assets/login-bg.png
admin-ui/assets/app-brand-mark.png
```

- [ ] **Step 2: 搜索主题残留**

在仓库根目录运行：

```powershell
Get-ChildItem -LiteralPath '.\admin-ui' -Recurse -File |
    Where-Object { $_.Extension -in @('.js','.mjs','.css','.html') } |
    Select-String -Pattern 'THEMES|currentTheme|applyTheme|data-theme|ai-dark|government|themeOptions|onThemeCommand|palette-outline'
```

预期：无输出。

- [ ] **Step 3: 运行完整前端测试**

在 `admin-ui` 目录运行：

```powershell
node --test
```

预期：所有测试文件通过，失败数为 0。

- [ ] **Step 4: 启动本地预览并验证页面**

在仓库根目录启动静态预览：

```powershell
python -m http.server 5599 --directory admin-ui
```

打开：

```text
http://127.0.0.1:5599/?preview=1#/chat
```

验证以下状态：

1. 1920px 宽：224px 导航、290px 会话栏、剩余区域为问答区。
2. 1199px 宽：导航自动折叠，会话栏约 250px，无横向滚动。
3. 899px 宽：会话栏在上、问答区在下，无新增展开按钮。
4. 顶栏只有原折叠控制与用户菜单，不出现主题、消息或帮助入口。
5. 新品牌标识在 34px 尺寸下清晰且无白底。
6. 普通用户和超级管理员菜单仍按权限显示。
7. 新建、切换、归档、删除、发送、流式回答、引用来源均可操作。
8. 浏览器控制台无错误和缺失资源。

- [ ] **Step 5: 检查改动范围**

```powershell
git status --short
git diff --check
git diff --stat
```

预期：没有空白错误；不得出现 `app/`、API 或数据库文件的新增改动。已有用户改动可能仍显示，提交时只暂存本计划文件。

- [ ] **Step 6: 确认素材清理没有误暂存用户改动**

```powershell
git status --short -- admin-ui/assets/company-logo-clean-v3.png admin-ui/assets/company-logo-clean-v4-preview.png admin-ui/assets/company-logo-clean-v4.png admin-ui/assets/company-logo-clean.png
git diff --cached --name-status
```

这四个文件在计划编写时是未跟踪中间稿，删除后预期不会产生 Git 差异，因此不得运行 `git add admin-ui` 或 `git add admin-ui/assets`。如果执行时它们已经被跟踪，只暂存这四个删除项并提交：

```powershell
git add -u -- admin-ui/assets/company-logo-clean-v3.png admin-ui/assets/company-logo-clean-v4-preview.png admin-ui/assets/company-logo-clean-v4.png admin-ui/assets/company-logo-clean.png
git commit -m "chore(ui): remove obsolete logo drafts"
```

## 完成定义

- 设计说明中的每条验收标准均有对应测试或人工验证结果。
- `node --test` 在 `admin-ui` 目录全绿。
- 主题运行时代码、两套废弃主题和主题入口完全删除。
- 新品牌标识已复制到正式资源目录并被全局框架引用。
- 智能问答桌面端为参考图同构三栏，小屏不新增按钮且功能可访问。
- 除主题切换外，现有按钮、路由、权限、API 和聊天行为没有减少或新增。
