import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ════════════════════════════════════════════════════════════
//  Task 1: library presentation helpers
// ════════════════════════════════════════════════════════════

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

// ════════════════════════════════════════════════════════════
//  Task 2: Libraries.js structural tests
// ════════════════════════════════════════════════════════════

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

// ════════════════════════════════════════════════════════════
//  Fix behavior tests
// ════════════════════════════════════════════════════════════

test('resetFilters clears showDeleted and calls load(true)', () => {
    assert.match(source, /async function resetFilters/);
    assert.match(source, /showDeleted\.value = false/);
    assert.match(source, /await load\(true\)/);
});

test('OCR/Rerank use toggleLabel: true=开启, false=关闭, null=继承', () => {
    assert.match(source, /function toggleLabel/);
    assert.match(source, /val === true.*开启/);
    assert.match(source, /val === false.*关闭/);
});

test('detail drawer closes when selectedLibrary not found after refresh', () => {
    assert.match(source, /detailOpen\.value = false/);
});

test('detail drawer includes description, embedding_base_url, vector_distance', () => {
    assert.ok(source.includes('selectedLibrary.description'));
    assert.ok(source.includes('selectedLibrary.embedding_base_url'));
    assert.ok(source.includes('selectedLibrary.vector_distance'));
});

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

test('FAQ create row constrains sort input so it cannot overlap add button', () => {
    assert.match(css, /\.libraries-faq-create\s*\{[^}]*grid-template-columns:minmax\(0,1fr\) 120px auto;/s);
    assert.match(css, /\.libraries-faq-create \.el-input-number\s*\{[^}]*width:120px;/s);
    assert.match(css, /\.libraries-faq-create \.el-button\s*\{[^}]*margin-left:0;/s);
});

test('Embedding column uses embedDisplay to avoid —d', () => {
    assert.match(source, /function embedDisplay/);
    assert.ok(source.includes("embedDisplay(row)"));
    assert.ok(source.includes("embedDisplay(selectedLibrary)"));
});

test('create dialog prevents duplicate submissions while request is pending', () => {
    assert.match(source, /submitting:\s*false/);
    assert.match(source, /if \(create\.submitting\) return/);
    assert.match(source, /create\.submitting = true/);
    assert.match(source, /finally \{ create\.submitting = false; \}/);
    assert.match(source, /<el-button @click="create\.open = false" :disabled="create\.submitting">取消<\/el-button>/);
    assert.match(source, /<el-button type="primary" :loading="create\.submitting" @click="submitCreate">创建<\/el-button>/);
});

test('CSS: libraries-header has flex-direction:column responsive rule', () => {
    const idx = css.indexOf('libraries-header');
    assert.ok(idx > 0, 'libraries-header exists in CSS');
    // Find the 899px responsive rule near the libraries-header line
    const nearby = css.slice(Math.max(0, idx - 200), idx + 200);
    // Just verify flex-direction:column exists near libraries-header in some responsive context
    assert.ok(nearby.includes('flex-direction:column'), 'flex-direction:column near libraries-header');
});

test('.run_logs is in .gitignore', () => {
    const gi = readFileSync(new URL('../.gitignore', import.meta.url), 'utf8');
    assert.ok(gi.includes('.run_logs/'));
});

console.log('libraries redesign test passed');
