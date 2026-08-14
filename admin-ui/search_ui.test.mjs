import assert from 'node:assert/strict';
import test from 'node:test';

import { resultDocumentRef, resultSourceLabel } from './src/search_ui.js';

test('prefers page ranges and falls back through stable source fields', () => {
    assert.equal(resultSourceLabel({ metadata: { page_start: 3, page_end: 4, seq: 7 } }), '第3-4页');
    assert.equal(resultSourceLabel({ metadata: { page_start: 3, page_end: 3 } }), '第3页');
    assert.equal(resultSourceLabel({ metadata: { seq: 7 } }), '片段 7');
    assert.equal(resultSourceLabel({ metadata: { document_revision: 2 } }), '版本 v2');
    assert.equal(resultSourceLabel({ metadata: { source_path: 'folder/source.pdf' } }), 'folder/source.pdf');
    assert.equal(resultSourceLabel({ document_id: '1234567890abcdef' }), '文档 1234567890ab...');
    assert.equal(resultSourceLabel({}), '来源未标注');
});

test('exposes a stable document reference when titles are duplicated', () => {
    assert.equal(resultDocumentRef({
        title: 'same.pdf', document_id: 'doc-001', metadata: { external_id: 'A-01' },
    }), '外部 ID A-01 · 文档 doc-001');
    assert.equal(resultDocumentRef({ title: 'same.pdf', document_id: '1234567890abcdef' }), '文档 1234567890ab...');
});
