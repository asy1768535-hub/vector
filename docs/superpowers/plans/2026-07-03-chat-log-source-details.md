# Chat Log Source Details Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace long inline chat log citation excerpts with compact source summary rows and a detail dialog for full source text.

**Architecture:** Keep chat log loading, filters, table columns, pagination, and CSV export unchanged. Add local dialog state in `ChatLogs.js`, update expanded source markup, style the compact rows and dialog content in `style.css`, and update regression tests.

**Tech Stack:** Zero-build Vue 3 ES modules, Element Plus, plain CSS, Node test runner.

## Global Constraints

- Update only `admin-ui/src/views/ChatLogs.js` rendering and local state needed for source details.
- Update only related `.logs-source-*` styles in `admin-ui/style.css`.
- Update existing chat log regression tests.
- Do not change API calls, filters, table columns, pagination, CSV export, or backend code.
- Do not invent metadata. Optional page/location metadata is displayed only when present.
- No inline `style` attributes.

---

### Task 1: Add Source Detail Regression Tests

**Files:**
- Modify: `admin-ui/chat_logs_redesign.test.mjs`

**Interfaces:**
- Consumes: existing `src` and `css` constants in `admin-ui/chat_logs_redesign.test.mjs`.
- Produces: failing tests for compact source summaries and the detail dialog.

- [ ] **Step 1: Add failing tests**

Add these tests near existing source-related assertions:

```js
test('引用来源使用紧凑列表，全文进入详情弹窗', () => {
    assert.ok(src.includes('sourceDetail.open'), 'source detail dialog state');
    assert.ok(src.includes('openSourceDetail(s)'), 'source detail action');
    assert.ok(src.includes('查看详情'), 'source detail button text');
    assert.ok(src.includes('logs-source-detail-dialog'), 'source detail dialog class');
    assert.ok(src.includes('sourceMeta(sourceDetail.item)'), 'dialog source metadata');
    assert.doesNotMatch(src, /<div class="logs-source-text">\{\{ s\.content \}\}<\/div>/);
});

test('引用来源样式支持紧凑摘要和详情正文', () => {
    assert.match(css, /\.logs-source-item\s*\{/);
    assert.match(css, /\.logs-source-title\s*\{[^}]*text-overflow:ellipsis/s);
    assert.match(css, /\.logs-source-actions\s*\{/);
    assert.match(css, /\.logs-source-detail-text\s*\{/);
});
```

- [ ] **Step 2: Verify red**

Run: `node chat_logs_redesign.test.mjs`

Expected: FAIL because source detail state, action, dialog, and compact CSS do not exist yet.

---

### Task 2: Add Source Detail UI

**Files:**
- Modify: `admin-ui/src/views/ChatLogs.js`
- Modify: `admin-ui/style.css`
- Test: `admin-ui/chat_logs_redesign.test.mjs`

**Interfaces:**
- Consumes: existing `row.sources`, `fmtSourceScore(s.score)`, and `documentTypeIcon({ title })`.
- Produces: `sourceDetail`, `openSourceDetail(source)`, `sourceMeta(source)`, compact source rows, and a source detail dialog.

- [ ] **Step 1: Add local state and helpers**

In `admin-ui/src/views/ChatLogs.js` setup, add:

```js
        const sourceDetail = reactive({ open: false, item: null });

        function sourceMeta(source) {
            if (!source) return [];
            const meta = [];
            if (source.document_id) meta.push({ label: '文档', value: source.document_id, mono: true });
            const page = source.page ?? source.page_number;
            if (page !== undefined && page !== null && page !== '') meta.push({ label: '页码', value: `第 ${page} 页` });
            const location = source.loc || source.location;
            if (location) meta.push({ label: '位置', value: location });
            meta.push({ label: '相似度', value: fmtSourceScore(source.score) });
            return meta;
        }

        function openSourceDetail(source) {
            sourceDetail.item = source || null;
            sourceDetail.open = true;
        }
```

Expose `sourceDetail`, `sourceMeta`, and `openSourceDetail` from the setup return object.

- [ ] **Step 2: Replace inline source content**

In the expanded row source list, replace the source item body with compact markup:

