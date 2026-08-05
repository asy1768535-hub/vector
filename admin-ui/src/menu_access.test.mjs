// 聚焦单测：menuAccess / canAccessRoute 的权限组合逻辑（Node 内置 assert）。
// 运行：node admin-ui/src/menu_access.test.mjs
import assert from 'node:assert/strict';
import {
    canAccessEffectiveRoute,
    canAccessOrganizationRoute,
    canAccessLibraryManagementRoute,
    canAccessRoute,
    menuAccess,
    manageableLibraries,
    readableLibraries,
    resolveSelectedSlug,
} from './menu_access.js';

const READER = [{ library_slug: 'a', actions: ['read'] }];
const INSERTER = [{ library_slug: 'a', actions: ['read', 'insert'] }];
const INSERT_ONLY = [{ library_slug: 'a', actions: ['insert'] }];
const NONE = [];
const SUPER = { is_superuser: true };
const USER = { is_superuser: false };

const cases = [
    // [name, user, perms, expected menuAccess]
    ['superuser 保留旧管理菜单但无有效 read 时不显示客户 Catalog', SUPER, NONE,
        { catalog: false, knowledgeGraph: false, documents: true, search: true, chat: true, import: true, organizationAdmin: false, retrievalTest: false, schemaLifecycle: false, apiKeys: true }],
    ['只有 read：文档/检索/问答可见，导入隐藏', USER, READER,
        { catalog: true, knowledgeGraph: true, documents: true, search: true, chat: true, import: false, organizationAdmin: false, retrievalTest: false, schemaLifecycle: false, apiKeys: true }],
    ['read+insert：全部业务菜单可见', USER, INSERTER,
        { catalog: true, knowledgeGraph: true, documents: true, search: true, chat: true, import: true, organizationAdmin: false, retrievalTest: false, schemaLifecycle: false, apiKeys: true }],
    ['只有 insert：导入可见，读类隐藏', USER, INSERT_ONLY,
        { catalog: false, knowledgeGraph: false, documents: false, search: false, chat: false, import: true, organizationAdmin: false, retrievalTest: false, schemaLifecycle: false, apiKeys: true }],
    ['无任何权限：仅 API Key 可见', USER, NONE,
        { catalog: false, knowledgeGraph: false, documents: false, search: false, chat: false, import: false, organizationAdmin: false, retrievalTest: false, schemaLifecycle: false, apiKeys: true }],
];

