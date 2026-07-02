# Library Management Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the administrator library-management page to match the approved enterprise workspace design while preserving every existing API and backend behavior.

**Architecture:** Keep API orchestration and destructive-operation guards in `Libraries.js`; extract only deterministic presentation logic into `libraries_ui.js`. Derive filters, statistics, and pagination from the already-loaded `libs` array. Reuse the existing local icons, empty-state illustration, request cache, and Element Plus components.

**Tech Stack:** Vue 3 Composition API without a build step, Element Plus, project-local CSS and SVG icons, Node `node:test`.

---

## Scope Guard

- Do not modify `admin-ui/src/api.js`, `admin-ui/src/store.js`, backend files, database code, routes, or request payloads.
- Do not add document counts, update times, rerank model names, or any field not returned by the current `listLibraries` response.
- Preserve create, edit-diff, FAQ, embedding test, collection rebuild, soft-delete, deleted-row protection, and confirmation dialogs.
- Reuse `dataEmpty` and `<local-icon>`; do not add image assets or external URLs.
- Keep all existing user changes in the dirty worktree. Never revert unrelated files.

### Task 1: Add Tested Library Presentation Helpers

**Files:**
- Create: `admin-ui/src/libraries_ui.js`
- Create: `admin-ui/libraries_redesign.test.mjs`

- [ ] **Step 1: Write failing helper tests**

Create `libraries_redesign.test.mjs` with real behavior tests:

```js
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    computeLibraryStats,
    filterLibraries,
    libraryStatus,
    paginateLibraries,
    srcSummary,
} from './src/libraries_ui.js';

const rows = [
    { slug: 'law', name: '法规库', deleted_at: null, retrieval_mode: 'hybrid',
      ocr_enabled: true, rerank_enabled: true },
    { slug: 'safe', name: '安全库', deleted_at: null, retrieval_mode: 'dense',
      ocr_enabled: false, rerank_enabled: null },
    { slug: 'old', name: '归档库', deleted_at: '2026-01-01', retrieval_mode: 'dense',
      ocr_enabled: true, rerank_enabled: false },
];

test('computeLibraryStats derives values from loaded rows', () => {
    assert.deepEqual(computeLibraryStats(rows), {
        total: 3, active: 2, ocr: 2, rerank: 1,
    });
});

test('filterLibraries searches name and slug', () => {
    assert.deepEqual(filterLibraries(rows, { keyword: 'LAW' }).map((x) => x.slug), ['law']);
    assert.deepEqual(filterLibraries(rows, { keyword: '安全' }).map((x) => x.slug), ['safe']);
});

test('filterLibraries combines status and retrieval mode', () => {
    assert.deepEqual(
        filterLibraries(rows, { status: 'active', retrievalMode: 'dense' }).map((x) => x.slug),
        ['safe'],
    );
    assert.deepEqual(filterLibraries(rows, { status: 'deleted' }).map((x) => x.slug), ['old']);
});

test('paginateLibraries clamps invalid pages', () => {
    assert.deepEqual(paginateLibraries(rows, 9, 2), {
        page: 2, pageSize: 2, total: 3, rows: [rows[2]],
    });
});

test('libraryStatus and srcSummary are safe', () => {
    assert.deepEqual(libraryStatus(rows[0]), { label: '正常', type: 'success' });
    assert.deepEqual(libraryStatus(rows[2]), { label: '已删除', type: 'info' });
    assert.equal(srcSummary(null), '未配置');
    assert.equal(
        srcSummary({ db_name: 'db', table: 'docs', key_field: 'id', text_column: 'content' }),
        'db.docs · id → content',
    );
});

const source = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
```

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run:

```powershell
cd admin-ui
node --test libraries_redesign.test.mjs
```

Expected: fail because `src/libraries_ui.js` does not exist.

- [ ] **Step 3: Implement the pure helper module**

Create `src/libraries_ui.js`:

