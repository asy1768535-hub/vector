import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';

import {
    APP_PATHS,
    domainTabTarget,
    sidebarItems,
} from './src/domain_navigation.js';

const catalogSource = readFileSync(
    new URL('./src/views/KnowledgeCatalog.js', import.meta.url),
    'utf8',
);
const documentsSource = readFileSync(
    new URL('./src/views/Documents.js', import.meta.url),
    'utf8',
);
const appSource = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('knowledge assets no longer render a document/catalog segmented switch', () => {
    assert.equal(
        existsSync(new URL('./src/components/KnowledgeAssetViewSwitch.js', import.meta.url)),
        false,
    );
    assert.doesNotMatch(catalogSource, /knowledge-asset-view-switch|KnowledgeAssetViewSwitch/);
    assert.doesNotMatch(documentsSource, /knowledge-asset-view-switch|KnowledgeAssetViewSwitch/);
    assert.doesNotMatch(css, /knowledge-asset-view-switch/);
});

test('single knowledge asset entry points to Catalog and old document path redirects', () => {
    const items = sidebarItems('knowledgeAssets', {
        documents: true,
        catalog: true,
        import: true,
    });
    assert.deepEqual(items.map((item) => item.label), ['知识资产', '导入与替换']);
    assert.equal(items[0].path, APP_PATHS.catalog);
    assert.deepEqual(items[0].activePaths, [APP_PATHS.catalog, APP_PATHS.documents]);
    assert.deepEqual(domainTabTarget(APP_PATHS.documents, { library: 'legal' }), {
        path: APP_PATHS.documents,
        query: { library: 'legal' },
    });
    assert.match(appSource, /path:\s*'documents'[\s\S]*?redirect:\s*\(to\) =>/);
    assert.match(appSource, /to\.query\.slug[\s\S]*?\{\s*library:\s*to\.query\.slug\s*\}/);
    assert.match(appSource, /to\.query\.open[\s\S]*?\{\s*document:\s*to\.query\.open\s*\}/);
});

test('catalog list owns document operations without a second document list request', () => {
    for (const token of [
        'api.listCatalogDocuments',
        'api.libraryStats',
        'openFileImport',
        'openIngest',
        'openEdit',
        'openReplaceImport',
        'api.ingestDocument',
        'api.updateDocument',
        'api.deleteDocument',
        'filterDraft.dateRange',
        'visibleItems',
        'handleRowCommand',
    ]) assert.ok(catalogSource.includes(token), `missing unified asset token ${token}`);
    assert.doesNotMatch(catalogSource, /api\.listDocuments/);
});