let passed = 0;
for (const [name, user, perms, expected] of cases) {
    const actual = menuAccess(user, perms);
    const legacyProjection = Object.fromEntries(
        Object.keys(expected).map((key) => [key, actual[key]]),
    );
    assert.deepEqual(legacyProjection, expected, `[FAIL menuAccess] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}

const readerDomains = menuAccess(USER, READER);
assert.equal(readerDomains.knowledgeUse, true);
assert.equal(readerDomains.knowledgeAssets, true);
assert.equal(readerDomains.knowledgeGovernance, true);
assert.equal(readerDomains.usersPermissions, false);
assert.equal(readerDomains.account, true);

const insertDomains = menuAccess(USER, INSERT_ONLY);
assert.equal(insertDomains.knowledgeUse, false);
assert.equal(insertDomains.knowledgeAssets, true);
assert.equal(insertDomains.knowledgeGovernance, false);

const superDomains = menuAccess(SUPER, NONE);
assert.equal(superDomains.usersPermissions, true);
assert.equal(superDomains.libraries, true);
assert.equal(superDomains.libraryConfiguration, true);
assert.equal(superDomains.operationsCenter, true);
assert.equal(superDomains.auditCenter, true);

// canAccessRoute：路由级
const routeCases = [
    ['superuser 任何 perm 放行', SUPER, NONE, 'insert', true],
    ['普通用户 read 路由：有 read 放行', USER, READER, 'read', true],
    ['普通用户 insert 路由：无 insert 拒绝', USER, READER, 'insert', false],
    ['普通用户 insert 路由：有 insert 放行', USER, INSERT_ONLY, 'insert', true],
    ['无 perm 要求：始终放行', USER, NONE, undefined, true],
    ['普通用户无权限：拒绝', USER, NONE, 'read', false],
];
for (const [name, user, perms, perm, expected] of routeCases) {
    assert.equal(canAccessRoute(user, perms, perm), expected, `[FAIL canAccessRoute] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}

assert.equal(canAccessEffectiveRoute(READER, 'read'), true, '[FAIL effective route] read');
assert.equal(canAccessEffectiveRoute(NONE, 'read'), false, '[FAIL effective route] empty');
assert.equal(canAccessEffectiveRoute(NONE, 'admin'), false, '[FAIL effective route] no superuser bypass');
assert.equal(
    canAccessOrganizationRoute([{ organization_id: 'org-a', role: 'organization_admin' }]),
    true,
    '[FAIL organization route] organization admin',
);
assert.equal(
    canAccessOrganizationRoute([{ organization_id: 'org-a', role: 'member' }]),
    false,
    '[FAIL organization route] normal member',
);
assert.equal(
    menuAccess(USER, READER, [{ organization_id: 'org-a', role: 'organization_admin' }]).retrievalTest,
    true,
    '[FAIL organization menu] exact role',
);

const managementPermissions = [
    { organization_id: 'org-a', library_slug: 'reader', library_name: '只读库', actions: ['read'] },
    { organization_id: 'org-a', library_slug: 'library-admin', library_name: '库管理员', actions: ['admin'] },
    { organization_id: 'org-b', library_slug: 'org-admin', library_name: '组织库', actions: ['read'] },
];
const organizationRows = [{ organization_id: 'org-b', role: 'organization_admin' }];
assert.deepEqual(manageableLibraries(managementPermissions, organizationRows), [
    { slug: 'library-admin', name: '库管理员', organizationId: 'org-a' },
    { slug: 'org-admin', name: '组织库', organizationId: 'org-b' },
]);
assert.equal(canAccessLibraryManagementRoute(managementPermissions, organizationRows), true);
assert.equal(canAccessLibraryManagementRoute(READER, []), false);
assert.equal(menuAccess(USER, managementPermissions, organizationRows).schemaLifecycle, true);
assert.equal(menuAccess(USER, managementPermissions, organizationRows).libraries, true);
assert.equal(menuAccess(USER, managementPermissions, organizationRows).libraryConfiguration, false);
assert.equal(menuAccess(SUPER, NONE, []).schemaLifecycle, false);

// readableLibraries / resolveSelectedSlug：Documents 默认库（P1-2）
let extra = 0;
function ok(name, cond) { assert.ok(cond, `[FAIL] ${name}`); console.log(`  ok  ${name}`); extra++; }

// 第一个 insert-only、第二个 read → 只能选第二个
const mixed = [
    { library_slug: 'a', actions: ['insert'], library_name: 'A库' },
    { library_slug: 'b', actions: ['read'], library_name: 'B库' },
];
const readable = readableLibraries(mixed);
ok('insert-only 被过滤，仅保留 read 库', readable.length === 1 && readable[0].slug === 'b');
ok('可读库带 library_name', readable[0].name === 'B库');
ok('默认 slug 从可读库选第一个', resolveSelectedSlug(null, readable) === 'b');
ok('当前 slug 不在可读库 → 切到第一个可读', resolveSelectedSlug('a', readable) === 'b');
ok('当前 slug 仍可读 → 保留', resolveSelectedSlug('b', readable) === 'b');

// 无 read 权限 → 空列表 + slug 置 null（上层据此不发请求）
const noRead = readableLibraries([{ library_slug: 'a', actions: ['insert'] }]);
ok('无 read 权限 → 可读库为空', noRead.length === 0);
ok('无可读库 → slug 为 null', resolveSelectedSlug('a', noRead) === null);
ok('library_name 缺失 → 回退 slug', readableLibraries([{ library_slug: 'x', actions: ['read'] }])[0].name === 'x');

console.log(`\n${passed + extra}/${cases.length + routeCases.length + extra} passed`);