```js
export function computeLibraryStats(libs = []) {
    return {
        total: libs.length,
        active: libs.filter((row) => !row.deleted_at).length,
        ocr: libs.filter((row) => row.ocr_enabled === true).length,
        rerank: libs.filter((row) => row.rerank_enabled === true).length,
    };
}

export function filterLibraries(libs = [], filters = {}) {
    const keyword = String(filters.keyword || '').trim().toLowerCase();
    const status = filters.status || '';
    const retrievalMode = filters.retrievalMode || '';
    return libs.filter((row) => {
        const haystack = `${row.name || ''} ${row.slug || ''}`.toLowerCase();
        if (keyword && !haystack.includes(keyword)) return false;
        if (status === 'active' && row.deleted_at) return false;
        if (status === 'deleted' && !row.deleted_at) return false;
        if (retrievalMode && row.retrieval_mode !== retrievalMode) return false;
        return true;
    });
}

export function paginateLibraries(libs = [], page = 1, pageSize = 10) {
    const size = [10, 20, 50].includes(Number(pageSize)) ? Number(pageSize) : 10;
    const pages = Math.max(1, Math.ceil(libs.length / size));
    const current = Math.min(Math.max(1, Number(page) || 1), pages);
    return {
        page: current,
        pageSize: size,
        total: libs.length,
        rows: libs.slice((current - 1) * size, current * size),
    };
}

export function libraryStatus(row) {
    return row?.deleted_at
        ? { label: '已删除', type: 'info' }
        : { label: '正常', type: 'success' };
}

export function srcSummary(cfg) {
    if (!cfg) return '未配置';
    const table = cfg.db_name ? `${cfg.db_name}.${cfg.table}` : cfg.table;
    return `${table || '—'} · ${cfg.key_field || '—'} → ${cfg.text_column || '—'}`;
}
```

- [ ] **Step 4: Run helper tests**

Run `node --test libraries_redesign.test.mjs`.

Expected: the five helper tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add admin-ui/src/libraries_ui.js admin-ui/libraries_redesign.test.mjs
git commit -m "test(ui): add library workspace helpers"
```

### Task 2: Rebuild the Library Workspace Without API Changes

**Files:**
- Modify: `admin-ui/src/views/Libraries.js`
- Modify: `admin-ui/libraries_redesign.test.mjs`

- [ ] **Step 1: Add structural regression tests**

Append tests that verify:

```js
test('Libraries keeps all existing APIs and confirmations', () => {
    for (const name of [
        'listLibraries', 'createLibrary', 'updateLibrary', 'deleteLibrary',
        'rebuildLibraryCollection', 'testLibraryEmbedding', 'listLibraryFaqs',
        'createLibraryFaq', 'updateLibraryFaq', 'deleteLibraryFaq',
    ]) assert.match(source, new RegExp(`api\\.${name}\\b`));
    assert.match(source, /ElMessageBox\.confirm/);
});

test('workspace uses only approved real fields and actions', () => {
    for (const fake of ['document_count', 'updated_at', 'rerank_model']) {
        assert.doesNotMatch(source, new RegExp(`\\b${fake}\\b`));
    }
    assert.match(source, /libraries-workspace/);
    assert.match(source, /libraries-detail-drawer/);
    assert.match(source, /openEdit\(row\)/);
    assert.match(source, /openFaq\(row\)/);
});

test('manual refresh bypasses cache and deleted behavior remains', () => {
    assert.match(source, /async function load\(forceRefresh = false\)/);
    assert.match(source, /api\.listLibraries\(params,\s*forceRefresh\)/);
    assert.match(source, /@click="load\(true\)"/);
    assert.match(source, /include_deleted/);
    assert.match(source, /:disabled="!!row\.deleted_at"/);
});

