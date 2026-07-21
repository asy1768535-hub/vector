# Documents Page Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将登录后的文档页重构为企业知识库文档工作台，在不修改后端的前提下提供概览、筛选、客户端分页、详情任务与管理员失败重试。

**Architecture:** `Documents.js` 继续负责权限、页面状态和现有 API 调用；新增 `documents_ui.js` 承载可独立测试的类型识别、筛选、格式化和分页规则。`Import.js` 只增加路由 query 到现有库选择/导入模式的衔接，所有新按钮均复用现有路由或 API。

**Tech Stack:** Vue 3 Composition API、Vue Router、Element Plus、原生 ES Modules、Node.js `node:test`。

---

## File Map

- Create: `admin-ui/src/documents_ui.js` - 文档展示、筛选与客户端分页的纯函数。
- Create: `admin-ui/documents_ui.test.mjs` - 上述纯函数的行为测试。
- Create: `admin-ui/src/import_query.js` - 文件导入页 query 的权限安全解析。
- Create: `admin-ui/import_query.test.mjs` - query 合法值与回退行为测试。
- Modify: `admin-ui/src/views/Import.js` - 读取 `/import?library=<slug>&mode=<mode>`。
- Modify: `admin-ui/src/views/Documents.js` - 文档工作台结构、详情抽屉和现有接口衔接。
- Create: `admin-ui/documents_redesign.test.mjs` - 文档页结构、权限和禁止项回归测试。
- Modify: `admin-ui/style.css` - 文档页桌面与移动端样式。
- Do not modify: `admin-ui/src/api.js`、`admin-ui/src/store.js`、`app/**` 或数据库文件。

### Task 0: Isolate Existing Chat Follow-up

**Files:**
- Existing follow-up only: `.gitignore`
- Existing follow-up only: `admin-ui/chat_redesign.test.mjs`
- Existing follow-up only: `admin-ui/copy_answer.test.mjs`
- Existing follow-up only: `admin-ui/src/copy_text.js`
- Existing follow-up only: `admin-ui/src/views/Chat.js`
- Existing follow-up only: `admin-ui/style.css`

- [ ] **Step 1: Inspect the dirty worktree without changing it**

Run:

```powershell
git status --short
git diff -- .gitignore admin-ui/chat_redesign.test.mjs admin-ui/src/views/Chat.js admin-ui/style.css
git diff --no-index -- NUL admin-ui/copy_answer.test.mjs
git diff --no-index -- NUL admin-ui/src/copy_text.js
```

Expected: the six paths contain only the already-verified chat empty-state, copy fallback, CSS consolidation, and local safety-ignore work. Do not stage `admin-ui/src/api.js`, `admin-ui/src/store.js`, backend files, login files, user files, or unrelated untracked files.

- [ ] **Step 2: Re-run the existing frontend baseline**

Run:

```powershell
Set-Location admin-ui
node --test
Set-Location ..
```

Expected: all current frontend test files pass with zero failures.

- [ ] **Step 3: Commit only the verified chat follow-up**

Run:

```powershell
git add -- .gitignore admin-ui/chat_redesign.test.mjs admin-ui/copy_answer.test.mjs admin-ui/src/copy_text.js admin-ui/src/views/Chat.js admin-ui/style.css
git diff --cached --name-status
git commit -m "fix(ui): harden chat copy and empty state"
```

Expected: the staged name list contains exactly those six paths. If any of those files contains unrelated edits, stop and report the mixed diff instead of committing.

### Task 1: Add Tested Document UI Helpers

**Files:**
- Create: `admin-ui/src/documents_ui.js`
- Create: `admin-ui/documents_ui.test.mjs`

- [ ] **Step 1: Write failing behavior tests**

Create `admin-ui/documents_ui.test.mjs` with `node:test` cases covering:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import {
    documentDisplayName,
    documentStatusLabel,
    documentStatusTag,
    documentType,
    filterDocuments,
    formatDocumentTime,
    paginateDocuments,
} from './src/documents_ui.js';

const rows = [
    { id: 'doc-a', title: '员工手册.pdf', external_id: 'HR-001', status: 'ready', updated_at: '2026-06-01T08:00:00Z' },
    { id: 'doc-b', title: '产品说明.docx', external_id: 'PD-002', status: 'processing', updated_at: '2026-06-15T08:00:00Z' },
    { id: 'doc-c', title: '知识索引.md', external_id: null, status: 'failed', updated_at: '2026-06-30T08:00:00Z' },
];

