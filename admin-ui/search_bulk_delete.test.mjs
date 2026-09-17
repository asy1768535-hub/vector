import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { bulkDeleteDocuments, listFolders } from './src/api.js';

const searchSource = readFileSync(new URL('./src/views/Search.js', import.meta.url), 'utf8');
const librariesSource = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');

test('folder and bulk-delete APIs use the library document routes', async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (path, options = {}) => {
        requests.push({ path: String(path), options });
        return new Response(JSON.stringify({ folders: [] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await listFolders('folder-api-test', true);
        await bulkDeleteDocuments('bulk-delete-api-test');
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.equal(requests[0].path, '/libraries/folder-api-test/folders');
    assert.equal(requests[0].options.method, undefined);
    assert.equal(requests[1].path, '/libraries/bulk-delete-api-test/documents/bulk-delete');
    assert.equal(requests[1].options.method, 'POST');
});

test('Search loads folders for the selected library and sends folder_id', () => {
    assert.match(searchSource, /api\.listFolders\(requestedSlug, forceRefresh\)/);
    assert.match(searchSource, /<el-form-item label="文件夹">/);
    assert.match(searchSource, /if \(requestedFolderId\) payload\.folder_id = requestedFolderId/);
    assert.match(searchSource, /folderId\.value = ''/);
    assert.match(searchSource, /folders\.value = \[\]/);
});

test('bulk document deletion has its own confirmation and is distinct from library deletion', () => {
    assert.match(librariesSource, /api\.bulkDeleteDocuments\(row\.slug\)/);
    assert.match(librariesSource, /删除库内全部文件/);
    assert.match(librariesSource, /此操作不删除知识库/);
    assert.match(librariesSource, /command="bulk-delete"/);
    assert.match(librariesSource, /ElMessageBox\.confirm/);
});
