import assert from 'node:assert/strict';
import test from 'node:test';

import { menuAccess } from './src/menu_access.js';
import {
    PREVIEW_ORGANIZATIONS,
    PREVIEW_PERMISSIONS,
    PREVIEW_USER,
    createPreviewFetch,
    isLocalPreviewUrl,
} from './src/preview_mode.js';

const PREVIEW_URL = 'http://127.0.0.1:5599/?preview=1#/login';

test('preview mode is restricted to loopback port 5599 with preview=1', () => {
    assert.equal(isLocalPreviewUrl(PREVIEW_URL), true);
    assert.equal(isLocalPreviewUrl('http://localhost:5599/?preview=1#/login'), true);
    assert.equal(isLocalPreviewUrl('http://127.0.0.1:5599/#/login'), false);
    assert.equal(isLocalPreviewUrl('http://127.0.0.1:8100/?preview=1#/login'), false);
    assert.equal(isLocalPreviewUrl('https://example.com:5599/?preview=1#/login'), false);
});

test('preview identity exposes all functional sidebar domains', () => {
    const access = menuAccess(
        PREVIEW_USER,
        PREVIEW_PERMISSIONS,
        PREVIEW_ORGANIZATIONS,
    );
    for (const domain of [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
        'usersPermissions',
        'libraries',
        'operationsCenter',
        'auditCenter',
    ]) {
        assert.equal(access[domain], true, domain);
    }
});

test('production URLs retain the original fetch implementation', async () => {
    const originalFetch = async () => new Response('production', { status: 418 });
    const productionFetch = createPreviewFetch(
        originalFetch,
        'https://example.com:5599/?preview=1',
    );
    assert.equal(productionFetch, originalFetch);
    assert.equal((await productionFetch('/users/me')).status, 418);
});

test('known preview API paths return compatible fixtures', async () => {
    const previewFetch = createPreviewFetch(
        async () => new Response('delegated', { status: 418 }),
        PREVIEW_URL,
    );

    const user = await (await previewFetch('/users/me')).json();
    assert.equal(user.id, PREVIEW_USER.id);

    const permissions = await (await previewFetch('/me/permissions')).json();
    assert.deepEqual(permissions[0].actions, ['read', 'insert', 'delete', 'admin']);

    const libraries = await (await previewFetch('/admin/libraries?limit=500')).json();
    assert.equal(libraries[0].slug, 'preview-library');

    const entities = await (await previewFetch(
        '/organizations/preview-org/graph-catalog/entities:search',
        { method: 'POST' },
    )).json();
    assert.equal(entities.contract_version, 'graph-catalog-entities-v1');
    assert.equal(entities.items.length, 3);
    assert.equal(entities.items[0].canonical_name, '向量数据库');
    assert.equal(entities.items[0].governance_state_hash.length, 64);
    assert.equal(entities.next_cursor, null);

    const graph = await (await previewFetch(
        '/libraries/preview-library/v06/graph/query',
        {
            method: 'POST',
            body: JSON.stringify({ seeds: [{ entity_id: entities.items[0].id }] }),
        },
    )).json();
    assert.equal(graph.contract_version, 'v1');
    assert.equal(graph.nodes.length, 3);
    assert.equal(graph.relations.length, 2);
    assert.equal(graph.seed_matches[0].entity_id, entities.items[0].id);

    const documents = await (await previewFetch(
        '/libraries/preview-library/documents?limit=500',
    )).json();
    assert.deepEqual(documents, []);
});

test('graph performance preview fixtures expose build mode and monitor metrics', async () => {
    const previewFetch = createPreviewFetch(
        async () => new Response('delegated', { status: 418 }),
        PREVIEW_URL,
    );

    const config = await (await previewFetch(
        '/libraries/preview-library/v04/graph-extractions/upload-configuration',
    )).json();
    assert.equal(config.default_build_mode, 'standard');

    const jobs = await (await previewFetch('/admin/jobs/monitor')).json();
    assert.equal(jobs[0].publication_status, 'publishing');
    assert.equal(jobs[0].progress.planned_batches, 5);
    assert.equal(jobs[0].metrics.effective_concurrency, 3);
});

test('unknown and cross-origin requests delegate to the original fetch', async () => {
    const delegated = [];
    const originalFetch = async (...args) => {
        delegated.push(args);
        return new Response('delegated', { status: 418 });
    };
    const previewFetch = createPreviewFetch(originalFetch, PREVIEW_URL);

    assert.equal((await previewFetch('/assets/app.js')).status, 418);
    assert.equal((await previewFetch('https://example.com/users/me')).status, 418);
    assert.equal(delegated.length, 2);
});