test('infers supported document types from title suffix', () => {
    assert.equal(documentType({ title: 'a.pdf' }), 'pdf');
    assert.equal(documentType({ title: 'a.DOCX' }), 'word');
    assert.equal(documentType({ title: 'a.xlsx' }), 'excel');
    assert.equal(documentType({ title: 'a.markdown' }), 'markdown');
    assert.equal(documentType({ title: 'a.txt' }), 'text');
    assert.equal(documentType({ title: 'a.bin' }), 'other');
});

test('falls back from title to external id to shortened id', () => {
    assert.equal(documentDisplayName({ title: '标题', external_id: 'EXT', id: '123456789' }), '标题');
    assert.equal(documentDisplayName({ title: '', external_id: 'EXT', id: '123456789' }), 'EXT');
    assert.equal(documentDisplayName({ title: '', external_id: '', id: '123456789' }), '12345678…');
    assert.equal(documentDisplayName({}), '未命名文档');
});

test('maps status to Chinese labels and Element Plus tag types', () => {
    assert.equal(documentStatusLabel('pending'), '等待中');
    assert.equal(documentStatusLabel('processing'), '处理中');
    assert.equal(documentStatusLabel('ready'), '完成');
    assert.equal(documentStatusLabel('failed'), '失败');
    assert.equal(documentStatusLabel('unknown'), 'unknown');
    assert.equal(documentStatusTag('ready'), 'success');
    assert.equal(documentStatusTag('failed'), 'danger');
});

test('combines keyword status type and inclusive date filters', () => {
    assert.deepEqual(
        filterDocuments(rows, {
            keyword: '知识',
            status: 'failed',
            type: 'markdown',
            dateRange: ['2026-06-30', '2026-06-30'],
        }).map((row) => row.id),
        ['doc-c'],
    );
    assert.deepEqual(filterDocuments(rows, { keyword: 'hr-001' }).map((row) => row.id), ['doc-a']);
    assert.deepEqual(filterDocuments(rows, { keyword: 'DOC-B' }).map((row) => row.id), ['doc-b']);
    assert.deepEqual(filterDocuments(rows, { dateRange: ['bad-date', '2026-06-30'] }), []);
});

test('paginates and clamps page boundaries', () => {
    assert.deepEqual(paginateDocuments([1, 2, 3, 4, 5], 2, 2), {
        items: [3, 4], total: 5, page: 2, pageCount: 3,
    });
    assert.deepEqual(paginateDocuments([1, 2, 3], 9, 2), {
        items: [3], total: 3, page: 2, pageCount: 2,
    });
    assert.deepEqual(paginateDocuments([], 1, 10), {
        items: [], total: 0, page: 1, pageCount: 1,
    });
});

test('formats valid time and safely handles empty or invalid values', () => {
    assert.equal(formatDocumentTime(null), '—');
    assert.equal(formatDocumentTime('not-a-date'), '—');
    assert.notEqual(formatDocumentTime('2026-06-01T08:00:00Z'), '—');
});
```

- [ ] **Step 2: Run the test and verify it fails for the missing module**

Run:

```powershell
Set-Location admin-ui
node --test documents_ui.test.mjs
```

Expected: FAIL because `src/documents_ui.js` does not exist.

- [ ] **Step 3: Implement the pure helpers**

Create `admin-ui/src/documents_ui.js`:

```js
const STATUS_LABEL = {
    pending: '等待中',
    processing: '处理中',
    ready: '完成',
    failed: '失败',
    deleted: '已删除',
};

const STATUS_TAG = {
    pending: 'info',
    processing: 'warning',
    ready: 'success',
    failed: 'danger',
    deleted: 'info',
};

export function documentType(row) {
    const name = String(row?.title || row?.external_id || '').toLowerCase();
    const ext = name.includes('.') ? name.split('.').pop() : '';
    if (ext === 'pdf') return 'pdf';
    if (ext === 'doc' || ext === 'docx') return 'word';
    if (ext === 'xls' || ext === 'xlsx') return 'excel';
    if (ext === 'md' || ext === 'markdown') return 'markdown';
    if (ext === 'txt') return 'text';
    return 'other';
}

export function documentDisplayName(row) {
    if (row?.title) return row.title;
    if (row?.external_id) return row.external_id;
    if (row?.id) return `${String(row.id).slice(0, 8)}…`;
    return '未命名文档';
}

export function documentStatusLabel(status) {
    return STATUS_LABEL[status] || status || '未知';
}

export function documentStatusTag(status) {
    return STATUS_TAG[status] || 'info';
}