```html
                        <img v-if="documentTypeIcon({title: s.title || ''})" :src="documentTypeIcon({title: s.title || ''})" class="logs-source-file-icon" alt="" aria-hidden="true" />
                        <div class="logs-source-content">
                          <div class="logs-source-title">{{ s.title || '(无标题)' }} <span class="logs-source-score">{{ fmtSourceScore(s.score) }}</span></div>
                          <div class="logs-source-meta-row">
                            <span v-for="meta in sourceMeta(s)" :key="meta.label" class="logs-source-meta">
                              {{ meta.label }}: <span :class="meta.mono ? 'mono' : ''">{{ meta.value }}</span>
                            </span>
                          </div>
                        </div>
                        <div class="logs-source-actions">
                          <el-button link type="primary" size="small" @click="openSourceDetail(s)">查看详情</el-button>
                        </div>
```

- [ ] **Step 3: Add source detail dialog**

Add this dialog near the end of the template, before the root closing `</div>`:

```html
      <el-dialog v-model="sourceDetail.open" title="引用来源详情" width="720px" class="logs-source-detail-dialog">
        <template v-if="sourceDetail.item">
          <h3 class="logs-source-detail-title">{{ sourceDetail.item.title || '(无标题)' }}</h3>
          <div class="logs-source-detail-meta">
            <span v-for="meta in sourceMeta(sourceDetail.item)" :key="meta.label" class="logs-source-detail-meta-item">
              {{ meta.label }}: <span :class="meta.mono ? 'mono' : ''">{{ meta.value }}</span>
            </span>
          </div>
          <div class="logs-source-detail-text">{{ sourceDetail.item.content || '—' }}</div>
        </template>
      </el-dialog>
```

- [ ] **Step 4: Update styles**

In `admin-ui/style.css`, update the `.logs-source-*` rules to include:

```css
.logs-source-item { display:flex; align-items:flex-start; gap:10px; padding:8px 10px; border:1px solid var(--app-border-light); border-radius:8px; background:var(--app-page-bg); }
.logs-source-content { min-width:0; flex:1; }
.logs-source-title { font-size:13px; font-weight:600; color:var(--app-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.logs-source-score { font-size:12px; color:var(--app-text-muted); font-weight:400; margin-left:4px; }
.logs-source-meta-row { display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:3px; }
.logs-source-meta { font-size:11px; color:var(--app-text-muted); }
.logs-source-actions { flex:none; padding-top:1px; }
.logs-source-detail-title { margin:0 0 10px; font-size:16px; line-height:1.5; color:var(--app-text); }
.logs-source-detail-meta { display:flex; flex-wrap:wrap; gap:8px 14px; margin-bottom:12px; color:var(--app-text-secondary); font-size:12px; }
.logs-source-detail-text { max-height:56vh; overflow:auto; padding:12px 14px; border:1px solid var(--app-border-light); border-radius:8px; background:var(--app-page-bg); color:var(--app-text); font-size:13px; line-height:1.7; white-space:pre-wrap; word-break:break-word; }
```

Remove or stop using the old inline `.logs-source-text` source row body.

- [ ] **Step 5: Run targeted test**

Run: `node chat_logs_redesign.test.mjs`

Expected: PASS.

- [ ] **Step 6: Run full admin UI tests**

Run: `node --test`

Expected: All tests pass with 0 failures.

---

### Task 3: Compress Source Summary Cards

**Files:**
- Modify: `admin-ui/chat_logs_redesign.test.mjs`
- Modify: `admin-ui/src/views/ChatLogs.js`
- Modify: `admin-ui/style.css`

**Interfaces:**
- Consumes: existing `sourceDetail`, `sourceMeta(source)`, `fmtSourceScore(score)`, and source objects from `row.sources`.
- Produces: compact two-column source summaries showing only document title and color-graded similarity, with button-style source and row detail actions.

- [ ] **Step 1: Write failing tests**

Add assertions to `admin-ui/chat_logs_redesign.test.mjs`:

