import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { buildFolderTree, documentsInFolder } from './src/folder_tree.js';

const source = fs.readFileSync(new URL('./src/views/Documents.js', import.meta.url), 'utf8');
const apiSource = fs.readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');


test('folder tree preserves parent-child hierarchy', () => {
    const folders = [
        { id: 'contracts', parent_id: 'project', name: '合同', path: '/项目甲/合同' },
        { id: 'project', parent_id: null, name: '项目甲', path: '/项目甲' },
    ];
    const tree = buildFolderTree(folders, [{ folder_id: 'contracts' }]);
    assert.equal(tree[0].label, '项目甲');
    assert.equal(tree[0].children[0].label, '合同');
    assert.equal(tree[0].children[0].count, 1);
});

test('folder filtering distinguishes root and named folders', () => {
    const docs = [
        { id: 'root', folder_id: null },
        { id: 'nested', folder_id: 'folder-1' },
    ];
    assert.deepEqual(documentsInFolder(docs, 'root').map((doc) => doc.id), ['root']);
    assert.deepEqual(documentsInFolder(docs, 'folder-1').map((doc) => doc.id), ['nested']);
    assert.equal(documentsInFolder(docs, 'all').length, 2);
});

test('documents page loads and renders the existing folder API', () => {
    for (const token of [
        'api.listFolders',
        'buildFolderTree',
        'documents-folder-pane',
        'selectedFolderId',
        'row.source_path',
    ]) {
        assert.ok(source.includes(token), `missing folder tree token: ${token}`);
    }
    assert.match(apiSource, /\/libraries\/\$\{slug\}\/folders/);
    assert.match(css, /\.documents-browser-layout\s*\{/);
    assert.match(css, /\.documents-folder-pane\s*\{/);
});