export function filterDocuments(rows, filters = {}) {
    const keyword = String(filters.keyword || '').trim().toLowerCase();
    const [startText, endText] = Array.isArray(filters.dateRange) ? filters.dateRange : [];
    const start = startText ? new Date(`${startText}T00:00:00`) : null;
    const end = endText ? new Date(`${endText}T23:59:59.999`) : null;
    if ((start && Number.isNaN(start.getTime())) || (end && Number.isNaN(end.getTime()))) return [];
    return (rows || []).filter((row) => {
        const haystack = [row.title, row.external_id, row.id].filter(Boolean).join(' ').toLowerCase();
        if (keyword && !haystack.includes(keyword)) return false;
        if (filters.status && row.status !== filters.status) return false;
        if (filters.type && documentType(row) !== filters.type) return false;
        if (start || end) {
            const updated = new Date(row.updated_at);
            if (Number.isNaN(updated.getTime())) return false;
            if (start && updated < start) return false;
            if (end && updated > end) return false;
        }
        return true;
    });
}

export function paginateDocuments(rows, requestedPage = 1, requestedSize = 10) {
    const total = rows.length;
    const size = [10, 20, 50].includes(Number(requestedSize)) ? Number(requestedSize) : 10;
    const pageCount = Math.max(1, Math.ceil(total / size));
    const page = Math.min(Math.max(1, Number(requestedPage) || 1), pageCount);
    const start = (page - 1) * size;
    return { items: rows.slice(start, start + size), total, page, pageCount };
}

export function formatDocumentTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', { hour12: false });
}
```

- [ ] **Step 4: Run the focused and full tests**

Run:

```powershell
node --test documents_ui.test.mjs
node --test
Set-Location ..
```

Expected: the new helper tests and all existing tests pass.

- [ ] **Step 5: Commit the helper unit**

Run:

```powershell
git add -- admin-ui/src/documents_ui.js admin-ui/documents_ui.test.mjs
git diff --cached --name-status
git commit -m "test(ui): add document list helpers"
```

Expected: exactly two files are committed.

### Task 2: Connect File Import Route Query

**Files:**
- Create: `admin-ui/src/import_query.js`
- Create: `admin-ui/import_query.test.mjs`
- Modify: `admin-ui/src/views/Import.js`

- [ ] **Step 1: Write query resolution tests**

Create `admin-ui/import_query.test.mjs`:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveImportEntry } from './src/import_query.js';

const libraries = [{ slug: 'hr' }, { slug: 'product' }];

test('accepts an allowed library and supported mode', () => {
    assert.deepEqual(resolveImportEntry({ library: 'product', mode: 'replace' }, libraries), {
        slug: 'product', mode: 'replace',
    });
});

test('defaults mode to add and rejects an unavailable library', () => {
    assert.deepEqual(resolveImportEntry({ library: 'secret', mode: 'other' }, libraries, 'hr'), {
        slug: 'hr', mode: 'add',
    });
});

test('falls back to first available library then null', () => {
    assert.deepEqual(resolveImportEntry({}, libraries), { slug: 'hr', mode: 'add' });
    assert.deepEqual(resolveImportEntry({ library: 'hr' }, []), { slug: null, mode: 'add' });
});
```

- [ ] **Step 2: Verify the new test fails**

Run:

```powershell
Set-Location admin-ui
node --test import_query.test.mjs
```

Expected: FAIL because `src/import_query.js` does not exist.

- [ ] **Step 3: Add the query resolver**

Create `admin-ui/src/import_query.js`:

```js
export function resolveImportEntry(query, libraries, currentSlug = null) {
    const allowed = new Set((libraries || []).map((library) => library.slug));
    const requested = typeof query?.library === 'string' ? query.library : '';
    const slug = allowed.has(requested)
        ? requested
        : allowed.has(currentSlug)
            ? currentSlug
            : (libraries?.[0]?.slug ?? null);
    const mode = query?.mode === 'replace' ? 'replace' : 'add';
    return { slug, mode };
}
```

- [ ] **Step 4: Apply the resolved entry after permission-filtered libraries load**

In `admin-ui/src/views/Import.js`:

```js
import { useRoute } from 'vue-router';
import { resolveImportEntry } from '../import_query.js';
```

At the start of `setup()`:

```js
const route = useRoute();
```

At the end of the successful branch of `loadLibs()`, replace the existing first-library assignment with:

```js
const entry = resolveImportEntry(route.query, libs.value, slug.value);
slug.value = entry.slug;
mode.value = entry.mode;
```

Do not change upload queue logic, supported extensions, API calls, or replace behavior.

