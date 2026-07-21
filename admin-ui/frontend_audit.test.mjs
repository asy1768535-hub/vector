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
const apiSrc = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const apiKeysSrc = readFileSync(new URL('./src/views/ApiKeys.js', import.meta.url), 'utf8');
const chatSrc = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const docsSrc = readFileSync(new URL('./src/views/Documents.js', import.meta.url), 'utf8');
const searchSrc = readFileSync(new URL('./src/views/Search.js', import.meta.url), 'utf8');
const auditSrc = readFileSync(new URL('./src/views/Audit.js', import.meta.url), 'utf8');

test('/dashboard has admin:true', () => {
    assert.ok(appSrc.includes('path: \'dashboard\''), 'dashboard route exists');
    assert.ok(appSrc.includes("admin: true"), 'dashboard has admin:true meta');
});

test('defaultRoute: superuser → /dashboard', () => {
    assert.ok(appSrc.includes("/dashboard'") || appSrc.includes('/dashboard"'), 'superuser redirect to dashboard');
    assert.ok(appSrc.includes('is_superuser'), 'checks is_superuser for default route');
});

test('defaultRoute: no perm → /api-keys', () => {
    assert.ok(appSrc.includes('/api-keys'), 'fallback to api-keys');
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

test('Documents: refresh calls loadDocs(true)', () => {
    assert.ok(docsSrc.includes('loadDocs(true)'), 'docs refresh uses forceRefresh');
});

test('Documents: loadLibs calls loadDocs exactly once (no duplicate branch)', () => {
    const afterLibs = docsSrc.slice(docsSrc.indexOf('async function loadLibs'));
    const fnBody = afterLibs.slice(0, afterLibs.indexOf('async function loadDocs'));
    const matches = fnBody.match(/loadDocs\(\)/g) || [];
    assert.equal(matches.length, 1, `loadLibs calls loadDocs ${matches.length} times, expected 1`);
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

test('API: getLibraryJob removed', () => {
    assert.ok(!apiSrc.includes('getLibraryJob'), 'getLibraryJob removed');
});

test('API: sendChatMessage removed', () => {
    assert.ok(!apiSrc.includes('sendChatMessage'), 'sendChatMessage removed');
});

console.log('frontend audit test passed');
