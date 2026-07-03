# Library Detail Drawer Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the knowledge base detail drawer in `admin-ui` into a polished, grouped information-card layout.

**Architecture:** Keep existing Vue state, API calls, and actions unchanged. Replace only the detail drawer markup in `admin-ui/src/views/Libraries.js`, add focused `libraries-detail-*` CSS in `admin-ui/style.css`, and update `admin-ui/libraries_redesign.test.mjs` to lock the structure and no-inline-style constraint.

**Tech Stack:** Zero-build Vue 3 ES modules, Element Plus, local `<local-icon>`, plain CSS, Node test runner.

## Global Constraints

- Update only the library detail drawer template in `admin-ui/src/views/Libraries.js`.
- Update only related `libraries-detail-*` styles in `admin-ui/style.css`.
- Preserve all existing actions and data sources.
- Do not change API calls, route behavior, edit dialog behavior, FAQ dialog behavior, or backend code.
- Drawer width should be around `480px`, with the existing mobile rule still responsive.
- Use existing enterprise theme tokens: `--app-text`, `--app-text-secondary`, `--app-border`, `--app-border-light`, `--app-primary`, and `--app-page-bg`.
- No inline `style` attributes.

---

### Task 1: Add Drawer Structure Regression Tests

**Files:**
- Modify: `admin-ui/libraries_redesign.test.mjs`

**Interfaces:**
- Consumes: existing `source` and `css` constants from `admin-ui/libraries_redesign.test.mjs`.
- Produces: failing tests that require the redesigned drawer class names and preserve existing actions.

- [ ] **Step 1: Write the failing tests**

Add these tests after the existing `detail drawer includes description, embedding_base_url, vector_distance` test:

```js
test('detail drawer uses information-card layout classes', () => {
    for (const cls of [
        'libraries-detail-header',
        'libraries-detail-title-row',
        'libraries-detail-title',
        'libraries-detail-slug',
        'libraries-detail-summary',
        'libraries-detail-pill',
        'libraries-detail-sections',
        'libraries-detail-section',
        'libraries-detail-section-title',
        'libraries-detail-field',
        'libraries-detail-label',
        'libraries-detail-value',
    ]) assert.match(source, new RegExp(cls));
});

test('detail drawer keeps existing actions wired to selected library', () => {
    assert.match(source, /openEdit\(selectedLibrary\)/);
    assert.match(source, /openFaq\(selectedLibrary\)/);
    assert.match(source, /:disabled="!!selectedLibrary\.deleted_at"/);
});

test('detail drawer CSS defines card layout and wider drawer', () => {
    assert.match(css, /\.libraries-detail-drawer\s*\{[^}]*width:min\(92vw,480px\) !important;/s);
    assert.match(css, /\.libraries-detail-section\s*\{[^}]*background:#fff/s);
    assert.match(css, /\.libraries-detail-value--mono\s*\{/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node libraries_redesign.test.mjs`

Expected: FAIL in the new tests because the new class names and CSS do not exist yet.

- [ ] **Step 3: Stop after red**

Do not edit implementation in this task. The failing tests are the deliverable.

---

### Task 2: Replace Detail Drawer Markup

**Files:**
- Modify: `admin-ui/src/views/Libraries.js`
- Test: `admin-ui/libraries_redesign.test.mjs`

**Interfaces:**
- Consumes: `selectedLibrary`, `libraryStatus(selectedLibrary)`, `embedDisplay(selectedLibrary)`, `srcSummary(selectedLibrary.source_config)`, `openEdit(selectedLibrary)`, `openFaq(selectedLibrary)`, and `detailOpen` from the existing component.
- Produces: redesigned detail drawer markup with the class names asserted by Task 1.

- [ ] **Step 1: Replace only the detail drawer block**

In `admin-ui/src/views/Libraries.js`, replace the current block starting at `<!-- Detail drawer -->` and ending at `</el-drawer>` with:

```html
      <!-- Detail drawer -->
      <el-drawer v-model="detailOpen" class="libraries-detail-drawer" :with-header="false" size="480px">
        <template v-if="selectedLibrary">
          <div class="libraries-detail-header">
            <div class="libraries-detail-title-row">
              <div class="libraries-detail-identity">
                <h3 class="libraries-detail-title">{{ selectedLibrary.name }}</h3>
                <span class="libraries-detail-slug mono">{{ selectedLibrary.slug }}</span>
              </div>
              <el-tag :type="libraryStatus(selectedLibrary).type" size="small">{{ libraryStatus(selectedLibrary).label }}</el-tag>
            </div>
            <div class="libraries-detail-summary">
              <span class="libraries-detail-pill">{{ selectedLibrary.retrieval_mode === 'hybrid' ? '混合检索' : '向量检索' }}</span>
              <span class="libraries-detail-pill">{{ selectedLibrary.vector_distance || '—' }}</span>
              <span class="libraries-detail-pill">{{ selectedLibrary.chunk_size }} / {{ selectedLibrary.chunk_overlap }}</span>
            </div>
          </div>

          <div class="libraries-detail-sections">
            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">基础信息</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">描述</span>
                <span class="libraries-detail-value">{{ selectedLibrary.description || '—' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">唯一 ID</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.slug }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">状态</span>
                <span class="libraries-detail-value"><el-tag :type="libraryStatus(selectedLibrary).type" size="small">{{ libraryStatus(selectedLibrary).label }}</el-tag></span>
              </div>
            </section>

            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">向量与检索</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">Embedding</span>
                <span class="libraries-detail-value">{{ embedDisplay(selectedLibrary) }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">向量距离</span>
                <span class="libraries-detail-value">{{ selectedLibrary.vector_distance || '—' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">检索模式</span>
                <span class="libraries-detail-value">{{ selectedLibrary.retrieval_mode === 'hybrid' ? '混合检索' : '向量检索' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">切分</span>
                <span class="libraries-detail-value">{{ selectedLibrary.chunk_size }} / {{ selectedLibrary.chunk_overlap }}</span>
              </div>
            </section>

            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">存储与来源</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">Qdrant</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.qdrant_collection }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">全文源</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ srcSummary(selectedLibrary.source_config) }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">接口地址</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.embedding_base_url || '—' }}</span>
              </div>
            </section>
          </div>

          <div class="libraries-detail-actions">
            <el-button :disabled="!!selectedLibrary.deleted_at" @click="detailOpen=false;openEdit(selectedLibrary)">编辑配置</el-button>
            <el-button type="primary" plain :disabled="!!selectedLibrary.deleted_at" @click="detailOpen=false;openFaq(selectedLibrary)">管理常用问题</el-button>
          </div>
        </template>
      </el-drawer>
```