- [ ] **Step 5: Run focused, syntax, and full tests**

Run:

```powershell
node --test import_query.test.mjs
node --check src/import_query.js
node --check src/views/Import.js
node --test
Set-Location ..
```

Expected: all commands pass.

- [ ] **Step 6: Commit the route connection**

Run:

```powershell
git add -- admin-ui/src/import_query.js admin-ui/import_query.test.mjs admin-ui/src/views/Import.js
git diff --cached --name-status
git commit -m "feat(ui): connect document import route query"
```

Expected: exactly these three paths are committed.

### Task 3: Rebuild the Documents Workspace

**Files:**
- Modify: `admin-ui/src/views/Documents.js`
- Create: `admin-ui/documents_redesign.test.mjs`

- [ ] **Step 1: Write structural regression tests before changing the page**

Create `admin-ui/documents_redesign.test.mjs` that reads `src/views/Documents.js` and asserts:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('./src/views/Documents.js', import.meta.url), 'utf8');

test('uses the approved document workspace sections', () => {
    for (const token of [
        'documents-workspace',
        'documents-overview',
        'documents-filters',
        'documents-table-shell',
        'documents-detail',
        'el-pagination',
        '当前加载',
    ]) assert.match(source, new RegExp(token));
});

test('keeps existing APIs and permission gates', () => {
    for (const token of [
        'api.listDocuments',
        'api.libraryStats',
        'api.ingestDocument',
        'api.updateDocument',
        'api.deleteDocument',
        'api.listDocumentJobs',
        'api.retryJob',
        'canInsert',
        'canDelete',
        'isSuperuser',
    ]) assert.ok(source.includes(token), `missing ${token}`);
});

test('contains only approved low-risk actions', () => {
    for (const forbidden of ['查看原文', '下载原文', '批量删除', '版本正文']) {
        assert.equal(source.includes(forbidden), false, `forbidden action: ${forbidden}`);
    }
});

test('loads at most 500 records and routes import with current library', () => {
    assert.match(source, /limit:\s*500/);
    assert.ok(source.includes("path: '/import'"));
    assert.ok(source.includes("mode: 'add'"));
});
```

- [ ] **Step 2: Run the structural test and verify it fails**

Run:

```powershell
Set-Location admin-ui
node --test documents_redesign.test.mjs
```

Expected: FAIL because the current page does not contain the approved sections.

- [ ] **Step 3: Add imports and page state**

Update the imports in `admin-ui/src/views/Documents.js`:

```js
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store, hasPermission } from '../store.js';
import { readableLibraries, resolveSelectedSlug } from '../menu_access.js';
import {
    documentDisplayName,
    documentStatusLabel,
    documentStatusTag,
    documentType,
    filterDocuments,
    formatDocumentTime,
    paginateDocuments,
} from '../documents_ui.js';
```

Inside `setup()` add:

```js
const router = useRouter();
const filters = reactive({ keyword: '', status: '', type: '', dateRange: [] });
const page = ref(1);
const pageSize = ref(10);
const detail = reactive({ open: false, row: null, jobs: [], loading: false });
const isSuperuser = computed(() => Boolean(store.user?.is_superuser));
const processingCount = computed(() =>
    Number(stats.value?.pending_jobs || 0) + Number(stats.value?.processing_jobs || 0)
);
const filteredDocs = computed(() => filterDocuments(docs.value, filters));
const pagination = computed(() => paginateDocuments(filteredDocs.value, page.value, pageSize.value));
const visibleDocs = computed(() => pagination.value.items);
const partialList = computed(() =>
    Number(stats.value?.document_count || 0) > docs.value.length
);
```

Change the document request to:

```js
api.listDocuments(slug.value, { limit: 500 })
```

If no slug is selected, `loadDocs()` must set `docs.value = []` and `stats.value = null` before returning.

- [ ] **Step 4: Add filter, route, detail, and retry behavior**

Add these functions inside `setup()`:

```js
function resetFilters() {
    filters.keyword = '';
    filters.status = '';
    filters.type = '';
    filters.dateRange = [];
    page.value = 1;
    pageSize.value = 10;
}

function openFileImport() {
    if (!slug.value || !canInsert.value) return;
    router.push({ path: '/import', query: { library: slug.value, mode: 'add' } });
}

async function openDetail(row) {
    detail.open = true;
    detail.row = row;
    detail.jobs = [];
    await loadDetailJobs(row);
}

