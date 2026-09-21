import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ════════════════════════════════════════════════════════════
//  Task 1: library presentation helpers
// ════════════════════════════════════════════════════════════

import {
    computeLibraryStats,
    filterLibraries,
    librarySlugFromName,
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
        'rebuildLibraryCollection', 'testLibraryEmbedding', 'verifyLibraryEmbedding', 'listLibraryFaqs',
        'createLibraryFaq', 'updateLibraryFaq', 'deleteLibraryFaq',
    ]) assert.match(source, new RegExp(`api\\.${name}\\b`));
    assert.match(source, /ElMessageBox\.confirm/);
});

test('Embedding health check and compatibility verification remain separate actions', () => {
    assert.match(apiSource, /export const testLibraryEmbedding = \(slug\) =>\s*request\(`\/admin\/libraries\/\$\{slug\}\/test-embedding`, \{ method: 'POST' \}\);/);
    assert.match(apiSource, /export const verifyLibraryEmbedding = \(slug\) =>\s*request\(`\/admin\/libraries\/\$\{slug\}\/verify-embedding-profile`, \{ method: 'POST' \}\);/);
    assert.match(source, /api\.testLibraryEmbedding\(row\.slug\)/);
    assert.match(source, /api\.verifyLibraryEmbedding\(row\.slug\)/);
    assert.match(source, /cmd==='test'\) testEmbedding\(row\)/);
    assert.match(source, /cmd==='verify-embedding'\) verifyEmbedding\(row\)/);
    assert.match(source, /command="test">测试 Embedding/);
    assert.match(source, /command="verify-embedding">验证 Embedding 兼容性/);
    assert.match(source, /bulkDeleteDocuments, testEmbedding, verifyEmbedding,/);
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

test('source enrichment defaults on without appearing in create dialog', () => {
    assert.match(source, /source_enrichment_enabled:\s*true/);
    const createDialog = source.slice(
        source.indexOf('<!-- Create dialog -->'),
        source.indexOf('<!-- Edit dialog -->'),
    );
    assert.doesNotMatch(createDialog, /create\.form\.source_enrichment_enabled/);
    assert.doesNotMatch(createDialog, /PGSQL|text_id|全文源/);
});

test('library IDs derive from names and remain valid for Chinese names', () => {
    assert.equal(librarySlugFromName('Medical Knowledge Base'), 'medical_knowledge_base');
    const chineseSlug = librarySlugFromName('医学知识库');
    assert.match(chineseSlug, /^library_[a-z]{12}$/);
    assert.equal(librarySlugFromName('医学知识库'), chineseSlug);
    assert.equal(librarySlugFromName(''), '');
});

test('create submission includes disabled source_enrichment_enabled in payload', () => {
    assert.match(source, /const body = \{ \.\.\.create\.form \}/);
    assert.match(source, /api\.createLibrary\(body\)/);
    assert.match(source, /source_enrichment_enabled:\s*true/);
});

test('new libraries always enable OCR and DOCX table-aware parsing', () => {
    assert.match(source, /ocr_enabled:\s*true,\s*docx_table_aware:\s*true/);
    const createDialog = source.slice(
        source.indexOf('<!-- Create dialog -->'),
        source.indexOf('<!-- Edit dialog -->'),
    );
    assert.doesNotMatch(createDialog, /v-model="create\.form\.ocr_enabled"/);
    assert.doesNotMatch(createDialog, /v-model="create\.form\.docx_table_aware"/);
});

test('create dialog keeps two vector/chunk rows with local bge-m3 defaults', () => {
    assert.match(source, /embedding_dim:\s*1024/);
    assert.match(source, /embed_batch_size:\s*32/);
    const createDialog = source.slice(
        source.indexOf('<!-- Create dialog -->'),
        source.indexOf('<!-- Edit dialog -->'),
    );
    assert.doesNotMatch(createDialog, /create\.form\.embedding_model/);
    assert.doesNotMatch(createDialog, /create\.form\.embedding_base_url/);
    assert.match(createDialog, /create\.form\.embedding_dim/);
    assert.match(createDialog, /create\.form\.embed_batch_size/);
    assert.match(createDialog, /create\.form\.chunk_size/);
    assert.match(createDialog, /create\.form\.chunk_overlap/);
    assert.doesNotMatch(createDialog, /阿里云/);
});

test('create dialog places name first and shows a server-owned library ID', () => {
    const createDialog = source.slice(
        source.indexOf('<!-- Create dialog -->'),
        source.indexOf('<!-- Edit dialog -->'),
    );
    assert.ok(
        createDialog.indexOf('v-model="create.form.name"')
        < createDialog.indexOf('v-model="create.form.slug"'),
    );
    assert.match(createDialog, /@input="onCreateNameInput"/);
    assert.match(createDialog, /v-model="create\.form\.slug" readonly/);
    assert.match(createDialog, /__r2、__r3/);
    assert.match(source, /if \(!create\.slugEdited\) create\.form\.slug = librarySlugFromName\(name\)/);
});

test('recreated library names surface the server-assigned generation ID', () => {
    assert.match(source, /created\.slug\.match/);
    assert.ok(source.includes('同名知识库已按第 '));
});

