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
    retreatCatalogCursor,
    safeCatalogAccessUrl,
} from './src/catalog_ui.js';
import { listCatalogDocuments } from './src/api.js';

const view = readFileSync(new URL('./src/views/KnowledgeCatalog.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('./src/menu_access.js', import.meta.url), 'utf8');
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

test('wires a read-gated Catalog route and sidebar entry', () => {
    assert.match(app, /path:\s*'catalog'/);
    assert.match(app, /KnowledgeCatalog/);
    assert.match(app, /perm:\s*'read'/);
    assert.match(app, /effectivePerm:\s*'read'/);
    assert.match(menu, /catalog:\s*acts\.has\('read'\)/);
    assert.match(app, /canAccessEffectiveRoute/);
    assert.match(layout, /v-if="access\.catalog"/);
    assert.match(layout, /index="\/catalog"/);
    assert.match(layout, /知识目录/);
});

test('uses only the four strict Catalog APIs', () => {
    for (const token of [
        'listCatalogDocuments',
        'getCatalogDocument',
        'getCatalogEvidence',
        'getCatalogFileAccess',
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
    ]) assert.ok(view.includes(token), `missing view contract ${token}`);
    assert.ok(!view.includes('v-html'), 'Evidence and source content must use escaped Vue text');
    assert.ok(!view.includes('api.listLibraries'), 'member Catalog must not use admin Library list');
    assert.ok(!view.includes('console.'), 'Catalog must not log content or signed URLs');
    assert.doesNotMatch(view, /style="[^"]*"/, 'Catalog template must not use inline styles');
});

test('catalog styles are compact, responsive, and bounded', () => {
    for (const token of [
        '.catalog-workspace',
        '.catalog-filter-band',
        '.catalog-capability-grid',
        '.catalog-graph-grid',
        '.catalog-evidence-drawer',
        '@media screen and (max-width: 1199px)',
        '@media screen and (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing CSS token ${token}`);
    assert.match(css, /\.catalog-graph-card\s*\{[^}]*border-radius:\s*8px/s);
    assert.match(css, /\.catalog-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
});