async function loadDetailJobs(row) {
    if (!row || !slug.value) return;
    detail.loading = true;
    try {
        detail.jobs = await api.listDocumentJobs(slug.value, row.id);
    } catch (e) {
        ElMessage.error(`加载文档任务失败：${e.message || String(e)}`);
    } finally {
        detail.loading = false;
    }
}

async function retryJob(job) {
    if (!isSuperuser.value || job.status !== 'failed') return;
    try {
        await ElMessageBox.confirm('确认重新提交这个失败任务？', '重试任务', { type: 'warning' });
        await api.retryJob(job.id);
        ElMessage.success('任务已重新提交');
        await Promise.all([loadDetailJobs(detail.row), loadDocs()]);
    } catch (e) {
        if (e !== 'cancel') ElMessage.error(e.message || String(e));
    }
}

async function retryDocument(row) {
    if (!isSuperuser.value || row.status !== 'failed') return;
    detail.row = row;
    try {
        const jobs = await api.listDocumentJobs(slug.value, row.id);
        const failedJob = jobs.find((job) => job.status === 'failed');
        if (!failedJob) {
            ElMessage.warning('未找到可重试的失败任务');
            return;
        }
        detail.jobs = jobs;
        await retryJob(failedJob);
    } catch (e) {
        ElMessage.error(`加载文档任务失败：${e.message || String(e)}`);
    }
}

