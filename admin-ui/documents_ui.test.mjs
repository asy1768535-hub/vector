import test from 'node:test';
import assert from 'node:assert/strict';
import {
    documentDisplayName,
    documentStatusLabel,
    documentStatusTag,
    documentType,
    documentTypeIcon,
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

test('maps document type to icon name for all six types', () => {
    assert.equal(documentTypeIcon({ title: 'a.pdf' }), 'doc:pdf');
    assert.equal(documentTypeIcon({ title: 'a.DOCX' }), 'doc:word');
    assert.equal(documentTypeIcon({ title: 'a.xlsx' }), 'doc:excel');
    assert.equal(documentTypeIcon({ title: 'a.markdown' }), 'doc:markdown');
    assert.equal(documentTypeIcon({ title: 'a.txt' }), 'doc:text');
    assert.equal(documentTypeIcon({ title: 'a.bin' }), 'doc:other');
    assert.equal(documentTypeIcon({}), 'doc:other');
});
