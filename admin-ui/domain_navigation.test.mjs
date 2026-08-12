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
    retrievalModeTarget,
    sidebarItems,
    visibleSidebarGroups,
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

const SCHEMA_ADMIN_ACCESS = {
    libraries: true,
    libraryConfiguration: false,
    schemaLifecycle: true,
};

test('sidebar leaf pages follow fine-grained access', () => {
    assert.deepEqual(domainTabs('knowledgeUse', READER_ACCESS).map((item) => item.key), [
        'chat',
        'search',
    ]);
    assert.deepEqual(sidebarItems('knowledgeUse', {
        ...READER_ACCESS,
        retrievalTest: true,
    }).map((item) => item.label), [
        '智能问答',
        '检索诊断',
    ]);
    assert.deepEqual(domainTabs('knowledgeAssets', READER_ACCESS).map((item) => item.key), [
        'catalog',
    ]);
    assert.deepEqual(domainTabs('knowledgeGovernance', READER_ACCESS).map((item) => item.key), [
        'knowledgeGraph',
    ]);
    assert.deepEqual(visibleSidebarGroups(READER_ACCESS).map((item) => item.key), [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
        'account',
    ]);
    assert.deepEqual(
        visibleSidebarGroups(READER_ACCESS).flatMap((group) => group.items.map((item) => item.path)),
        [
            APP_PATHS.chat,
            APP_PATHS.search,
            APP_PATHS.catalog,
            APP_PATHS.knowledgeGraph,
            APP_PATHS.profile,
            APP_PATHS.apiKeys,
        ],
    );
});

test('knowledge assets use one content sidebar entry', () => {
    const items = sidebarItems('knowledgeAssets', READER_ACCESS);
    assert.deepEqual(items.map((item) => item.label), ['知识资产']);
    assert.equal(items[0].path, APP_PATHS.catalog);
    assert.deepEqual(items[0].activePaths, [APP_PATHS.catalog, APP_PATHS.documents]);

    const documentsOnly = sidebarItems('knowledgeAssets', {
        documents: true,
        catalog: false,
    });
    assert.equal(documentsOnly[0].path, APP_PATHS.catalog);
    assert.deepEqual(documentsOnly[0].activePaths, [APP_PATHS.catalog, APP_PATHS.documents]);
});

test('Schema management is shown in the administrator Library section', () => {
    assert.deepEqual(domainTabs('knowledgeGovernance', {
        knowledgeGraph: true,
        schemaLifecycle: true,
    }).map((item) => item.key), [
        'knowledgeGraph',
    ]);
    assert.deepEqual(domainTabs('libraries', SCHEMA_ADMIN_ACCESS).map((item) => item.key), [
        'schemaLifecycle',
    ]);
    const groups = visibleSidebarGroups(SCHEMA_ADMIN_ACCESS);
    assert.deepEqual(groups.map((item) => item.key), ['libraries']);
    assert.equal(groups[0].section, 'admin');
    assert.equal(groups[0].items[0].path, APP_PATHS.schemaLifecycle);

    const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
    assert.match(
        app,
        /path: 'schema',[\s\S]*?domain: 'libraries',[\s\S]*?domainTitle: '库管理'/,
    );
});

test('default routes and domain roots choose the first accessible leaf', () => {
    assert.equal(defaultRouteForAccess(READER_ACCESS), APP_PATHS.chat);
    assert.equal(defaultRouteForAccess({
        ...READER_ACCESS,
        operationsCenter: true,
    }), APP_PATHS.chat);
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
    assert.deepEqual(sidebarItems('knowledgeUse', {
        knowledgeUse: true,
        retrievalTest: true,
    }), [{
        key: 'searchDiagnostics',
        label: '检索诊断',
        path: APP_PATHS.retrievalTest,
        activePaths: [APP_PATHS.search, APP_PATHS.retrievalTest],
        access: 'searchDiagnostics',
        icon: 'sidebar:search',
    }]);
    assert.equal(defaultRouteForAccess({ account: true, apiKeys: true }), APP_PATHS.profile);
});

test('knowledge asset tabs translate Library context without leaking it to other domains', () => {
    assert.deepEqual(domainTabTarget(APP_PATHS.documents, { library: 'legal' }), {
        path: APP_PATHS.documents,
        query: { library: 'legal' },
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

test('retrieval modes preserve query text and applicable library scope', () => {
    assert.deepEqual(retrievalModeTarget(APP_PATHS.retrievalTest, {}, {
        queryText: '合同期限', librarySlugs: ['legal'],
    }), { path: APP_PATHS.retrievalTest, query: { q: '合同期限', libraries: 'legal' } });
    assert.deepEqual(retrievalModeTarget(APP_PATHS.search, { q: '合同期限', libraries: 'a,b' }), {
        path: APP_PATHS.search, query: { q: '合同期限', library: 'a' },
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
        'libraries',
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
    const workspace = readFileSync(new URL('./src/views/DomainWorkspace.js', import.meta.url), 'utf8');
    assert.match(profile, /api\.updateMe/);
    assert.match(profile, /api\.updateMe\(\{\s*password:/);
    assert.match(profile, /clearAuthState/);
    assert.match(layout, /command="account"/);
    assert.match(layout, /router\.push\('\/account\/profile'\)/);
    assert.match(layout, /visibleSidebarGroups/);
    assert.match(layout, /sidebarEntries/);
    assert.match(layout, /v-for="entry in sidebarEntries"/);
    assert.doesNotMatch(layout, /el-sub-menu/);
    assert.doesNotMatch(workspace, /el-tabs|domainTabs|domain-workspace-tabs/);
});
