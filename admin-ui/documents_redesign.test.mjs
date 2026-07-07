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

test('declares detail state used by the document drawer', () => {
    assert.match(
        source,
        /const\s+detail\s*=\s*reactive\s*\(\s*\{\s*open:\s*false,\s*row:\s*null,\s*jobs:\s*\[\],\s*loading:\s*false/s,
        'detail drawer state must be declared before computed/template usage'
    );
});

test('details open a real full source reader with states and search', () => {
    assert.ok(source.includes('阅读原文'), 'detail has source entry label');
    assert.ok(source.includes('api.getDocumentFullSource'), 'document detail calls full source API');
    assert.ok(source.includes('openFullSource(detail.row)'), 'source button opens current document source');
    for (const token of [
        'documents-source-dialog',
        '正在加载原文...',
        '该文档缺少原文快照，请重新导入后再阅读原文。',
        '原文快照为空',
        'documents-source-text',
        'highlightedSourceParts',
        'sourceMatchCount',
        '搜索原文关键词',
        'normalized_text',
    ]) assert.ok(source.includes(token), `missing source reader token: ${token}`);
    assert.equal(source.includes('api.getDocumentSource'), false, 'document detail must not call chunk-scoped source API without chunk_id');
    assert.equal(source.includes('需从问答引用或切片定位进入原文位置'), false, 'chunk-id degradation is no longer the only behavior');
    assert.ok(source.includes('downloadOriginalFile'), 'source reader does not handle original file download');
});

test('source information keeps reading and original-file download separate', () => {
    assert.ok(source.includes('阅读原文'), 'read original text button exists');
    assert.ok(source.includes('下载原文件'), 'download original file button exists');
    assert.ok(source.includes('api.getDocumentFullSource'), 'reading uses normalized text endpoint');
    assert.ok(source.includes('api.downloadDocumentFile'), 'download uses original file endpoint');
    assert.ok(source.includes('该文档缺少原始文件，请重新导入后再下载'), 'missing original file message exists');
    assert.ok(source.includes('阅读原文使用 normalized_text 快照'), 'copy separates normalized text reading');
});
test('edit copy separates metadata from overwrite/reimport semantics', () => {
    for (const token of [
        '基础信息编辑',
        'external_id 当前版本不可修改',
        '覆盖文档内容（文本覆盖）',
        '会替换正文、重新切分、重新向量化，旧问答引用不会自动更新',
        '重新导入并覆盖此文档',
        '保存文本覆盖并重新向量化',
    ]) assert.ok(source.includes(token), `missing edit wording: ${token}`);
    assert.ok(source.includes("path: '/import'"), 'reimport routes to import page');
    assert.ok(source.includes("mode: 'replace'"), 'reimport uses replace mode');
    assert.ok(source.includes('replaceDocumentId: row.id'), 'reimport carries target id');
    assert.ok(source.includes('replaceTitle: documentDisplayName(row)'), 'reimport carries target title');
});

test('delete remains permission-gated and explains risks', () => {
    assert.ok(source.includes("Boolean(slug.value) && (store.user?.is_superuser || hasPermission(slug.value, 'delete'))"), 'canDelete requires slug and delete permission/superuser');
    assert.ok(source.includes('if (!canDelete.value)'), 'delete handler guards canDelete');
    assert.ok(source.includes(':disabled="!canDelete"'), 'delete button disabled without permission');
    assert.ok(source.includes('没有删除权限'), 'disabled delete explains missing permission');
    for (const token of [
        '删除后文档将不可用于后续检索',
        '向量清理为异步执行',
        '历史问答引用不会自动恢复',
    ]) assert.ok(source.includes(token), `missing delete risk: ${token}`);
});

test('contains only approved low-risk actions and no fake backend additions', () => {
    for (const forbidden of ['批量删除', '版本正文', 'downloadDocumentSource', 'getDocumentFullText']) {
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
        '.documents-source-dialog',
        '.documents-source-text',
        '@media (max-width: 1199px)',
        '@media (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing ${token}`);
    assert.match(css, /\.documents-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
    assert.match(css, /\.documents-detail\s*\{[^}]*90vw/s, 'mobile detail width uses 90vw');
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
        "if (!slug.value) return;",
        "if (!slug.value || !isSuperuser.value || row.status !== 'failed') return;",
    ]) {
        assert.ok(source.includes(guard), `missing slug guard: ${guard}`);
    }
    assert.ok(source.includes("ElMessage.warning('请先选择知识库');"), 'missing no-library warning');
    assert.ok(source.includes("slug ? ('向 ' + slug + ' 提交文档') : '提交文档'"), 'dialog title avoids null');
});