- [ ] **Step 2: Run targeted test**

Run: `node libraries_redesign.test.mjs`

Expected: The structure tests for `source` should pass, and the CSS test should still fail until Task 3.

---

### Task 3: Add Detail Drawer Styles

**Files:**
- Modify: `admin-ui/style.css`
- Test: `admin-ui/libraries_redesign.test.mjs`

**Interfaces:**
- Consumes: class names added in Task 2.
- Produces: responsive, card-like visual treatment for the library detail drawer.

- [ ] **Step 1: Replace existing detail drawer CSS**

In `admin-ui/style.css`, replace the existing `.libraries-detail-drawer`, `.libraries-detail-meta`, and `.libraries-detail-actions` rules around the libraries section with:

```css
.libraries-detail-drawer { width:min(92vw,480px) !important; }
.libraries-detail-drawer .el-drawer__body { display:flex; flex-direction:column; gap:16px; padding:0; background:var(--app-page-bg); }
.libraries-detail-header { padding:24px 24px 18px; background:#fff; border-bottom:1px solid var(--app-border-light); }
.libraries-detail-title-row { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.libraries-detail-identity { min-width:0; }
.libraries-detail-title { margin:0 0 5px; font-size:22px; line-height:1.25; font-weight:700; color:var(--app-text); overflow-wrap:anywhere; }
.libraries-detail-slug { display:block; font-size:12px; color:var(--app-text-secondary); }
.libraries-detail-summary { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
.libraries-detail-pill { display:inline-flex; align-items:center; min-height:26px; padding:0 10px; border-radius:999px; background:var(--app-primary-soft); color:var(--app-primary); font-size:12px; font-weight:600; }
.libraries-detail-sections { display:flex; flex-direction:column; gap:12px; padding:0 16px; }
.libraries-detail-section { background:#fff; border:1px solid var(--app-border-light); border-radius:12px; padding:16px; box-shadow:0 8px 20px rgba(30,70,54,.05); }
.libraries-detail-section-title { margin:0 0 14px; font-size:14px; line-height:1.3; font-weight:700; color:var(--app-text); }
.libraries-detail-field { display:grid; grid-template-columns:92px minmax(0,1fr); gap:12px; padding:10px 0; border-top:1px solid var(--app-border-light); }
.libraries-detail-field:first-of-type { border-top:0; padding-top:0; }
.libraries-detail-field:last-child { padding-bottom:0; }
.libraries-detail-label { color:var(--app-text-secondary); font-size:13px; line-height:1.6; }
.libraries-detail-value { min-width:0; color:var(--app-text); font-size:14px; line-height:1.6; overflow-wrap:anywhere; }
.libraries-detail-value--mono { font-family:Consolas, Monaco, 'Courier New', monospace; font-size:13px; }
.libraries-detail-actions { display:flex; gap:10px; margin-top:auto; padding:16px 16px 20px; background:#fff; border-top:1px solid var(--app-border-light); }
.libraries-detail-actions .el-button { flex:1; }
```

- [ ] **Step 2: Run targeted test**

Run: `node libraries_redesign.test.mjs`

Expected: PASS.

- [ ] **Step 3: Run full admin UI tests**

Run: `node --test`

Expected: All tests pass, including the updated libraries redesign tests.

---

### Task 4: Final Review

**Files:**
- Review: `admin-ui/src/views/Libraries.js`
- Review: `admin-ui/style.css`
- Review: `admin-ui/libraries_redesign.test.mjs`

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: verified final change summary.

- [ ] **Step 1: Inspect diff**

Run: `git diff -- admin-ui/src/views/Libraries.js admin-ui/style.css admin-ui/libraries_redesign.test.mjs docs/superpowers/specs/2026-07-03-library-detail-drawer-redesign.md docs/superpowers/plans/2026-07-03-library-detail-drawer-redesign.md`

Expected: Diff only includes the drawer redesign, related styles, tests, spec, and plan.

- [ ] **Step 2: Verify constraints manually**

Check these points in the diff:
- No API calls changed.
- No route behavior changed.
- No edit dialog or FAQ dialog behavior changed.
- No inline `style="..."` added in the drawer template.
- Mobile CSS still has `.libraries-detail-drawer { width:min(92vw,420px) !important; }` in the existing responsive block or a compatible override.

- [ ] **Step 3: Final verification command**

Run: `node --test`

Expected: All tests pass with 0 failures.
