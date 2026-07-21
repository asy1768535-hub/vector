# Permission Matrix Tools Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the colorful read/write/delete legend with a quiet management hint in the user configuration box, and make the matrix tools row start with knowledge-base search.

**Architecture:** Keep permission state, API calls, filtering, save/reset behavior, and table columns unchanged. Update only the `Permissions.js` tools markup, related `permissions-*` CSS, and regression tests.

**Tech Stack:** Zero-build Vue 3 ES modules, Element Plus, plain CSS, Node test runner.

## Global Constraints

- Update only the tools area in `admin-ui/src/views/Permissions.js`.
- Update only related `permissions-matrix-tools`, `permissions-action-legend`, `permissions-search-wrap`, and supporting styles in `admin-ui/style.css`.
- Preserve existing permission data, save/reset behavior, filtering behavior, and table columns.
- Do not change API calls or permission semantics.
- Remove the colored legend boxes and checkmarks.
- Put the management hint in the top user configuration box, not in the matrix tools row.
- Put the knowledge-base search input first in the matrix tools row.
- No inline `style` attributes.

---

### Task 1: Add Regression Test

**Files:**
- Modify: `admin-ui/permissions_redesign.test.mjs`

**Interfaces:**
- Consumes: existing `source` and `css` constants in `admin-ui/permissions_redesign.test.mjs`.
- Produces: failing assertions requiring the new hint and removal of old legend boxes.

- [ ] **Step 1: Add failing test**

Add this test near the other template regression tests:

```js
test('management hint lives in user toolbar and matrix tools start with search', () => {
    assert.match(source, /可为该用户配置各知识库的读取、写入、删除权限/);
    assert.doesNotMatch(source, /permissions-legend-box/);
    assert.doesNotMatch(source, /permissions-legend--read/);
    assert.doesNotMatch(source, /permissions-legend--insert/);
    assert.doesNotMatch(source, /permissions-legend--delete/);
    assert.match(source, /permissions-config-hint/);
    assert.match(source, /permissions-search-wrap/);
    assert.match(source, /匹配 \{\{ filteredLibs\.length \}\} \/ \{\{ libs\.length \}\} 个库/);
    assert.match(css, /\.permissions-matrix-tools\s*\{/);
    assert.match(css, /\.permissions-config-hint\s*\{/);
});
```

- [ ] **Step 2: Verify red**

Run: `node permissions_redesign.test.mjs`

Expected: FAIL because the hint and CSS class do not exist and old legend markup still exists.

---

### Task 2: Replace Tools Markup and Styles

**Files:**
- Modify: `admin-ui/src/views/Permissions.js`
- Modify: `admin-ui/style.css`
- Test: `admin-ui/permissions_redesign.test.mjs`

**Interfaces:**
- Consumes: existing `selectedUser`, `keyword`, `filteredLibs`, and `libs` bindings.
- Produces: a single tools row with neutral hint text and the existing search input/match count.

- [ ] **Step 1: Replace markup**

In `admin-ui/src/views/Permissions.js`, add this hint inside the top `.permissions-toolbar-row`, after the role/status user info and before the save hint:

```html
              <span v-if="currentUser" class="permissions-config-hint">可为该用户配置各知识库的读取、写入、删除权限</span>
```

Then remove the current `.permissions-tools-hint` from `<section class="permissions-matrix-tools" v-if="selectedUser">`, leaving `permissions-search-wrap` as the first child in that section.

- [ ] **Step 2: Replace styles**

In `admin-ui/style.css`, remove the old legend box styles and add:

```css
.permissions-config-hint { color:var(--app-text-secondary); font-size:13px; white-space:nowrap; }
.permissions-matrix-tools { display:flex; align-items:center; gap:10px; }
.permissions-search-wrap { display:flex; align-items:center; gap:10px; }
```

Remove these obsolete rules if present:

```css
.permissions-action-legend { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--app-text-secondary); margin-left:auto; }
.permissions-legend-dot { width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; }
.permissions-legend--read { background:var(--color-info); }
.permissions-legend--insert { background:var(--color-success); }
.permissions-legend--delete { background:var(--app-danger); }
```

- [ ] **Step 3: Update mobile styles**

In the existing `@media (max-width: 899px)` permissions block, add or keep:

```css
    .permissions-config-hint { white-space:normal; }
    .permissions-matrix-tools { flex-direction:column; align-items:stretch; }
    .permissions-search-wrap { flex-direction:column; align-items:stretch; }
```

- [ ] **Step 4: Run targeted test**

Run: `node permissions_redesign.test.mjs`

Expected: PASS.

- [ ] **Step 5: Run full admin UI tests**

Run: `node --test`

Expected: All tests pass with 0 failures.
