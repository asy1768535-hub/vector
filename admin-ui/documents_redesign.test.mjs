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
    const overviewBase = css.indexOf('.documents-overview { display: grid; grid-template-columns: minmax(220px, 1fr) 2fr auto');
    const overview899 = css.indexOf('@media (max-width: 899px)');
    const filtersBase = css.indexOf('.documents-filters { display: grid; grid-template-columns: minmax(220px, 1fr) 150px 150px');
    assert.ok(overviewBase > 0, 'documents-overview base rule exists');
    assert.ok(overview899 > 0, '899px media query exists');
    assert.ok(filtersBase > 0, 'documents-filters base rule exists');
    assert.ok(overviewBase < overview899, 'documents-overview base rule must come before 899px media query');
    assert.ok(filtersBase < overview899, 'documents-filters base rule must come before 899px media query');
});

test('blocks write actions when no library is selected', () => {
    assert.ok(source.includes("Boolean(slug.value) && (store.user?.is_superuser || hasPermission(slug.value, 'insert'))"), 'canInsert requires slug');
    assert.ok(source.includes("Boolean(slug.value) && (store.user?.is_superuser || hasPermission(slug.value, 'delete'))"), 'canDelete requires slug');
    for (const guard of [
        "if (!slug.value || !canInsert.value) return;",
        "if (!slug.value) {",
        "if (!slug.value || !canInsert.value) return;",
        "if (!slug.value) return;",
        "if (!slug.value || !isSuperuser.value || row.status !== 'failed') return;",
    ]) {
        assert.ok(source.includes(guard), `missing slug guard: ${guard}`);
    }
    assert.ok(source.includes("ElMessage.warning('请先选择知识库');"), 'missing no-library warning');
    assert.ok(source.includes("slug ? ('向 ' + slug + ' 提交文档') : '提交文档'"), 'dialog title avoids null');
});
