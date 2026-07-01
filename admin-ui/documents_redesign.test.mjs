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
});