function metadataText(row) {
    if (!row?.metadata) return '—';
    try { return JSON.stringify(row.metadata, null, 2); }
    catch (_) { return '—'; }
}
```

Add watchers:

```js
watch(
    () => [filters.keyword, filters.status, filters.type, ...(filters.dateRange || [])],
    () => { page.value = 1; },
);
watch(pageSize, () => { page.value = 1; });
watch(slug, async () => {
    detail.open = false;
    detail.row = null;
    resetFilters();
    await loadDocs();
});
watch(() => pagination.value.page, (validPage) => {
    if (page.value !== validPage) page.value = validPage;
});
```

In `loadLibs()`, replace direct slug assignment with:

```js
const nextSlug = resolveSelectedSlug(slug.value, libs.value);
if (nextSlug === slug.value) {
    slug.value = nextSlug;
    await loadDocs();
} else {
    slug.value = nextSlug;
}
```

Use `onMounted(loadLibs)`. Remove the old `watch(slug, loadDocs)` and the old
`onMounted(async () => { await loadLibs(); await loadDocs(); })`. This guarantees exactly one
initial request when a readable library exists and clears the page without requesting when none exists.

- [ ] **Step 5: Replace the template with the approved four-part hierarchy**

Replace the old page header, stats row, and table with this concrete structure. Keep the existing
create/edit `<el-dialog>` immediately after the drawer without changing its fields or submission behavior.

```html
<div class="documents-workspace">
  <section class="documents-overview">
    <div class="documents-heading">
      <div class="documents-eyebrow">KNOWLEDGE DOCUMENTS</div>
      <h2>文档管理</h2>
      <el-select v-model="slug" placeholder="选择知识库" style="width: 100%">
        <el-option v-for="l in myLibs" :key="l.slug"
                   :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
      </el-select>
    </div>
    <div class="documents-stats">
      <div class="documents-stat"><span>文档总数</span><b class="documents-stat-value">{{ stats?.document_count || 0 }}</b></div>
      <div class="documents-stat"><span>处理中</span><b class="documents-stat-value">{{ processingCount }}</b></div>
      <div class="documents-stat"><span>已完成</span><b class="documents-stat-value">{{ stats?.done_jobs || 0 }}</b></div>
      <div class="documents-stat"><span>失败</span><b class="documents-stat-value documents-stat-danger">{{ stats?.failed_jobs || 0 }}</b></div>
    </div>
    <div class="documents-actions">
      <el-button :disabled="!canInsert" :title="canInsert ? '' : '没有写入权限'" @click="openFileImport">文件导入</el-button>
      <el-button type="primary" :disabled="!canInsert" :title="canInsert ? '' : '没有写入权限'" @click="openIngest">提交文本</el-button>
    </div>
  </section>

  <el-alert v-if="!myLibs.length" title="当前账号没有可读取的知识库"
            type="info" :closable="false" show-icon />

  <section class="documents-filters">
    <el-input v-model="filters.keyword" clearable placeholder="搜索文件名、external_id 或文档 ID" />
    <el-select v-model="filters.status" clearable placeholder="全部状态">
      <el-option label="等待中" value="pending" />
      <el-option label="处理中" value="processing" />
      <el-option label="完成" value="ready" />
      <el-option label="失败" value="failed" />
    </el-select>
    <el-select v-model="filters.type" clearable placeholder="全部类型">
      <el-option label="PDF" value="pdf" />
      <el-option label="Word" value="word" />
      <el-option label="Excel" value="excel" />
      <el-option label="Markdown" value="markdown" />
      <el-option label="文本" value="text" />
      <el-option label="其他" value="other" />
    </el-select>
    <el-date-picker v-model="filters.dateRange" type="daterange" value-format="YYYY-MM-DD"
                    start-placeholder="开始日期" end-placeholder="结束日期" style="width: 100%" />
    <div class="documents-filter-actions">
      <el-button @click="resetFilters">重置</el-button>
      <el-button :loading="loading" @click="loadDocs">刷新</el-button>
    </div>
  </section>

  <section class="documents-table-panel">
    <div v-if="partialList" class="documents-load-note">
      当前加载 {{ docs.length }} / 总计 {{ stats.document_count }}，筛选与分页仅作用于已加载文档
    </div>
    <div class="documents-table-shell">
      <el-table :data="visibleDocs" v-loading="loading" empty-text="当前条件下暂无文档">
        <el-table-column label="文件名" min-width="250">
          <template #default="{row}">
            <div class="documents-file">
              <span class="documents-file-type">{{ documentType(row) }}</span>
              <span class="documents-file-name" :title="documentDisplayName(row)">{{ documentDisplayName(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{row}">
            <el-tag :type="documentStatusTag(row.status)" size="small">{{ documentStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{row}">v{{ row.current_revision || 0 }}</template>
        </el-table-column>
        <el-table-column label="external_id" min-width="150" show-overflow-tooltip>
          <template #default="{row}">{{ row.external_id || '—' }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{row}">{{ formatDocumentTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{row}">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button link type="primary" :disabled="!canInsert" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="isSuperuser && row.status === 'failed'" link type="warning"
                       @click="retryDocument(row)">重试</el-button>
            <el-button link type="danger" :disabled="!canDelete" @click="del(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="documents-pagination">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize"
                     :page-sizes="[10, 20, 50]" :total="pagination.total"
                     layout="total, sizes, prev, pager, next" />
    </div>
  </section>

  <el-drawer v-model="detail.open" class="documents-detail" title="文档详情" size="520px">
    <template v-if="detail.row">
      <div class="documents-detail-meta">
        <span>标题</span><b>{{ documentDisplayName(detail.row) }}</b>
        <span>文档 ID</span><span class="mono">{{ detail.row.id }}</span>
        <span>external_id</span><span>{{ detail.row.external_id || '—' }}</span>
        <span>状态</span><el-tag :type="documentStatusTag(detail.row.status)">{{ documentStatusLabel(detail.row.status) }}</el-tag>
        <span>版本</span><span>v{{ detail.row.current_revision || 0 }}</span>
        <span>content hash</span><span class="mono">{{ detail.row.content_hash || '—' }}</span>
        <span>创建时间</span><span>{{ formatDocumentTime(detail.row.created_at) }}</span>
        <span>更新时间</span><span>{{ formatDocumentTime(detail.row.updated_at) }}</span>
        <span>最后错误</span><span class="documents-error">{{ detail.row.last_error || '—' }}</span>
      </div>
      <h3>Metadata</h3>
      <pre class="documents-detail-json">{{ metadataText(detail.row) }}</pre>
      <h3>摄入任务</h3>
      <div v-loading="detail.loading">
        <el-empty v-if="!detail.loading && !detail.jobs.length" description="暂无摄入任务" />
        <article v-for="job in detail.jobs" :key="job.id" class="documents-job">
          <div class="documents-job-head">
            <el-tag :type="documentStatusTag(job.status)">{{ documentStatusLabel(job.status) }}</el-tag>
            <el-button v-if="isSuperuser && job.status === 'failed'" link type="warning"
                       @click="retryJob(job)">重试</el-button>
          </div>
          <div>创建：{{ formatDocumentTime(job.created_at) }}</div>
          <div>开始：{{ formatDocumentTime(job.claimed_at) }}</div>
          <div>结束：{{ formatDocumentTime(job.finished_at) }}</div>
          <div>尝试次数：{{ job.attempt_count ?? '—' }}</div>
          <div v-if="job.last_error" class="documents-error">{{ job.last_error }}</div>
        </article>
      </div>
    </template>
  </el-drawer>

</div>
```

Use `documentDisplayName(row)`, `documentType(row)`, `documentStatusLabel(row.status)`,
`documentStatusTag(row.status)`, and `formatDocumentTime(...)` in the table/drawer. Do not call
`listDocumentJobs` per table row. Do not add raw-content, download, bulk-delete, chunk-count,
or version-body controls. Move the current create/edit `<el-dialog>` as one intact block so it
remains inside `.documents-workspace`, after the new drawer and before the closing `</div>`.

- [ ] **Step 6: Return every new binding used by the template**

The `setup()` return object must include:

```js
return {
    myLibs, slug, docs, stats, loading, canInsert, canDelete, isSuperuser,
    processingCount, filters, page, pageSize, pagination, visibleDocs, partialList,
    detail, dialog, loadDocs, resetFilters, openFileImport, openIngest, openEdit,
    openDetail, retryJob, retryDocument, submitIngest, del, metadataText,
    documentDisplayName, documentStatusLabel, documentStatusTag, documentType,
    formatDocumentTime,
};
```

- [ ] **Step 7: Run focused and full tests**

Run:

```powershell
node --test documents_ui.test.mjs documents_redesign.test.mjs
node --check src/views/Documents.js
node --test
Set-Location ..
```

Expected: all tests and syntax checks pass.

- [ ] **Step 8: Commit the document workspace behavior**

Run:

```powershell
git add -- admin-ui/src/views/Documents.js admin-ui/documents_redesign.test.mjs
git diff --cached --name-status
git commit -m "feat(ui): rebuild documents workspace"
```

Expected: exactly these two paths are committed.

### Task 4: Add Responsive Documents Styling

**Files:**
- Modify: `admin-ui/style.css`
- Modify: `admin-ui/documents_redesign.test.mjs`

- [ ] **Step 1: Extend the structural test with responsive contracts**

Read `style.css` in the test and assert:

```js
const css = await readFile(new URL('./style.css', import.meta.url), 'utf8');

test('defines responsive document workspace styling', () => {
    for (const token of [
        '.documents-workspace',
        '.documents-overview',
        '.documents-stats',
        '.documents-filters',
        '.documents-table-shell',
        '.documents-detail',
        '@media (max-width: 1199px)',
        '@media (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing ${token}`);
    assert.match(css, /\.documents-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
});
```

- [ ] **Step 2: Run the test and verify the CSS assertions fail**

Run:

```powershell
Set-Location admin-ui
node --test documents_redesign.test.mjs
```

Expected: FAIL because the document workspace styles are absent.

- [ ] **Step 3: Add scoped document page styles**

Append a single consolidated “Documents workspace” block to `admin-ui/style.css`. Use the existing
green theme variables already defined in the file and implement these declarations:

```css
.documents-workspace { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.documents-overview,
.documents-filters,
.documents-table-panel {
    border: 1px solid var(--app-border);
    border-radius: 14px;
    background: #fff;
    box-shadow: 0 8px 24px rgba(30, 70, 54, .06);
}
.documents-overview { display: grid; grid-template-columns: minmax(220px, 1fr) 2fr auto; gap: 24px; padding: 22px 24px; align-items: center; }
.documents-stats { display: grid; grid-template-columns: repeat(4, minmax(92px, 1fr)); gap: 12px; }
.documents-stat { padding: 12px 14px; border-radius: 10px; background: var(--app-panel-2); }
.documents-stat-value { display: block; margin-top: 6px; font-size: 24px; font-weight: 700; color: var(--app-text); }
.documents-stat-danger,
.documents-error { color: var(--app-danger); }
.documents-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.documents-filters { display: grid; grid-template-columns: minmax(220px, 1fr) 150px 150px minmax(250px, 1fr) auto; gap: 12px; padding: 16px 18px; align-items: center; }
.documents-table-panel { min-width: 0; padding: 18px; }
.documents-load-note { margin-bottom: 12px; color: var(--app-text-secondary); font-size: 13px; }
.documents-table-shell { width: 100%; overflow-x: auto; }
.documents-table-shell .el-table { min-width: 920px; }
.documents-file { display: flex; align-items: center; gap: 10px; min-width: 0; }
.documents-file-type { flex: 0 0 auto; min-width: 42px; padding: 4px 7px; border-radius: 6px; background: var(--app-primary-soft); color: var(--app-primary); text-transform: uppercase; text-align: center; font-size: 11px; font-weight: 700; }
.documents-file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.documents-pagination { display: flex; justify-content: flex-end; margin-top: 16px; }
.documents-detail-meta { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px 14px; }
.documents-detail-json { margin: 0; padding: 12px; overflow: auto; border-radius: 8px; background: var(--app-panel-2); white-space: pre-wrap; word-break: break-word; }
.documents-job { margin-top: 12px; padding: 14px; border: 1px solid var(--app-border); border-radius: 10px; }
.documents-job-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
```

Merge these declarations into the existing media blocks instead of creating duplicate
`@media` blocks when matching blocks already exist:

```css
@media (max-width: 1199px) {
    .documents-overview { grid-template-columns: 1fr 1fr; }
    .documents-stats { grid-column: 1 / -1; order: 3; }
    .documents-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 899px) {
    .documents-overview { grid-template-columns: 1fr; padding: 18px; }
    .documents-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .documents-actions { justify-content: stretch; }
    .documents-actions .el-button { flex: 1; margin-left: 0; }
    .documents-filters { grid-template-columns: 1fr; }
    .documents-table-panel { padding: 12px; }
    .documents-pagination { justify-content: flex-start; overflow-x: auto; }
    .documents-detail-meta { grid-template-columns: 1fr; }
}
```

The table shell may scroll horizontally at 899px, but the page body must not.

- [ ] **Step 4: Run tests and inspect CSS duplication**

Run:

```powershell
node --test documents_redesign.test.mjs
node --test
Set-Location ..
Select-String -Path admin-ui/style.css -Pattern '@media \\(max-width: 1199px\\)|@media \\(max-width: 899px\\)|\\.documents-'
```

Expected: tests pass; document selectors are in one consolidated block and responsive rules are merged into existing matching media queries.

- [ ] **Step 5: Commit only the page styling**

Run:

```powershell
git add -- admin-ui/style.css admin-ui/documents_redesign.test.mjs
git diff --cached --name-status
git commit -m "style(ui): add responsive documents workspace"
```

Expected: exactly these two paths are committed.

### Task 5: Verify Behavior, Layout, and Scope

**Files:**
- Verify only; modify only files already listed in this plan if a defect is found.

- [ ] **Step 1: Run all frontend tests and syntax checks**

Run:

```powershell
Set-Location admin-ui
node --test
Get-ChildItem -Recurse -File -Include *.js,*.mjs | ForEach-Object { node --check $_.FullName }
Set-Location ..
```

Expected: zero failed tests and every JS/MJS file exits successfully.

- [ ] **Step 2: Run repository safety checks**

Run:

```powershell
python scripts/check_release_safety.py
git diff --check
git diff --name-only HEAD~4..HEAD
```

Expected: release safety passes; no whitespace errors; the four feature commits contain only the frontend files listed in Tasks 1-4. The separate chat follow-up commit may contain only the six paths from Task 0.

- [ ] **Step 3: Prove no backend or API surface was changed**

Run:

```powershell
git diff --name-only HEAD~5..HEAD -- app admin-ui/src/api.js admin-ui/src/store.js
git diff --stat HEAD~5..HEAD
```

Expected: the first command prints nothing. If it prints any path, stop and report the scope violation rather than hiding it.

- [ ] **Step 4: Verify the page in a real browser at three widths**

Open:

```text
http://127.0.0.1:5599/?preview=1#/documents
```

At 1920px verify: overview hierarchy is clear, four stats align, filters fit on one row, table and pagination are readable, detail drawer opens, and console has no errors.

At 1199px verify: overview and actions wrap cleanly, filters use two columns, and the page has no horizontal overflow.

At 899px verify: controls stack, both primary actions remain usable, only the table shell scrolls horizontally, the page itself has no horizontal overflow, and the drawer remains usable.

Exercise these interactions with preview fixtures or a running backend:

1. Change library and confirm filters/page reset.
2. Combine keyword, status, type, and date filters.
3. Change page size among 10/20/50 and move pages.
4. Click “文件导入” and confirm the URL carries `library` and `mode=add`.
5. Open details and confirm task loading failure does not close the drawer.
6. Confirm retry is absent for normal users and only appears for failed tasks as superuser.
7. Confirm edit/delete keep their original permission and confirmation behavior.

- [ ] **Step 5: Report backend-dependent residual verification honestly**

If no backend service is running, report that actual ingest, edit, delete, task retrieval, and retry
were verified by code paths/tests but still require one integration pass against a running backend.
Do not claim end-to-end success without that evidence.

- [ ] **Step 6: Review final worktree and commits**

Run:

```powershell
git status --short
git log -5 --oneline
```

Expected feature commits:

```text
test(ui): add document list helpers
feat(ui): connect document import route query
feat(ui): rebuild documents workspace
style(ui): add responsive documents workspace
```

Do not push. Report all remaining dirty/untracked paths separately as pre-existing or unrelated.
