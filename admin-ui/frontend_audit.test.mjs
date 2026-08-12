import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { menuAccess, canAccessRoute } from './src/menu_access.js';
import { csvEscape, downloadCSV } from './src/logs_ui.js';

// ══════════════════════════════════════════════════
//  Task 1: Route safety
// ══════════════════════════════════════════════════

test('menuAccess: superuser → all true', () => {
    const a = menuAccess({ is_superuser: true }, []);
    assert.equal(a.documents, true);
    assert.equal(a.chat, true);
    assert.equal(a.import, true);
    assert.equal(a.apiKeys, true);
});

test('menuAccess: read-only user → read pages true, import false', () => {
    const a = menuAccess({}, [{ library_slug: 'l1', actions: ['read'] }]);
    assert.equal(a.chat, true);
    assert.equal(a.documents, true);
    assert.equal(a.import, false);
    assert.equal(a.apiKeys, true);
});

test('menuAccess: insert-only user → import true, read pages false', () => {
    const a = menuAccess({}, [{ library_slug: 'l1', actions: ['insert'] }]);
    assert.equal(a.chat, false);
    assert.equal(a.import, true);
    assert.equal(a.apiKeys, true);
});

test('menuAccess: no permissions → only apiKeys', () => {
    const a = menuAccess({}, []);
    assert.equal(a.chat, false);
    assert.equal(a.import, false);
    assert.equal(a.apiKeys, true);
});

test('canAccessRoute: superuser always allowed', () => {
    assert.equal(canAccessRoute({ is_superuser: true }, [], 'read'), true);
    assert.equal(canAccessRoute({ is_superuser: true }, [], 'insert'), true);
});

test('canAccessRoute: normal user needs matching perm', () => {
    assert.equal(canAccessRoute({}, [{ actions: ['read'] }], 'read'), true);
    assert.equal(canAccessRoute({}, [{ actions: ['read'] }], 'insert'), false);
});

// ══════════════════════════════════════════════════
//  Task 2: CSV shared utils
// ══════════════════════════════════════════════════

test('csvEscape: formula injection (=, +, -, @) all prefixed', () => {
    for (const v of ['=SUM(A1)', '+trigger', '-cmd', '@ref']) {
        assert.ok(csvEscape(v).startsWith("'"), `${v} must be escaped`);
    }
});

test('csvEscape: comma and double-quote correctly escaped', () => {
    assert.equal(csvEscape('a,b'), '"a,b"');
    assert.equal(csvEscape('say "hi"'), '"say ""hi"""');
});

test('csvEscape: newline escaped', () => {
    assert.equal(csvEscape('line1\nline2'), '"line1\nline2"');
});

// ══════════════════════════════════════════════════
//  Task 3: Template regression checks
// ══════════════════════════════════════════════════

const appSrc = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const navigationSrc = readFileSync(new URL('./src/domain_navigation.js', import.meta.url), 'utf8');
const apiSrc = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const apiKeysSrc = readFileSync(new URL('./src/views/ApiKeys.js', import.meta.url), 'utf8');
const chatSrc = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const searchSrc = readFileSync(new URL('./src/views/Search.js', import.meta.url), 'utf8');
const auditSrc = readFileSync(new URL('./src/views/Audit.js', import.meta.url), 'utf8');

test('/operations-center/overview has admin:true', () => {
    assert.ok(appSrc.includes("path: 'overview'"), 'operations overview route exists');
    assert.ok(appSrc.includes("admin: true"), 'dashboard has admin:true meta');
});

test('defaultRoute: platform admin starts in the first accessible business domain', () => {
    assert.ok(
        navigationSrc.indexOf("for (const domain of ['knowledgeUse', 'knowledgeAssets', 'knowledgeGovernance'])")
            < navigationSrc.indexOf('if (access?.operationsCenter) return APP_PATHS.dashboard'),
        'business domains take precedence over operations overview',
    );
    assert.ok(appSrc.includes('defaultRouteForAccess'), 'app uses shared default projection');
});

test('authenticated root uses a real redirect instead of a component-less route guard', () => {
    assert.ok(
        appSrc.includes("{ path: '', redirect: () => ({ path: defaultRoute() }) }"),
        'root child route redirects to the shared authenticated default',
    );
    assert.ok(
        !appSrc.includes("{ path: '', beforeEnter: () => ({ path: defaultRoute() }) }"),
        'root child route does not rely on a component-less beforeEnter record',
    );
});

test('defaultRoute: no business permission → account profile', () => {
    assert.ok(navigationSrc.includes('return APP_PATHS.profile'), 'fallback enters account settings');
});

test('ApiKeys: refresh calls load(true)', () => {
    assert.ok(apiKeysSrc.includes('load(true)'), 'refresh passes forceRefresh');
});

test('ApiKeys: expired stat card present', () => {
    assert.ok(apiKeysSrc.includes('已过期'), 'expired label visible');
    assert.ok(apiKeysSrc.includes('api-keys-stat-card--expired'), 'expired class present');
});

test('Users: refresh calls loadUsers(true)', () => {
    const usersSrc = readFileSync(new URL('./src/views/Users.js', import.meta.url), 'utf8');
    assert.ok(usersSrc.includes('loadUsers(true)'), 'users refresh uses forceRefresh');
});

test('Chat: opens document source details in page', () => {
    assert.ok(chatSrc.includes('getDocumentSource'), 'Chat uses source location API');
    assert.ok(chatSrc.includes('sourceLocationDialog'), 'Chat has source location dialog state');
    assert.ok(!chatSrc.includes('router.resolve'), 'Chat no longer routes to Documents for source details');
    assert.ok(!chatSrc.includes('/console/'), 'Chat no longer hardcodes /console/');
});

test('Chat: source details do not open a new window', () => {
    assert.ok(!chatSrc.includes('window.open'), 'Chat source detail stays in page');
});

test('Chat: listChatLibraries accepts forceRefresh', () => {
    assert.ok(chatSrc.includes('listChatLibraries(true)') || chatSrc.includes('loadLibs(true)'), 'chat refresh uses forceRefresh');
});

test('Search: window.open uses noopener', () => {
    assert.ok(searchSrc.includes('noopener'), 'Search window.open has noopener');
});

test('Search: uses shared csvEscape/downloadCSV', () => {
    assert.ok(searchSrc.includes("from '../logs_ui.js'"), 'Search imports logs_ui');
    assert.ok(!searchSrc.includes('function csvField'), 'no local csvField');
    assert.ok(searchSrc.includes('downloadCSV('), 'uses downloadCSV');
});

test('Audit: no unused targetTypeKey import', () => {
    assert.ok(!auditSrc.includes('targetTypeKey'), 'targetTypeKey removed from Audit import');
});

test('primary pages use readable terms while keeping protocol fields internal', () => {
    const importSrc = readFileSync(new URL('./src/views/Import.js', import.meta.url), 'utf8');
    const jobsSrc = readFileSync(new URL('./src/views/Jobs.js', import.meta.url), 'utf8');
    assert.ok(importSrc.includes('externalId'));
    assert.ok(importSrc.includes('外部文档编号'));
    assert.ok(!importSrc.includes('placeholder="external_id'));
    assert.ok(jobsSrc.includes('处理服务（Worker）'));
    assert.ok(jobsSrc.includes('向量化'));
});

test('API: getLibraryJob removed', () => {
    assert.ok(!apiSrc.includes('getLibraryJob'), 'getLibraryJob removed');
});

test('API: sendChatMessage removed', () => {
    assert.ok(!apiSrc.includes('sendChatMessage'), 'sendChatMessage removed');
});

console.log('frontend audit test passed');