```js
test('引用来源摘要只显示文档名和分级相似度', () => {
    const sourceBlock = src.slice(src.indexOf('logs-source-list'), src.indexOf('logs-source-detail-dialog'));
    assert.ok(src.includes('sourceScoreTone(s.score)'), 'source score tone helper is used');
    assert.ok(sourceBlock.includes('logs-source-score-pill'), 'score pill class in source card');
    assert.ok(!sourceBlock.includes('sourceMeta(s)'), 'compact card omits document metadata');
    assert.ok(!sourceBlock.includes('document_id'), 'compact card omits document id');
});

test('引用来源列表使用两列紧凑网格，详情操作为按钮', () => {
    assert.match(css, /\.logs-source-list\s*\{[^}]*grid-template-columns:repeat\(2,/s);
    assert.match(css, /\.logs-source-item\s*\{[^}]*min-height:48px/s);
    assert.match(css, /\.logs-source-score-pill--high\s*\{/);
    assert.match(css, /\.logs-source-score-pill--medium\s*\{/);
    assert.match(css, /\.logs-source-score-pill--low\s*\{/);
    assert.doesNotMatch(src, /<el-button link type="primary" size="small" @click="openSourceDetail\(s\)">查看详情<\/el-button>/);
});

test('问答日志行级详情操作使用按钮样式', () => {
    assert.ok(src.includes('class="chat-logs-row-action"'), 'row detail button class');
    assert.doesNotMatch(src, /<el-button size="small" link type="primary" @click\.stop="toggleExpand\(row\)">/);
});
```

- [ ] **Step 2: Run targeted test and verify it fails**

Run: `node chat_logs_redesign.test.mjs`

Expected: FAIL because the compact card still renders metadata, score tone classes are absent, and row action is still link-style.

- [ ] **Step 3: Implement helpers and template updates**

In `admin-ui/src/views/ChatLogs.js`, add:

```js
        function sourceScoreTone(score) {
            const n = Number(score || 0);
            if (n >= 0.75) return 'high';
            if (n >= 0.5) return 'medium';
            return 'low';
        }
```

Expose `sourceScoreTone` from setup.

Replace compact source card metadata with:

```html
                          <div class="logs-source-title">{{ s.title || '(无标题)' }}</div>
                          <div class="logs-source-summary-line">
                            <span class="logs-source-score-pill" :class="'logs-source-score-pill--' + sourceScoreTone(s.score)">相似度 {{ fmtSourceScore(s.score) }}</span>
                          </div>
```

Change source detail action to:

```html
                          <el-button type="primary" plain size="small" @click="openSourceDetail(s)">查看详情</el-button>
```

Change row detail action to use a real button class and remove `link`:

```html
<el-button size="small" type="primary" plain class="chat-logs-row-action" @click.stop="toggleExpand(row)">{{ expandRowKeys.includes(row.message_id) ? '收起详情' : '查看详情' }}</el-button>
```

- [ ] **Step 4: Implement compact grid and score tone styles**

Update `admin-ui/style.css`:

```css
.logs-source-list { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px; }
.logs-source-item { display:flex; align-items:center; gap:8px; min-height:48px; padding:7px 8px; border:1px solid var(--app-border-light); border-radius:8px; background:var(--app-page-bg); }
.logs-source-file-icon { width:18px; height:18px; flex-shrink:0; object-fit:contain; }
.logs-source-content { min-width:0; flex:1; }
.logs-source-title { font-size:13px; font-weight:600; color:var(--app-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.logs-source-summary-line { display:flex; align-items:center; gap:6px; margin-top:4px; }
.logs-source-score-pill { display:inline-flex; align-items:center; height:20px; padding:0 7px; border-radius:999px; font-size:11px; font-weight:600; line-height:1; }
.logs-source-score-pill--high { color:#176B57; background:#EAF4F0; }
.logs-source-score-pill--medium { color:#3976C5; background:#EEF5FF; }
.logs-source-score-pill--low { color:#8A5A12; background:#FFF7E8; }
.logs-source-actions { flex:none; }
.chat-logs-row-action { min-width:76px; }
```

Keep the existing `@media (max-width: 899px)` rule that sets `.logs-source-item { flex-direction:column; }` only if it still looks appropriate; otherwise change it to `.logs-source-list { grid-template-columns:1fr; }` so cards stay compact.

- [ ] **Step 5: Run targeted and full verification**

Run: `node chat_logs_redesign.test.mjs`

Expected: PASS.

Run: `node --test`

Expected: All tests pass with 0 failures.