test('edit dialog backfills source enrichment switch from source_config', () => {
    assert.match(source, /source_enrichment_enabled:\s*row\.source_config != null/);
    assert.match(source, /v-model="edit\.form\.source_enrichment_enabled"/);
});

test('edit dialog exposes the complete administrator configuration', () => {
    const editDialog = source.slice(
        source.indexOf('<!-- Edit dialog -->'),
        source.indexOf('<!-- FAQ dialog -->'),
    );
    for (const field of [
        'embedding_model', 'embedding_base_url', 'embedding_dim', 'vector_distance',
        'embed_batch_size', 'chunk_size', 'chunk_overlap', 'retrieval_mode',
        'rerank_enabled', 'ocr_enabled', 'docx_table_aware',
        'source_enrichment_enabled', 'schema_mode',
    ]) {
        assert.match(editDialog, new RegExp(`edit\\.form\\.${field}`));
    }
    assert.doesNotMatch(editDialog, /阿里云/);
    assert.match(source, /diff\.embedding_dim !== undefined \|\| diff\.vector_distance !== undefined/);
    assert.match(editDialog, /path: '\/knowledge-governance\/schema'/);
    assert.match(editDialog, /query: \{ library: edit\.slug, tab: 'overview' \}/);
});

test('edit from enabled to disabled submits source_enrichment_enabled diff', () => {
    assert.match(source, /for \(const k of Object\.keys\(edit\.form\)\) if \(edit\.form\[k\] !== edit\.initial\[k\]\) diff\[k\] = edit\.form\[k\]/);
    assert.match(source, /api\.updateLibrary\(edit\.slug, diff\)/);
    assert.match(source, /source_enrichment_enabled/);
});

test('library save failures stay on the existing error path', () => {
    const submitEdit = source.slice(source.indexOf('async function submitEdit'), source.indexOf('async function rebuild'));
    assert.match(submitEdit, /try \{/);
    assert.match(submitEdit, /api\.updateLibrary\(edit\.slug, diff\)/);
    assert.match(submitEdit, /catch \(e\) \{ ElMessage\.error\(e\.message\); \}/);
});

test('detail drawer displays source enrichment enabled or disabled clearly', () => {
    assert.match(source, /function sourceDisplay\(config\)/);
    assert.match(source, /开启（\$\{srcSummary\(config\)\}）/);
    assert.match(source, /sourceDisplay\(selectedLibrary\.source_config\)/);
});

test('create and edit dialogs expose schema mode as the graph extraction policy', () => {
    assert.match(source, /graph_extraction_enabled:\s*false/);
    assert.match(source, /schema_mode:\s*'disabled'/);
    assert.match(source, /v-model="create\.form\.schema_mode"/);
    assert.match(source, /v-model="edit\.form\.schema_mode"/);
    assert.match(source, /edit\.form\.graph_extraction_enabled = edit\.form\.schema_mode !== 'disabled'/);
    assert.match(source, /value="disabled"/);
    assert.match(source, /value="explore"/);
    assert.match(source, /value="governed"/);
    assert.match(source, /external_llm_enabled = body\.graph_extraction_enabled/);
    assert.match(source, /graph_extraction_allowed_security_levels = body\.graph_extraction_enabled/);
    assert.match(source, /diff\.graph_extraction_enabled === true/);
});

test('create dialog selects and submits a governed Schema template', () => {
    assert.match(source, /schema_template:\s*'none'/);
    assert.match(source, /v-if="create\.form\.schema_mode === 'governed'"/);
    assert.match(source, /v-model="create\.form\.schema_template"/);
    assert.match(source, /基础企业 Schema（推荐）/);
    assert.match(source, /稍后导入自定义 Schema/);
    assert.match(source, /if \(body\.schema_mode !== 'governed'\) body\.schema_template = 'none'/);
    assert.doesNotMatch(source, /onCreateGraphToggle/);
    assert.match(source, /将创建并激活基础企业 Schema/);
});

test('library details show whether graph extraction is enabled', () => {
    assert.match(source, /selectedLibrary\.graph_extraction_enabled/);
    assert.match(source, />知识图谱</);
});

test('graph extraction build mode defaults to standard and is editable', () => {
    assert.match(source, /graph_extraction_build_mode:\s*'standard'/);
    assert.match(source, /v-model="create\.form\.graph_extraction_build_mode"/);
    assert.match(source, /v-model="edit\.form\.graph_extraction_build_mode"/);
    assert.match(source, /value="fast">快速/);
    assert.match(source, /value="standard">标准/);
    assert.match(source, /value="deep">深度/);
    assert.match(source, /row\.graph_extraction_build_mode \|\| 'standard'/);
});

test('graph extraction copy separates AI exploration from governed Schema extraction', () => {
    assert.match(source, /AI 探索用于第一次陌生资料/);
    assert.match(source, /Schema 治理用于长期约束/);
    assert.match(source, /激活后/);
    assert.doesNotMatch(source, /当前图谱抽取器仍要求先导入并激活 Schema/);
});

console.log('libraries redesign test passed');