test('template has no inline style attributes', () => {
    const template = source.slice(source.indexOf('template:'));
    assert.doesNotMatch(template, /\sstyle="/);
});
```

Run `node --test libraries_redesign.test.mjs`; expect the new tests to fail.

- [ ] **Step 2: Add derived UI state to `Libraries.js`**

Change the Vue import to:

```js
import { computed, onMounted, reactive, ref, watch } from 'vue';
```

Import helpers:

```js
import {
    computeLibraryStats,
    filterLibraries,
    libraryStatus,
    paginateLibraries,
    srcSummary,
} from '../libraries_ui.js';
```

Delete the local `srcSummary`. Inside `setup()`, add:

```js
const keyword = ref('');
const statusFilter = ref('');
const retrievalFilter = ref('');
const page = ref(1);
const pageSize = ref(10);
const detailOpen = ref(false);
const selectedLibrary = ref(null);

const stats = computed(() => computeLibraryStats(libs.value));
const filteredLibraries = computed(() => filterLibraries(libs.value, {
    keyword: keyword.value,
    status: statusFilter.value,
    retrievalMode: retrievalFilter.value,
}));
const pagedLibraries = computed(() =>
    paginateLibraries(filteredLibraries.value, page.value, pageSize.value)
);
watch([keyword, retrievalFilter, showDeleted, pageSize], () => { page.value = 1; });
watch(statusFilter, async (value) => {
    page.value = 1;
    if (value === 'deleted' && !showDeleted.value) {
        showDeleted.value = true;
        await load(true);
    }
});

function resetFilters() {
    keyword.value = '';
    statusFilter.value = '';
    retrievalFilter.value = '';
    page.value = 1;
}

function openDetail(row) {
    selectedLibrary.value = row;
    detailOpen.value = true;
}
```

Change loading to:

```js
async function load(forceRefresh = false) {
    loading.value = true;
    try {
        const params = showDeleted.value ? { include_deleted: 'true' } : {};
        libs.value = await api.listLibraries(params, forceRefresh);
        if (selectedLibrary.value) {
            selectedLibrary.value =
                libs.value.find((row) => row.slug === selectedLibrary.value.slug) || null;
        }
    } catch (e) {
        ElMessage.error(e.message);
    } finally {
        loading.value = false;
    }
}
```

All successful create/edit/rebuild/delete handlers must call `await load(true)` so refreshed data is visible immediately. Keep their existing payloads, confirmations, messages, and exception handling unchanged.

- [ ] **Step 3: Replace only the page template structure**

Use these required sections and classes:

```html
<div class="libraries-workspace">
  <header class="libraries-header">
    <div>
      <h2 class="libraries-title">知识库管理</h2>
      <p class="libraries-desc">管理知识库配置、检索模式与处理能力</p>
    </div>
    <div class="libraries-header-actions">
      <el-button @click="load(true)" :loading="loading">刷新</el-button>
      <el-button type="primary" @click="openCreate">新建知识库</el-button>
    </div>
  </header>

  <section class="libraries-toolbar">
    <el-input v-model="keyword" class="libraries-search" clearable
      placeholder="搜索知识库名称或唯一 ID" />
    <el-select v-model="statusFilter" class="libraries-filter" placeholder="全部状态" clearable>
      <el-option label="正常" value="active" />
      <el-option label="已删除" value="deleted" />
    </el-select>
    <el-select v-model="retrievalFilter" class="libraries-filter"
      placeholder="全部检索模式" clearable>
      <el-option label="向量检索" value="dense" />
      <el-option label="混合检索" value="hybrid" />
    </el-select>
    <el-checkbox v-model="showDeleted" @change="load(true)">包含已删除</el-checkbox>
    <el-button @click="resetFilters">重置</el-button>
  </section>

  <section class="libraries-stats">
    <div class="libraries-stat"><local-icon icon="overview:kb-count" class="libraries-stat-icon" /><b>{{ stats.total }}</b><span>知识库总数</span></div>
    <div class="libraries-stat libraries-stat--active"><local-icon icon="overview:online-services" class="libraries-stat-icon" /><b>{{ stats.active }}</b><span>正常</span></div>
    <div class="libraries-stat libraries-stat--ocr"><span class="libraries-stat-text-icon">OCR</span><b>{{ stats.ocr }}</b><span>OCR 开启</span></div>
    <div class="libraries-stat libraries-stat--rerank"><local-icon icon="overview:service-status" class="libraries-stat-icon" /><b>{{ stats.rerank }}</b><span>Rerank 配置</span></div>
  </section>

  <section class="libraries-table-card">
    <div class="libraries-table-shell">
      <el-table :data="pagedLibraries.rows" v-loading="loading" row-key="slug"
        @row-click="openDetail">
        <template #empty>
          <div class="illustration-empty-wrapper">
            <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
            <p>暂无知识库</p>
          </div>
        </template>
      </el-table>
    </div>
    <div class="libraries-pagination">
      <span>共 {{ pagedLibraries.total }} 条</span>
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize"
        :page-sizes="[10,20,50]" layout="sizes, prev, pager, next"
        :total="pagedLibraries.total" />
    </div>
  </section>
</div>
```

Implement the table columns explicitly:

- Name cell: `row.name`, secondary monospace `row.slug`.
- Status: `libraryStatus(row).label/type`.
- Embedding: `row.embedding_model || '全局默认'` and `row.embedding_dim ? row.embedding_dim + 'd' : '维度未配置'`.
- Processing: tags for `row.ocr_enabled === true` and `row.rerank_enabled === true`; all other values show “继承/关闭” without inventing a model.
- Retrieval: `hybrid` → “混合检索”, otherwise “向量检索”.
- Chunk: `row.chunk_size / row.chunk_overlap`.
- Actions: text buttons “详情”“编辑”“常用问题”; dropdown “更多” contains test, rebuild, delete. Add `.stop` to every action click so row-click does not reopen the drawer.
- Keep all write actions disabled when `row.deleted_at` is truthy.
- Keep the current `dataEmpty` wrapper in the table `#empty` slot.

Add an `el-drawer` using `class="libraries-detail-drawer"` and `selectedLibrary`; display only current real fields. Its footer has “编辑配置” and “管理常用问题”, disabled for deleted rows.

Keep the three existing dialogs after the workspace and add `class="libraries-dialog"` to each. Reorganize create/edit forms into three titled sections, but preserve every field and handler. Replace every inline style with `libraries-form-*` or `libraries-faq-*` classes.

- [ ] **Step 4: Expose all new state and run tests**

Return all template dependencies from `setup()`: `keyword`, `statusFilter`, `retrievalFilter`, `page`, `pageSize`, `stats`, `pagedLibraries`, `detailOpen`, `selectedLibrary`, `resetFilters`, `openDetail`, `libraryStatus`, plus all existing values and handlers.

Run:

```powershell
node --check src/views/Libraries.js
node --test libraries_redesign.test.mjs
```

Expected: all library tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add admin-ui/src/views/Libraries.js admin-ui/libraries_redesign.test.mjs
git commit -m "feat(ui): rebuild library management workspace"
```

### Task 3: Add Responsive Library Workspace Styles

**Files:**
- Modify: `admin-ui/style.css`
- Modify: `admin-ui/libraries_redesign.test.mjs`

- [ ] **Step 1: Add failing CSS tests**

```js
test('CSS defines library workspace and responsive rules', () => {
    for (const cls of [
        'libraries-workspace', 'libraries-header', 'libraries-toolbar',
        'libraries-stats', 'libraries-table-card', 'libraries-table-shell',
        'libraries-pagination', 'libraries-detail-drawer', 'libraries-form-section',
    ]) assert.match(css, new RegExp(`\\.${cls}\\b`));
    assert.match(css, /@media \(max-width: 1199px\)[\s\S]*libraries-/);
    assert.match(css, /@media \(max-width: 899px\)[\s\S]*libraries-/);
});
```

Run the test and confirm failure.

- [ ] **Step 2: Implement desktop styles before responsive media queries**

Add a single consolidated `libraries-*` block before the existing `@media (max-width: 1199px)` section. Match existing variables and cards:

```css
.libraries-workspace { display:flex; flex-direction:column; gap:16px; min-width:0; }
.libraries-header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
.libraries-title { margin:0 0 4px; font-size:22px; font-weight:600; color:var(--app-text); }
.libraries-desc { margin:0; font-size:13px; color:var(--app-text-secondary); }
.libraries-header-actions { display:flex; gap:8px; }
.libraries-toolbar { display:flex; align-items:center; gap:10px; padding:14px 16px; border:1px solid var(--app-border); border-radius:14px; background:#fff; }
.libraries-search { width:min(320px, 100%); }
.libraries-filter { width:150px; }
.libraries-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border:1px solid var(--app-border); border-radius:14px; background:#fff; overflow:hidden; }
.libraries-stat { min-width:0; padding:18px 20px; display:grid; grid-template-columns:38px 1fr; grid-template-rows:auto auto; column-gap:12px; border-right:1px solid var(--app-border-light); }
.libraries-stat:last-child { border-right:0; }
.libraries-stat-icon,.libraries-stat-text-icon { grid-row:1/3; align-self:center; width:30px; height:30px; color:var(--app-primary); }
.libraries-stat b { font-size:24px; line-height:1.1; color:var(--app-text); }
.libraries-stat span { font-size:12px; color:var(--app-text-secondary); }
.libraries-stat-text-icon { display:grid; place-items:center; border:2px solid #e88816; border-radius:7px; font-size:10px; font-weight:700; color:#e88816; }
.libraries-table-card { min-width:0; border:1px solid var(--app-border); border-radius:14px; background:#fff; box-shadow:0 8px 24px rgba(30,70,54,.06); overflow:hidden; }
.libraries-table-shell { width:100%; overflow-x:auto; }
.libraries-library-cell { min-width:0; }
.libraries-library-name { font-weight:600; color:var(--app-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.libraries-library-slug { margin-top:3px; font-size:12px; color:var(--app-text-secondary); }
.libraries-actions { display:flex; align-items:center; gap:4px; white-space:nowrap; }
.libraries-pagination { display:flex; justify-content:space-between; align-items:center; padding:12px 16px; border-top:1px solid var(--app-border-light); color:var(--app-text-secondary); font-size:13px; }
.libraries-detail-drawer .el-drawer__body { display:flex; flex-direction:column; gap:16px; }
.libraries-detail-meta { display:grid; grid-template-columns:110px minmax(0,1fr); gap:11px 14px; }
.libraries-detail-meta dt { color:var(--app-text-secondary); }
.libraries-detail-meta dd { margin:0; color:var(--app-text); overflow-wrap:anywhere; }
.libraries-detail-actions { display:flex; gap:8px; margin-top:auto; }
.libraries-form-section { margin-bottom:18px; }
.libraries-form-section-title { margin:0 0 14px; padding-bottom:8px; border-bottom:1px solid var(--app-border-light); font-size:14px; font-weight:600; color:var(--app-text); }
.libraries-form-control { width:100%; }
.libraries-form-hint { margin-top:3px; font-size:12px; line-height:1.4; color:var(--app-text-secondary); }
.libraries-form-alert { margin-top:4px; }
.libraries-faq-alert { margin-bottom:12px; }
.libraries-faq-create { display:grid; grid-template-columns:minmax(0,1fr) 120px auto; gap:8px; margin-bottom:12px; }
```

- [ ] **Step 3: Add responsive rules to existing media blocks**

At `max-width:1199px`, make toolbar wrap and allow the drawer to overlay normally:

```css
.libraries-toolbar { flex-wrap:wrap; }
.libraries-search { flex:1 1 260px; }
.libraries-stats { grid-template-columns:repeat(4,minmax(120px,1fr)); overflow-x:auto; }
```

At `max-width:899px`:

```css
.libraries-header { align-items:stretch; }
.libraries-header-actions { width:100%; }
.libraries-header-actions .el-button { flex:1; }
.libraries-toolbar { flex-direction:column; align-items:stretch; }
.libraries-search,.libraries-filter { width:100%; }
.libraries-stats { grid-template-columns:repeat(2,minmax(0,1fr)); overflow:visible; }
.libraries-stat { border-right:0; border-bottom:1px solid var(--app-border-light); }
.libraries-stat:nth-child(odd) { border-right:1px solid var(--app-border-light); }
.libraries-stat:nth-last-child(-n+2) { border-bottom:0; }
.libraries-pagination { flex-direction:column; align-items:flex-start; gap:10px; }
.libraries-faq-create { grid-template-columns:1fr; }
.libraries-detail-drawer { width:min(92vw,420px) !important; }
.libraries-dialog { width:calc(100vw - 24px) !important; }
```

- [ ] **Step 4: Run tests and commit Task 3**

```powershell
node --test libraries_redesign.test.mjs
git diff --check
git add admin-ui/style.css admin-ui/libraries_redesign.test.mjs
git commit -m "style(ui): add responsive library workspace"
```

### Task 4: Full Verification and Report

**Files:**
- No production changes unless verification reveals a defect.

- [ ] **Step 1: Run syntax and automated tests**

```powershell
cd admin-ui
Get-ChildItem -Recurse -File -Include *.js,*.mjs | ForEach-Object {
  node --check $_.FullName
  if ($LASTEXITCODE -ne 0) { throw "syntax failed: $($_.FullName)" }
}
node --test
cd ..
python scripts/check_release_safety.py
git diff --check
```

Expected: zero syntax failures, zero test failures, release-safety pass, and no whitespace errors.

- [ ] **Step 2: Verify in a real browser**

Use the running application and authenticated administrator account. Verify `/console/#/libraries` at 1920, 1199, and 899 CSS pixels:

- No page-level horizontal overflow.
- Toolbar, four statistics, table, pagination, and empty state render correctly.
- Only the table shell may scroll horizontally.
- Detail drawer opens from row/detail action and displays real fields.
- Search, status, retrieval mode, reset, include-deleted, page size, and pagination work.
- Refresh bypasses cache.
- Deleted rows cannot edit, manage FAQ, test, rebuild, or delete.
- Create/edit/FAQ dialogs retain their previous fields and behavior.
- No missing local icon or image; no JavaScript console error.

- [ ] **Step 3: Report scope and residual risk**

Report:

- Exact files modified/created.
- Test counts and commands.
- Browser results for all three widths.
- Confirmation that no backend, `api.js`, `store.js`, routes, payloads, or external assets changed.
- Any backend-dependent flow not exercised end-to-end.

Do not claim browser or backend verification unless it was actually performed.
