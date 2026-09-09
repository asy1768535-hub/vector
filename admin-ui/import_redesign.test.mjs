import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    ALLOWED_EXTENSIONS, MAX_FILE_SIZE, MAX_BATCH_SIZE, LEGACY_SAVE_AS,
    validateFile, validateBatch, createBatchValidationState, validateBatchChunk,
    fileKey, formatSize, fileTypeIcon,
    ST_LABEL, ST_TAG, OP_LABEL, OP_TAG, graphJobProgress, graphProgressDetail,
    SECURITY_LEVEL_LABEL, securityLevelLabel,
} from './src/import_ui.js';
import {
    BATCH_REPLACE_MATCH_LABEL, BATCH_REPLACE_MATCH_TAG,
    BATCH_REPLACE_STATUS_LABEL, BATCH_REPLACE_STATUS_TAG,
    normalizeDocumentName, matchDocumentsForFile, createBatchReplaceItems,
    setBatchReplaceTarget, submittableBatchReplaceItems, submitBatchReplaceItems,
} from './src/batch_replace.js';

const source = readFileSync(new URL('./src/views/Import.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');

// ── Helpers for creating test File-like objects ──
function mockFile(name, size, lastModified = 1700000000000) {
    return { name, size, lastModified };
}

test('graphJobProgress maps real stages and extraction counts', () => {
    const active = graphJobProgress({
        status: 'processing',
        current_stage: 'binding_evidence',
        statistics: { candidate_pipeline: { entity_candidate_count: 7, relation_candidate_count: 4 } },
    });
    assert.equal(active.progress, 70);
    assert.equal(active.terminal, false);
    assert.match(graphProgressDetail(active), /实体 7，关系 4/);

    const completed = graphJobProgress({
        status: 'succeeded',
        statistics: { materialization: { entity_count: 5, relation_count: 3 } },
    });
    assert.equal(completed.progress, 100);
    assert.equal(completed.terminal, true);
    assert.equal(completed.tone, 'success');
});

test('graphJobProgress warns when only entity candidates remain', () => {
    const completed = graphJobProgress({
        status: 'succeeded',
        statistics: {
            materialization: {
                outcome: 'entities_only',
                entity_count: 0,
                relation_count: 0,
                pending_entity_candidate_count: 3,
            },
        },
    });
    assert.equal(completed.tone, 'warning');
    assert.equal(completed.label, '仅有实体、暂无有效关系，未发布');
    assert.equal(completed.entityCount, 3);
    assert.equal(completed.relationCount, 0);
});

test('graphJobProgress reports library graph replacement after entity-only update', () => {
    const completed = graphJobProgress({
        status: 'succeeded',
        statistics: {
            materialization: {
                outcome: 'entities_only',
                pending_entity_candidate_count: 2,
                relation_count: 0,
            },
            publication: { outcome: 'activated' },
        },
    });
    assert.equal(completed.label, '暂无新关系，已更新库级图谱');
    assert.equal(completed.entityCount, 2);
    assert.equal(completed.relationCount, 0);
});

test('graphJobProgress keeps the current graph label when update fails', () => {
    const failed = graphJobProgress({
        status: 'failed',
        statistics: { publication: { current_graph_unchanged: true } },
    });
    assert.equal(failed.label, '图谱更新失败，当前正式图谱未切换');
});

test('localizes security levels without changing submitted enum values', () => {
    assert.equal(SECURITY_LEVEL_LABEL.internal, '内部');
    assert.equal(securityLevelLabel('internal'), '内部');
    assert.equal(securityLevelLabel('restricted'), '受限');
    assert.equal(securityLevelLabel('partner-only'), 'partner-only（自定义安全级别）');
    assert.ok(source.includes('formatSize, fileTypeIcon, securityLevelLabel, MAX_BATCH_SIZE'));
    assert.ok(source.includes(':label="securityLevelLabel(level)" :value="level"'));
    assert.ok(source.includes('外部文档编号'));
    assert.ok(source.includes('用于与外部业务系统中的文档建立对应关系，普通上传无需填写。'));
    assert.ok(source.includes('title="高级设置"'));
    assert.equal(source.includes('placeholder="external_id"'), false);
});

// ════════════════════════════════════════════════════════════
//  Real behavior tests (import_ui.js pure functions)
// ════════════════════════════════════════════════════════════

// ── validateFile ──
test('validateFile accepts all allowed extensions', () => {
    for (const ext of [
        '.txt', '.md', '.markdown', '.rst', '.log', '.ini', '.cfg', '.conf',
        '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.csv', '.tsv',
        '.docx', '.pptx', '.xls', '.xlsx', '.pdf',
        '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp',
    ]) {
        const r = validateFile(mockFile(`doc${ext}`, 1024));
        assert.equal(r.valid, true, `${ext} should be valid`);
    }
});

test('validateFile rejects legacy .doc with save-as message', () => {
    const r = validateFile(mockFile('old.doc', 1024));
    assert.equal(r.valid, false);
    assert.ok(r.reason.includes('.docx'), 'reason mentions .docx');
});

test('validateFile accepts legacy .xls through the bounded xlrd parser', () => {
    const r = validateFile(mockFile('sheet.xls', 1024));
    assert.equal(r.valid, true);
});

test('graph batches create all upload sessions before file transfer', () => {
    const queueBatch = source.indexOf('const batchIds = createImportBatchIds(items);');
    const sessionPreparation = source.indexOf('createImportSessionForFile({', queueBatch);
    const contentUpload = source.indexOf('attemptedCount = await runConcurrent(', sessionPreparation + 1);
    assert.ok(queueBatch >= 0, 'upload queue assigns batch IDs before transfer');
    assert.ok(sessionPreparation >= 0, 'graph batch upload prepares sessions');
    assert.ok(contentUpload > sessionPreparation, 'file transfer waits for session preparation');
});

test('validateFile accepts .doc only when the asynchronous import contract advertises it', () => {
    const r = validateFile(mockFile('old.doc', 1024), {
        allowed_extensions: [...ALLOWED_EXTENSIONS, '.doc'],
        max_file_bytes: MAX_FILE_SIZE,
    });
    assert.equal(r.valid, true);
});

test('validateFile rejects unknown extensions', () => {
    for (const name of ['a.exe', 'a.zip', 'noext']) {
        const r = validateFile(mockFile(name, 1024));
        assert.equal(r.valid, false, `${name} should be rejected`);
    }
});

test('validateFile rejects files over 500 MB', () => {
    const r = validateFile(mockFile('big.pdf', MAX_FILE_SIZE + 1));
    assert.equal(r.valid, false);
    assert.ok(r.reason.includes('500 MB'), 'reason mentions 500 MB limit');
});

test('validateFile accepts files at or under 500 MB', () => {
    assert.equal(validateFile(mockFile('max.pdf', MAX_FILE_SIZE)).valid, true);
    assert.equal(validateFile(mockFile('small.pdf', MAX_FILE_SIZE - 1)).valid, true);
});

test('validateFile returns failType for classification', () => {
    assert.equal(validateFile(mockFile('old.doc', 100)).failType, 'format');
    assert.equal(validateFile(mockFile('bad.exe', 100)).failType, 'format');
    assert.equal(validateFile(mockFile('big.pdf', MAX_FILE_SIZE + 1)).failType, 'size');
    assert.equal(validateFile(mockFile('ok.pdf', 100)).failType, undefined);
});

// ── validateBatch: new signature { accepted, duplicates, invalid } ──
test('validateBatch returns three arrays', () => {
    const r = validateBatch([mockFile('a.pdf', 100)], []);
    assert.ok(Array.isArray(r.accepted));
    assert.ok(Array.isArray(r.duplicates));
    assert.ok(Array.isArray(r.invalid));
    assert.equal(r.accepted.length, 1);
    assert.equal(r.duplicates.length, 0);
    assert.equal(r.invalid.length, 0);
});

test('validateBatch puts duplicates in separate array (not invalid)', () => {
    const f = mockFile('a.pdf', 100);
    const key = fileKey(f);
    const { accepted, duplicates, invalid } = validateBatch([f], [key]);
    assert.equal(accepted.length, 0);
    assert.equal(duplicates.length, 1);
    assert.equal(invalid.length, 0);
});

test('validateBatch puts format/size errors in invalid array', () => {
    const files = [mockFile('bad.exe', 100), mockFile('big.pdf', MAX_FILE_SIZE + 1)];
    const { accepted, duplicates, invalid } = validateBatch(files, []);
    assert.equal(accepted.length, 0);
    assert.equal(duplicates.length, 0);
    assert.equal(invalid.length, 2);
});

test('validateBatch enforces MAX_BATCH_SIZE without silent drop', () => {
    const existing = [];
    const files = Array.from({ length: MAX_BATCH_SIZE + 5 }, (_, i) => mockFile(`doc${i}.pdf`, 100, i));
    const { accepted, duplicates, invalid } = validateBatch(files, existing);
    assert.equal(accepted.length, MAX_BATCH_SIZE);
    assert.equal(duplicates.length, 0);
    assert.equal(invalid.length, 5);
    // Over-limit files are in invalid, not silently dropped
    assert.ok(invalid.some((r) => r.reason.includes(String(MAX_BATCH_SIZE))));
});

test('validateBatch mixes accepted, duplicates, and invalid in one batch', () => {
    const f1 = mockFile('a.pdf', 100, 1);
    const f2 = mockFile('bad.exe', 100, 2);
    const f3 = mockFile('a.pdf', 100, 1); // same as f1
    const f4 = mockFile('ok.txt', 200, 4);
    const { accepted, duplicates, invalid } = validateBatch([f1, f2, f3, f4], []);
    assert.equal(accepted.length, 2, 'f1 and f4 accepted');
    assert.equal(duplicates.length, 1, 'f3 is duplicate of f1');
    assert.equal(invalid.length, 1, 'f2 is invalid format');
});

// ── fileKey ──
test('fileKey produces stable dedup key', () => {
    const a = mockFile('a.pdf', 100, 999);
    const b = mockFile('a.pdf', 100, 999);
    assert.equal(fileKey(a), fileKey(b));
});

test('fileKey differs for different files', () => {
    assert.notEqual(fileKey(mockFile('a.pdf', 100, 1)), fileKey(mockFile('b.pdf', 100, 1)));
    assert.notEqual(fileKey(mockFile('a.pdf', 100, 1)), fileKey(mockFile('a.pdf', 200, 1)));
});

// ── formatSize ──
test('formatSize handles boundaries correctly', () => {
    assert.equal(formatSize(0), '0 B');
    assert.equal(formatSize(1024), '1 KB');
    assert.equal(formatSize(1536), '1.5 KB');
    assert.equal(formatSize(1048576), '1 MB');
    assert.equal(formatSize(MAX_FILE_SIZE), '500 MB');
});

// ── fileTypeIcon delegates to documents_ui.js ──
test('fileTypeIcon returns correct asset path for known types', () => {
    assert.equal(fileTypeIcon(mockFile('a.pdf', 1)), './assets/file-types/pdf.svg');
    assert.equal(fileTypeIcon(mockFile('a.DOCX', 1)), './assets/file-types/docx.svg');
    assert.equal(fileTypeIcon(mockFile('a.xlsx', 1)), './assets/file-types/xlsx.svg');
    assert.equal(fileTypeIcon(mockFile('a.md', 1)), './assets/file-types/md.svg');
    assert.equal(fileTypeIcon(mockFile('a.txt', 1)), './assets/file-types/txt.svg');
    assert.equal(fileTypeIcon(mockFile('a.json', 1)), './assets/file-types/json.svg');
    assert.equal(fileTypeIcon(mockFile('a.csv', 1)), './assets/file-types/csv.svg');
});

test('fileTypeIcon returns null for unknown extensions', () => {
    assert.equal(fileTypeIcon(mockFile('a.exe', 1)), null);
    assert.equal(fileTypeIcon({ name: '' }), null);
});

// ── Constants ──
test('constants have expected values', () => {
    assert.equal(MAX_FILE_SIZE, 500 * 1024 * 1024);
    assert.equal(MAX_BATCH_SIZE, 1000);
    assert.equal(ALLOWED_EXTENSIONS.has('.pdf'), true);
    assert.equal(ALLOWED_EXTENSIONS.has('.doc'), false);
});


// ── batch_replace helpers ──
const docsForReplace = [
    { id: 'd1', title: 'Alpha.pdf' },
    { id: 'd2', title: '  beta （ final ）.DOCX ' },
    { id: 'd3', title: 'Delta.PDF' },
    { id: 'd4', title: ' delta.pdf ' },
];

test('batch replace filename exact match wins', () => {
    const match = matchDocumentsForFile(mockFile('Alpha.pdf', 100), docsForReplace);
    assert.equal(match.matchStatus, 'matched');
    assert.equal(match.matchType, 'exact');
    assert.equal(match.matchedDocId, 'd1');
});

test('batch replace filename normalized match handles trim case spaces and full-width parens', () => {
    assert.equal(normalizeDocumentName('  beta （ final ）.DOCX '), 'beta ( final ).docx');
    const match = matchDocumentsForFile(mockFile('Beta ( final ).docx', 100), docsForReplace);
    assert.equal(match.matchStatus, 'matched');
    assert.equal(match.matchType, 'normalized');
    assert.equal(match.matchedDocId, 'd2');
});

test('batch replace marks unmatched files', () => {
    const match = matchDocumentsForFile(mockFile('missing.pdf', 100), docsForReplace);
    assert.equal(match.matchStatus, 'unmatched');
    assert.equal(match.matchedDocId, null);
});

test('batch replace marks multi-candidate normalized matches', () => {
    const match = matchDocumentsForFile(mockFile('delta.pdf', 100), docsForReplace);
    assert.equal(match.matchStatus, 'multiple');
    assert.equal(match.candidates.length, 2);
    assert.equal(match.matchedDocId, null);
});

test('batch replace submits only matched and valid items', () => {
    const items = createBatchReplaceItems([
        mockFile('Alpha.pdf', 100, 1),
        mockFile('missing.pdf', 100, 2),
        mockFile('bad.exe', 100, 3),
        mockFile('delta.pdf', 100, 4),
    ], docsForReplace);
    assert.equal(submittableBatchReplaceItems(items).length, 1);
    setBatchReplaceTarget(items[3], 'd3', docsForReplace);
    assert.equal(submittableBatchReplaceItems(items).length, 2);
});

test('batch replace failure of one item does not stop later items', async () => {
    const items = createBatchReplaceItems([
        mockFile('Alpha.pdf', 100, 1),
        mockFile('Beta ( final ).docx', 100, 2),
    ], docsForReplace);
    const calls = [];
    const result = await submitBatchReplaceItems(items, 'lib', async (slug, file, options) => {
        calls.push({ slug, file, options });
        if (file.name === 'Alpha.pdf') throw new Error('boom');
        return { documents: [{ operation: 'updated' }] };
    });
    assert.equal(calls.length, 2);
    assert.equal(items[0].status, 'failed');
    assert.equal(items[1].status, 'submitted');
    assert.equal(result.failed, 1);
    assert.equal(result.submitted, 1);
});

test('batch replace never calls add upload path', async () => {
    const items = createBatchReplaceItems([mockFile('Alpha.pdf', 100, 1)], docsForReplace);
    await submitBatchReplaceItems(items, 'lib', async (_slug, _file, options) => {
        assert.deepEqual(options, { replaceDocumentId: 'd1' });
    });
});


test('Import.js uses resolveImportEntry return value', () => {
    // Should destructure the return value and assign to slug.value / mode.value
    assert.ok(source.includes('resolveImportEntry(route.query'), 'calls resolveImportEntry');
    assert.ok(source.includes('slug.value = entry.slug') || (source.includes('.slug') && source.includes('resolveImportEntry')), 'assigns slug from return');
    assert.ok(source.includes('mode.value = entry.mode') || (source.includes('.mode') && source.includes('resolveImportEntry')), 'assigns mode from return');
});

test('Import.js reads resp.documents not resp.operations', () => {
    assert.ok(source.includes('resp.documents') || source.includes('.documents'), 'reads documents field');
    assert.ok(!source.includes('resp.operations'), 'no longer reads operations');
    assert.ok(!source.includes('importResult.operations'), 'no longer reads importResult.operations');
});

test('replace mode validates file before upload', () => {
    assert.ok(source.includes('onReplaceFileChange'), 'replace file change handler');
    assert.ok(source.includes('onReplaceDrop'), 'replace drop handler');
    assert.ok(source.includes('replaceFile'), 'replaceFile ref');
    assert.ok(source.includes('canReplace'), 'canReplace computed for button disable');
});

test('no inline style attributes in template', () => {
    const tpl = source.slice(source.indexOf('template:'));
    const styles = tpl.match(/style="/g) || [];
    assert.equal(styles.length, 0, 'zero inline style="..." attributes');
});

test('template uses import-* CSS classes', () => {
    for (const c of ['import-workspace', 'import-config-card', 'import-body',
                     'import-upload-card', 'import-file-card', 'import-dropzone',
                     'import-summary-bar']) {
        assert.ok(source.includes(c), `uses ${c}`);
    }
});

test('template includes drag-and-drop bindings', () => {
    assert.ok(source.includes('@dragover'), 'dragover event');
    assert.ok(source.includes('@drop'), 'drop event');
});

test('chunked batch validation preserves duplicate and selection-limit semantics', () => {
    const state = createBatchValidationState([], { max_files_per_selection: 2 });
    const first = validateBatchChunk([
        mockFile('a.pdf', 100, 1),
        mockFile('bad.exe', 100, 2),
    ], state);
    const second = validateBatchChunk([
        mockFile('a.pdf', 100, 1),
        mockFile('b.pdf', 100, 3),
    ], state);
    assert.equal(first.accepted.length, 1);
    assert.equal(first.invalid.length, 1);
    assert.equal(second.duplicates.length, 1);
    assert.equal(second.accepted.length, 1);
    assert.equal(second.invalid.length, 0);
});

test('library admins can select daily, initial-import, or custom upload limits', () => {
    for (const token of [
        '上传限制设置',
        '首次导入',
        '日常使用',
        '自定义',
        'updateImportConfiguration',
        'canManageLibrary',
        'queuePageSize',
        ':data="displayQueue"',
    ]) assert.ok(source.includes(token) || apiSource.includes(token), `missing upload setting token: ${token}`);
    assert.equal(source.includes('IMPORT_PROFILE_INITIAL'), true);
    assert.match(apiSource, /import-configuration`, jsonBody\('PUT'/);
});

test('template uses _failType not error text for check columns', () => {
    assert.ok(source.includes('格式校验'), 'format check column');
    assert.ok(source.includes('大小校验'), 'size check column');
    assert.ok(source.includes("row._failType === 'format'"), 'checks _failType for format');
    assert.ok(source.includes("row._failType === 'size'"), 'checks _failType for size');
    assert.ok(!source.includes("row.error.includes('格式')"), 'no longer checks error text for format');
});

test('external_id toggle uses showExtId boolean not space hack', () => {
    assert.ok(source.includes('showExtId'), 'showExtId ref exists');
    assert.ok(source.includes('showExtId = !showExtId'), 'toggles showExtId');
    assert.ok(!source.includes("externalId = extIdSet ? '' : ' '"), 'no space hack');
});

test('template references libraryStats and calls loadStats after upload', () => {
    assert.ok(source.includes('libraryStats'), 'libraryStats called');
    assert.ok(source.includes('loadStats()'), 'loadStats called after upload');
});

test('CSS defines import-* classes', () => {
    for (const c of ['import-workspace', 'import-config-card', 'import-body',
                     'import-upload-card', 'import-dropzone', 'import-file-card',
                     'import-summary-bar', 'import-check-ok', 'import-check-fail']) {
        const escaped = c.replace(/-/g, '\\-');
        assert.match(css, new RegExp('\\.' + escaped + '\\s*\\{'), `${c} defined`);
    }
});

test('CSS includes import responsive rules', () => {
    assert.match(css, /1199px[\s\S]*import-upload-card/, 'import at 1199px');
    assert.match(css, /899px[\s\S]*import-body/, 'import at 899px');
});

test('CSS defines import-dropzone with dashed border and drag-over highlight', () => {
    assert.match(css, /\.import-dropzone\s*\{[\s\S]*?border:\s*2px\s+dashed/, 'dashed border');
    assert.match(css, /\.import-dropzone\.is-dragover\s*\{/, 'is-dragover state');
});

test('replace route query auto-selects target document and handles not-found state', () => {
    for (const token of [
        'routeReplaceDocumentId',
        'routeReplaceTitle',
        'applyRouteReplaceTarget()',
        'replaceDocId.value = target.id',
        '目标文档不存在或已被删除，请返回文档管理重新选择',
        '正在加载目标文档...',
        '正在替换《',
        '返回文档管理',
    ]) assert.ok(source.includes(token), `missing replace deep-link token: ${token}`);
});

test('replace submission preserves target document id and never falls back to add', () => {
    assert.match(source, /api\.importFile\(slug\.value, file, \{[\s\S]*?replaceDocumentId: replaceDocId\.value,[\s\S]*?\}\)/, 'submits selected replace target');
    assert.ok(source.includes('!routeReplaceError.value'), 'canReplace blocks not-found target');
    assert.ok(source.includes('if (!routeReplaceActive.value) replaceDocId.value = null'), 'route-driven target is not cleared after replace');
    assert.equal(source.includes('api.importFile(slug.value, file, {})'), false, 'replace mode does not upload without target');
});

test('upload area confirms graph extraction configuration before submitting', () => {
    for (const token of [
        '抽取实体和关系',
        'graphExtractionRequested',
        'getUploadGraphExtractionConfiguration',
        'graphExtractionSecurityLevel',
        'graphExtractionReady',
        'graphExplorationMode',
        'formalGraphExtractionRequested',
        'graphJobRequested',
        '确认上传并抽取图谱',
        'graphJobRequested.value',
        'securityLevel: graphExtractionSecurityLevel.value',
    ]) assert.ok(source.includes(token), `missing graph upload token: ${token}`);
    assert.match(css, /\.import-graph-option\s*\{/);
    assert.match(apiSource, /formData\.append\('graph_extraction_requested', 'true'\)/);
    assert.match(apiSource, /formData\.append\('security_level', securityLevel\)/);
    assert.match(apiSource, /v04\/graph-extractions\/upload-configuration/);
});

test('upload inherits graph extraction default from the selected library', () => {
    assert.match(source, /typeof config\.default_requested !== 'boolean'/);
    assert.match(source, /typeof config\.exploration_available !== 'boolean'/);
    assert.match(source, /typeof config\.requires_active_schema !== 'boolean'/);
    assert.match(source, /!\['disabled', 'explore', 'governed'\]\.includes\(config\.schema_mode\)/);
    assert.match(source, /graphExtractionRequested\.value = config\.default_requested/);
    assert.match(source, /loadGraphExtractionConfiguration\(\{ applyLibraryDefault: true \}\)/);
    assert.match(source, /watch\(graphExtractionRequested,\s*\(requested\)/);
    assert.match(source, /:disabled="graphExtractionConfigLoading"/);
});

test('AI self-extraction sends graph jobs without requiring Schema activation', () => {
    assert.match(source, /graphExplorationMode = computed\(\(\) =>\s*graphExtractionConfig\.value\?\.schema_mode === 'explore'\s*\)/);
    assert.match(source, /graphExtractionConfig\.value\?\.exploration_available === true/);
    assert.match(source, /graphExtractionConfig\.value\?\.available === true/);
    assert.match(source, /return graphJobRequested\.value \?/);
    assert.match(source, /AI 自主抽取/);
});

test('uploaded files track vectorization and graph extraction progress', () => {
    for (const token of [
        'attachGraphTracking',
        'listDocumentJobs',
        'listGraphExtractions',
        'pollGraphProgress',
        '等待图谱抽取任务',
        '知识图谱构建',
        'graphProgressDetail',
        'stopGraphProgressPolling();',
    ]) assert.ok(source.includes(token) || apiSource.includes(token), `missing graph progress token: ${token}`);
    assert.match(css, /\.import-graph-progress\s*\{/);
    assert.match(apiSource, /v04\/graph-extractions\/\?/);
});

test('failed graph construction retries the graph job instead of the completed import', () => {
    assert.match(source, /it\.importJob\.retry_target_type === 'graph'/);
    assert.match(source, /it\.importJob\.retry_target_id/);
    assert.match(source, /api\.retryGraphExtraction\(/);
    assert.match(apiSource, /graph-extractions\/\$\{jobId\}\/retry/);
    assert.match(source, /error\?\.body\?\.detail !== 'no_retryable_units'/);
    assert.match(source, /api\.rerunGraphExtraction\(/);
    assert.match(source, /retry_target_type !== 'import'/);
    assert.match(source, /api\.retryImportJob\(/);
});

test('batch replacement forwards graph extraction upload options', async () => {
    const item = {
        _key: 'doc', file: mockFile('doc.txt', 12), validationStatus: 'valid',
        matchStatus: 'matched', matchedDocId: 'document-id', status: 'pending', error: '',
    };
    const calls = [];
    await submitBatchReplaceItems([item], 'public', async (...args) => calls.push(args), undefined, {
        graphExtractionRequested: true,
        securityLevel: 'internal',
    });
    assert.deepEqual(calls[0][2], {
        graphExtractionRequested: true,
        securityLevel: 'internal',
        replaceDocumentId: 'document-id',
    });
    assert.ok(item.importResponse !== undefined, 'batch replacement keeps upload response for graph tracking');
});

test('replace route query locks library and mode controls', () => {
    assert.match(source, /class="import-lib-select" :disabled="routeReplaceActive \|\| batchReplacing \|\| uploading \|\| addingFiles"/, 'route replace and active upload lock library select');
    assert.match(source, /<el-radio-group v-model="mode" :disabled="routeReplaceActive \|\| batchReplacing \|\| uploading \|\| addingFiles">/, 'route replace and active upload lock mode switch');
});

test('upload configuration displays schema policy and freezes the library build mode', () => {
    assert.match(source, /Object\.hasOwn\(BUILD_MODE_LABEL, config\.default_build_mode\)/);
    assert.match(source, /schemaModeLabel\(graphExtractionConfig\)/);
    assert.match(source, /抽取策略：\{\{ schemaModeLabel\(graphExtractionConfig\) \}\} · 构建模式：\{\{ buildModeLabel\(graphExtractionConfig\?\.default_build_mode\) \}\}/);
    assert.match(source, /使用\$\{buildModeLabel\(graphExtractionConfig\.value\?\.default_build_mode\)\}模式/);
});

test('upload confirmation describes automatic publication and later correction', () => {
    assert.match(source, /自动抽取并发布合格事实/);
    assert.match(source, /使用中发现错误后，可在知识治理中修正并重新发布/);
    assert.doesNotMatch(source, /抽取结果仍需在知识治理中发布后/);
});

console.log('import redesign test passed');
