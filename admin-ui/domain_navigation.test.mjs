import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    APP_PATHS,
    DOMAIN_TABS,
    LEGACY_REDIRECTS,
    defaultRouteForAccess,
    domainTabTarget,
    domainTabs,
    firstDomainPath,
    legacyRedirectTarget,
    visibleSidebarDomains,
} from './src/domain_navigation.js';

const READER_ACCESS = {
    knowledgeUse: true,
    knowledgeAssets: true,
    knowledgeGovernance: true,
    chat: true,
    search: true,
    documents: true,
    catalog: true,
    knowledgeGraph: true,
    account: true,
    apiKeys: true,
};

test('domain tabs and sidebar follow fine-grained access', () => {
    assert.deepEqual(domainTabs('knowledgeUse', READER_ACCESS).map((item) => item.key), [
        'chat',
        'search',
    ]);
    assert.deepEqual(domainTabs('knowledgeAssets', READER_ACCESS).map((item) => item.key), [
        'documents',
        'catalog',
    ]);
    assert.deepEqual(domainTabs('knowledgeGovernance', READER_ACCESS).map((item) => item.key), [
        'knowledgeGraph',
    ]);
    assert.deepEqual(visibleSidebarDomains(READER_ACCESS).map((item) => item.key), [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
    ]);
});

test('default routes and domain roots choose the first accessible leaf', () => {
    assert.equal(defaultRouteForAccess(READER_ACCESS), APP_PATHS.chat);
    assert.equal(firstDomainPath('knowledgeAssets', {
        knowledgeAssets: true,
        import: true,
    }), APP_PATHS.importData);
    assert.equal(defaultRouteForAccess({
        operationsCenter: true,
        account: true,
    }), APP_PATHS.dashboard);
    assert.equal(defaultRouteForAccess({
        knowledgeUse: true,
        retrievalTest: true,
        account: true,
    }), APP_PATHS.retrievalTest);
    assert.equal(defaultRouteForAccess({ account: true, apiKeys: true }), APP_PATHS.profile);
});

test('knowledge asset tabs translate Library context without leaking it to other domains', () => {
    assert.deepEqual(domainTabTarget(APP_PATHS.documents, { library: 'legal' }), {
        path: APP_PATHS.documents,
        query: { slug: 'legal' },
    });
    assert.deepEqual(domainTabTarget(APP_PATHS.catalog, { slug: 'legal' }), {
        path: APP_PATHS.catalog,
        query: { library: 'legal' },
    });
    assert.deepEqual(domainTabTarget(APP_PATHS.importData, { library: 'legal' }), {
        path: APP_PATHS.importData,
        query: { library: 'legal' },
    });
    assert.deepEqual(domainTabTarget(APP_PATHS.chat, { library: 'legal' }), {
        path: APP_PATHS.chat,
    });
});

test('all 17 historical routes preserve query and hash', () => {
    assert.equal(Object.keys(LEGACY_REDIRECTS).length, 17);
    const query = { library: 'legal', page: '2' };
    for (const [legacyPath, canonicalPath] of Object.entries(LEGACY_REDIRECTS)) {
        assert.deepEqual(legacyRedirectTarget(legacyPath, query, '#detail'), {
            path: canonicalPath,
            query,
            hash: '#detail',
        });
    }
    assert.equal(legacyRedirectTarget('/not-a-route', query), null);
});

test('nine page domains and account contracts remain represented', () => {
    assert.deepEqual(Object.keys(DOMAIN_TABS), [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
        'usersPermissions',
        'operationsCenter',
        'auditCenter',
        'account',
    ]);
    assert.equal(APP_PATHS.login, '/login');
    assert.equal(APP_PATHS.libraries, '/libraries');
    assert.deepEqual(DOMAIN_TABS.account.map((item) => item.path), [
        APP_PATHS.profile,
        APP_PATHS.apiKeys,
    ]);

    const profile = readFileSync(new URL('./src/views/AccountProfile.js', import.meta.url), 'utf8');
    const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
    assert.match(profile, /api\.updateMe/);
    assert.match(profile, /api\.updateMe\(\{\s*password:/);
    assert.match(profile, /clearAuthState/);
    assert.match(layout, /command="account"/);
    assert.match(layout, /router\.push\('\/account\/profile'\)/);
});
