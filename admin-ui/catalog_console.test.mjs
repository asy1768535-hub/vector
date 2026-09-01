import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    advanceCatalogCursor,
    capabilityStateLabel,
    catalogEvidenceParts,
    catalogOverallLabel,
    catalogOverallTag,
    collectCatalogLabels,
    formatCatalogConfidence,
    processingErrorKind,
    processingErrorLabel,
    processingResponseMatches,
    processingStageLabel,
    processingStageRetryable,
    processingStatusLabel,
    processingStatusTag,
    retreatCatalogCursor,
    safeCatalogAccessUrl,
} from './src/catalog_ui.js';
import {
    getCatalogDocumentProcessing,
    getDocumentFullSource,
    listCatalogDocuments,
    retryCatalogDocumentProcessing,
    setDocumentClassification,
} from './src/api.js';
import { canManageLibrary } from './src/menu_access.js';

const view = readFileSync(new URL('./src/views/KnowledgeCatalog.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('./src/menu_access.js', import.meta.url), 'utf8');
const navigation = readFileSync(new URL('./src/domain_navigation.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('maps coarse and capability states without treating unknown values as ready', () => {
    assert.equal(catalogOverallLabel('usable'), '可用');
    assert.equal(catalogOverallLabel('partial'), '部分可用');
    assert.equal(catalogOverallLabel('unexpected'), '未知');
    assert.equal(catalogOverallTag('usable'), 'success');
    assert.equal(catalogOverallTag('unexpected'), 'info');
    assert.equal(capabilityStateLabel('pending_review'), '待审核');
    assert.equal(capabilityStateLabel('disabled'), '未启用');
});

test('collects effective labels by UUID rather than display text', () => {
    const labels = collectCatalogLabels([
        {
            classification: {
                labels: [
                    { id: '2', key: 'contract', label: '合同', role: 'primary', ordinal: 0 },
                    { id: '3', key: 'risk', label: '风险', role: 'secondary', ordinal: 1 },
                ],
            },
        },
        {
            classification: {
                labels: [
                    { id: '2', key: 'contract', label: '合同新版', role: 'primary', ordinal: 0 },
                    { id: '4', key: 'other-contract', label: '合同', role: 'secondary', ordinal: 1 },
                ],
            },
        },
    ]);
    assert.deepEqual(labels.map((item) => item.id), ['2', '3', '4']);
    assert.equal(labels[0].label, '合同');
    assert.equal(labels[2].label, '合同');
});

test('formats confidence defensively', () => {
    assert.equal(formatCatalogConfidence(0.9234), '92.3%');
    assert.equal(formatCatalogConfidence(1), '100%');
    assert.equal(formatCatalogConfidence(null), '—');
    assert.equal(formatCatalogConfidence(Number.NaN), '—');
});

test('maps bounded processing stages, statuses, failures, and retry eligibility', () => {
    assert.equal(processingStageLabel('summary'), '摘要');
    assert.equal(processingStageLabel('graph'), '图谱抽取');
    assert.equal(processingStageLabel('other'), '未知阶段');
    assert.equal(processingStatusLabel('partially_succeeded'), '部分完成');
    assert.equal(processingStatusLabel('other'), '未知状态');
    assert.equal(processingStatusTag('failed'), 'danger');
    assert.equal(processingStatusTag('other'), 'info');
    assert.equal(processingErrorLabel('provider_timeout'), '模型服务响应超时');
    assert.equal(processingErrorLabel('private_provider_trace'), '该阶段处理失败');
    assert.equal(processingErrorKind({ status: 409 }), 'conflict');
    assert.equal(processingErrorKind({ status: 403 }), 'forbidden');
    assert.equal(processingStageRetryable({
        stage: 'summary',
        availability: 'enabled',
        retryable: true,
        job_id: 'job-1',
        retry_generation: 2,
    }), true);
    assert.equal(processingStageRetryable({
        stage: 'summary',
        availability: 'disabled',
        retryable: true,
        job_id: 'job-1',
        retry_generation: 2,
    }), false);
    assert.equal(processingStageRetryable({
        stage: 'unknown',
        availability: 'enabled',
        retryable: true,
        job_id: 'job-1',
        retry_generation: 2,
    }), false);
});

test('processing response identity and management scope fail closed', () => {
    const identity = { libraryId: 'lib-1', documentId: 'doc-1', revisionId: 'rev-1' };
    assert.equal(processingResponseMatches({
        library_id: 'lib-1',
        document_id: 'doc-1',
        document_revision_id: 'rev-1',
    }, identity), true);
    assert.equal(processingResponseMatches({
        library_id: 'lib-1',
        document_id: 'doc-1',
        document_revision_id: 'old-revision',
    }, identity), false);

    const permissions = [
        { library_slug: 'managed', organization_id: 'org-1', actions: ['read', 'admin'] },
        { library_slug: 'implicit', organization_id: 'org-2', actions: ['read'] },
        { library_slug: 'reader', organization_id: 'org-3', actions: ['read'] },
    ];
    const organizations = [
        { organization_id: 'org-2', role: 'organization_admin' },
        { organization_id: 'org-3', role: 'member' },
    ];
    assert.equal(canManageLibrary(permissions, organizations, 'managed'), true);
    assert.equal(canManageLibrary(permissions, organizations, 'implicit'), true);
    assert.equal(canManageLibrary(permissions, organizations, 'reader'), false);
    assert.equal(canManageLibrary(permissions, [
        { organization_id: 'org-other', role: 'organization_admin' },
    ], 'implicit'), false);
});

test('translates absolute evidence offsets into bounded window parts', () => {
    const parts = catalogEvidenceParts({
        text_window: '0123456789',
        window_start: 100,
        source_start: 103,
        source_end: 107,
    });
    assert.deepEqual(parts, [
        { text: '012', highlight: false },
        { text: '3456', highlight: true },
        { text: '789', highlight: false },
    ]);
    assert.deepEqual(catalogEvidenceParts({ text_window: 'abc', window_start: 20 }), [
        { text: 'abc', highlight: false },
    ]);
});

test('cursor navigation preserves opaque cursors and supports previous page', () => {
    const page2 = advanceCatalogCursor({ history: [], current: null }, 'opaque-2');
    assert.deepEqual(page2, { history: [null], current: 'opaque-2' });
    const page3 = advanceCatalogCursor(page2, 'opaque-3');
    assert.deepEqual(page3, { history: [null, 'opaque-2'], current: 'opaque-3' });
    assert.deepEqual(retreatCatalogCursor(page3), {
        history: [null],
        current: 'opaque-2',
    });
});

test('accepts only safe proxy or HTTP signed file URLs', () => {
    assert.equal(
        safeCatalogAccessUrl({ access_mode: 'proxy', url: '/libraries/a/documents/1/file' }),
        '/libraries/a/documents/1/file',
    );
    assert.equal(safeCatalogAccessUrl({ access_mode: 'proxy', url: '//evil.invalid/x' }), null);
    assert.equal(
        safeCatalogAccessUrl(
            { access_mode: 'signed_url', url: 'https://objects.invalid/signed' },
            'https://app.invalid',
        ),
        'https://objects.invalid/signed',
    );
    assert.equal(
        safeCatalogAccessUrl({ access_mode: 'signed_url', url: 'javascript:alert(1)' }),
        null,
    );
    assert.equal(
        safeCatalogAccessUrl({ access_mode: 'signed_url', url: '/not-a-signed-url' }),
        null,
    );
});

test('catalog list API serializes only the strict query allowlist', async () => {
    const originalFetch = globalThis.fetch;
    let requestedPath = '';
    globalThis.fetch = async (path) => {
        requestedPath = String(path);
        return new Response(JSON.stringify({ items: [], total: 0, next_cursor: null }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await listCatalogDocuments('allowed-library', {
            title: 'contract',
            status: 'ready',
            classification_state: 'classified',
            label_id: 'label-1',
            limit: 20,
            cursor: 'opaque-cursor',
            object_key: 'must-not-leak',
        }, true);
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.equal(
        requestedPath,
        '/libraries/allowed-library/catalog/documents?title=contract&status=ready&classification_state=classified&label_id=label-1&limit=20&cursor=opaque-cursor',
    );
});

test('processing API uses the exact current-document routes and fenced retry body', async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (path, options = {}) => {
        requests.push({ path: String(path), options });
        return new Response(JSON.stringify({
            contract_version: 'document-processing-v1',
            library_id: 'lib-1',
            document_id: 'doc-1',
            document_revision_id: 'rev-1',
            stages: [],
        }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await getCatalogDocumentProcessing('contracts', 'doc-1');
        await retryCatalogDocumentProcessing('contracts', 'doc-1', 'summary', {
            source_job_id: 'job-1',
            retry_generation: 3,
        });
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.equal(
        requests[0].path,
        '/libraries/contracts/catalog/documents/doc-1/processing',
    );
    assert.equal(
        requests[1].path,
        '/libraries/contracts/catalog/documents/doc-1/processing/summary/retry',
    );
    assert.equal(requests[1].options.method, 'POST');
    assert.deepEqual(JSON.parse(requests[1].options.body), {
        source_job_id: 'job-1',
        retry_generation: 3,
    });
});

test('classification editing uses the document endpoint and effective decision fence', async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (path, options = {}) => {
        requests.push({ path: String(path), options });
        return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    const body = {
        expected_effective_decision_set_id: 'decision-set-1',
        primary_label_id: 'label-a',
        secondary_label_ids: ['label-b'],
    };
    try {
        await setDocumentClassification('contracts', 'doc-1', body);
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.deepEqual(requests.map((item) => item.path), [
        '/libraries/contracts/classifications/documents/doc-1',
    ]);
    assert.equal(requests[0].options.method, 'PUT');
    assert.deepEqual(JSON.parse(requests[0].options.body), body);
});
test('wires a read-gated Catalog route inside the knowledge asset domain', () => {
    assert.match(app, /path:\s*'catalog'/);
    assert.match(app, /KnowledgeCatalog/);
    assert.match(app, /perm:\s*'read'/);
    assert.match(app, /effectivePerm:\s*'read'/);
    assert.match(menu, /catalog:\s*acts\.has\('read'\)/);
    assert.match(app, /canAccessEffectiveRoute/);
    assert.match(layout, /visibleSidebarGroups/);
    assert.match(navigation, /knowledgeAssets/);
    assert.match(navigation, /label:\s*'知识资产'/);
    assert.match(navigation, /path:\s*APP_PATHS\.catalog/);
    assert.match(navigation, /access:\s*'catalog'/);
});

test('uses the strict Catalog read and processing APIs', () => {
    for (const token of [
        'listCatalogDocuments',
        'getCatalogDocument',
        'getCatalogEvidence',
        'getCatalogFileAccess',
        'getCatalogDocumentProcessing',
        'retryCatalogDocumentProcessing',
    ]) assert.ok(api.includes(`export const ${token}`), `missing ${token}`);
    for (const path of [
        '/catalog/documents',
        '/catalog/evidence/',
        '/catalog/files/',
        '/access',
    ]) assert.ok(api.includes(path), `missing Catalog path ${path}`);
});

test('view keeps list, deep-linked detail, evidence, and file access in one read flow', () => {
    for (const token of [
        'readableLibraries',
        'route.query.document',
        'api.listCatalogDocuments',
        'api.getCatalogDocument',
        'api.getCatalogEvidence',
        'api.getCatalogFileAccess',
        'requestSeq',
        'detailRequestSeq',
        'evidenceRequestSeq',
        'fileRequestSeq',
        'isCurrentFile',
        'noopener noreferrer',
        'entities_truncated',
        'relations_truncated',
        'canManageProcessing',
        'processingRequestSeq',
        'processingMutationSeq',
        'api.getCatalogDocumentProcessing',
        'api.retryCatalogDocumentProcessing',
        'processingResponseMatches',
        'classificationEditor',
        'api.setDocumentClassification',
        'expected_effective_decision_set_id',
        'source_job_id: stage.job_id',
        'retry_generation: stage.retry_generation',
    ]) assert.ok(view.includes(token), `missing view contract ${token}`);
    assert.ok(!view.includes('v-html'), 'Evidence and source content must use escaped Vue text');
    assert.ok(!view.includes('api.listLibraries'), 'member Catalog must not use admin Library list');
    assert.ok(!view.includes('console.'), 'Catalog must not log content or signed URLs');
    assert.doesNotMatch(view, /style="[^"]*"/, 'Catalog template must not use inline styles');
});

test('document detail shows content directly and only surfaces processing failures', () => {
    assert.ok(view.includes('processingIssues'));
    assert.ok(view.includes('<h3>处理异常</h3>'));
    assert.ok(view.includes('v-for="stage in processingIssues"'));
    assert.ok(!view.includes('<h3>知识能力</h3>'));
    assert.ok(!view.includes('<h3>文档处理</h3>'));
    assert.ok(!view.includes('catalog-capability-grid'));
});

test('full source API sends bounded window parameters', async () => {
    const originalFetch = globalThis.fetch;
    let requestedPath = '';
    globalThis.fetch = async (path) => {
        requestedPath = String(path);
        return new Response(JSON.stringify({ normalized_text: 'page' }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await getDocumentFullSource('contracts', 'doc-1', { offset: 100000, limit: 100000 });
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.equal(
        requestedPath,
        '/libraries/contracts/documents/doc-1/source/full?offset=100000&limit=100000',
    );
});

test('full source reader exposes bounded progress, explicit loading, and stale response fence', () => {
    assert.match(view, /sourceRequestSeq/);
    assert.match(view, /Array\.from\(String\(data\.normalized_text \|\| ''\)\)\.length/);
    assert.match(view, /已加载 \{\{ sourceReader\.nextOffset \}\}/);
    assert.match(view, /搜索已加载内容/);
    assert.match(view, /@click="loadMoreSource"/);
});

test('full source dialog keeps long documents inside a scrollable viewport', () => {
    assert.match(css, /\.documents-source-dialog\s*\{[^}]*width:\s*min\(860px,\s*calc\(100vw - 32px\)\)/s);
    assert.match(css, /\.documents-source-dialog \.el-dialog__body\s*\{[^}]*max-height:\s*calc\(90vh - 72px\)[^}]*overflow-y:\s*auto/s);
    assert.match(css, /\.documents-source-text\s*\{[^}]*overflow-wrap:\s*anywhere[^}]*white-space:\s*pre-wrap/s);
});

test('pending classifications are reviewed inside the catalog document detail', () => {
    for (const token of [
        'loadCurrentClassificationReview',
        'api.listClassificationReviews',
        'reviewPageMatches',
        'api.reviewClassificationRun',
        'expected_run_status: run.status',
        "submitClassificationReview('accept')",
        "submitClassificationReview('change')",
        "submitClassificationReview('reject')",
        '确认建议',
        '调整后确认',
    ]) assert.ok(view.includes(token), `missing inline review contract ${token}`);
    assert.match(view, /state === 'pending_review'[\s\S]*?\? '审核' : '详情'/);
    assert.ok(css.includes('.catalog-classification-review'));
});

test('catalog styles are compact, responsive, and bounded', () => {
    for (const token of [
        '.catalog-workspace',
        '.catalog-filter-band',
        '.catalog-graph-grid',
        '.catalog-processing-list',
        '.catalog-processing-row',
        '.catalog-evidence-drawer',
        '@media screen and (max-width: 1199px)',
        '@media screen and (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing CSS token ${token}`);
    assert.match(css, /\.catalog-graph-card\s*\{[^}]*border-radius:\s*8px/s);
    assert.match(css, /\.catalog-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
    assert.match(css, /\.catalog-processing-row\s*\{[^}]*grid-template-columns:\s*10px minmax\(0, 1fr\) auto/s);
});
