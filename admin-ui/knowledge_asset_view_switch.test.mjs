import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    APP_PATHS,
    domainTabTarget,
    sidebarItems,
} from './src/domain_navigation.js';

const switchSource = readFileSync(
    new URL('./src/components/KnowledgeAssetViewSwitch.js', import.meta.url),
    'utf8',
);
const documentsSource = readFileSync(
    new URL('./src/views/Documents.js', import.meta.url),
    'utf8',
);
const catalogSource = readFileSync(
    new URL('./src/views/KnowledgeCatalog.js', import.meta.url),
    'utf8',
);
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('knowledge asset pages share one permission-aware segmented switch', () => {
    for (const token of [
        'menuAccess(',
        "label: '文档'",
        "label: '知识目录'",
        'domainTabTarget',
        'aria-label="知识内容视图"',
    ]) {
        assert.ok(switchSource.includes(token), `missing switch token: ${token}`);
    }
    assert.match(documentsSource, /import KnowledgeAssetViewSwitch from/);
    assert.match(catalogSource, /import KnowledgeAssetViewSwitch from/);
    assert.match(documentsSource, /<knowledge-asset-view-switch :library="slug \|\| ''" \/>/);
    assert.match(catalogSource, /<knowledge-asset-view-switch :library="selectedSlug \|\| ''" \/>/);
    assert.match(css, /\.knowledge-asset-view-switch\s*\{/);
});

test('merged sidebar entry retains both guarded routes and library context', () => {
    const items = sidebarItems('knowledgeAssets', {
        documents: true,
        catalog: true,
        import: true,
    });
    assert.deepEqual(items.map((item) => item.label), ['知识内容', '导入与替换']);
    assert.deepEqual(items[0].activePaths, [APP_PATHS.documents, APP_PATHS.catalog]);
    assert.deepEqual(domainTabTarget(APP_PATHS.documents, { library: 'legal' }), {
        path: APP_PATHS.documents,
        query: { slug: 'legal' },
    });
    assert.deepEqual(domainTabTarget(APP_PATHS.catalog, { slug: 'legal' }), {
        path: APP_PATHS.catalog,
        query: { library: 'legal' },
    });
});

test('existing document and catalog paths remain stable', () => {
    assert.equal(APP_PATHS.documents, '/knowledge-assets/documents');
    assert.equal(APP_PATHS.catalog, '/knowledge-assets/catalog');
});
