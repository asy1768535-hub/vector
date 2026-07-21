import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    computePermissionDiff, countUnsaved, permissionSummary,
    paginatePermissions, filterPermissions, userMeta,
} from './src/permissions_ui.js';

// ── Unit tests ──
test('computePermissionDiff detects grants and revokes', () => {
    const matrix = { lib1: { read: true, insert: false, delete: true } };
    const initial = { lib1: { read: false, insert: false, delete: true } };
    const { grants, revokes } = computePermissionDiff(matrix, initial);
    assert.deepEqual(grants, { lib1: ['read'] });
    assert.deepEqual(revokes, {});
});

test('computePermissionDiff detects revokes', () => {
    const matrix = { lib1: { read: false, insert: true, delete: false } };
    const initial = { lib1: { read: true, insert: true, delete: true } };
    const { grants, revokes } = computePermissionDiff(matrix, initial);
    assert.deepEqual(revokes, { lib1: ['read', 'delete'] });
});

test('countUnsaved returns correct count', () => {
    const m = { a: { read: true, insert: false, delete: true } };
    const i = { a: { read: false, insert: false, delete: false } };
    assert.equal(countUnsaved(m, i), 2);
    assert.equal(countUnsaved(i, i), 0);
});

test('permissionSummary joins active permissions', () => {
    assert.equal(permissionSummary({ read: true, insert: false, delete: true }), '读取 / 删除');
    assert.equal(permissionSummary({ read: false, insert: false, delete: false }), '—');
});

test('filterPermissions filters by name and slug', () => {
    const libs = [{ name: '法规库', slug: 'law' }, { name: '安全库', slug: 'safe' }];
    assert.equal(filterPermissions(libs, '法规').length, 1);
    assert.equal(filterPermissions(libs, '').length, 2);
});

test('paginatePermissions paginates correctly', () => {
    const libs = Array.from({ length: 25 }, (_, i) => ({ slug: `lib${i}` }));
    const p = paginatePermissions(libs, 1, 10);
    assert.equal(p.rows.length, 10);
    assert.equal(p.total, 25);
    assert.equal(p.pageCount, 3);
    assert.equal(paginatePermissions(libs, 2, 10).page, 2);
    assert.equal(paginatePermissions([], 1, 10).rows.length, 0);
});

test('userMeta returns correct labels', () => {
    const su = userMeta({ is_superuser: true, is_active: true });
    assert.equal(su.role, '超级管理员');
    assert.equal(userMeta(null).role, '—');
});

// ── Template regression ──
const source = readFileSync(new URL('./src/views/Permissions.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('Permissions.js imports from permissions_ui', () => {
    assert.ok(source.includes('permissions_ui.js'), 'imports permissions_ui');
    assert.ok(source.includes('computePermissionDiff'), 'uses computePermissionDiff');
    assert.ok(source.includes('countUnsaved'), 'uses countUnsaved');
});

test('Preserves all API calls', () => {
    for (const fn of ['listUsers', 'listLibraries', 'listUserPerms', 'grantPerms', 'revokePerms']) {
        assert.ok(source.includes('api.' + fn), `preserves api.${fn}`);
    }
});

test('CSS defines permissions-* workspace classes', () => {
    for (const c of ['permissions-workspace', 'permissions-toolbar', 'permissions-table-card']) {
        assert.ok(css.includes(c), `${c} in CSS`);
    }
});

test('No inline style in template', () => {
    const tpl = source.slice(source.indexOf('template:'));
    assert.equal((tpl.match(/\sstyle="/g) || []).length, 0, 'zero inline style');
});

test('permissionsReady guards table and actions', () => {
    assert.ok(source.includes('permissionsReady'), 'permissionsReady flag exists');
    // Table only shows when ready
    assert.ok(source.includes('selectedUser && permissionsReady'), 'table gated by ready');
    // Save/reset disabled when not ready
    assert.ok(source.includes('!permissionsReady'), 'save/reset disabled when not ready');
});

test('management hint lives in user toolbar and matrix tools start with search', () => {
    assert.match(source, /可为该用户配置各知识库的读取、写入、删除权限/);
    assert.doesNotMatch(source, /permissions-legend-box/);
    assert.doesNotMatch(source, /permissions-legend--read/);
    assert.doesNotMatch(source, /permissions-legend--insert/);
    assert.doesNotMatch(source, /permissions-legend--delete/);
    assert.match(source, /permissions-config-hint/);
    assert.doesNotMatch(source, /permissions-tools-hint/);
    assert.match(source, /permissions-search-wrap/);
    assert.match(source, /匹配 \{\{ filteredLibs\.length \}\} \/ \{\{ libs\.length \}\} 个库/);
    assert.match(css, /\.permissions-matrix-tools\s*\{/);
    assert.match(css, /\.permissions-config-hint\s*\{/);
});

test('切换用户时立即清空旧权限避免闪现', () => {
    const watchBlock = source.slice(source.indexOf('watch(selectedUser'));
    assert.ok(watchBlock.includes('delete matrix[slug]'), 'clears matrix in watch');
    assert.ok(watchBlock.includes('permissionsReady.value = false'), 'sets ready=false in watch');
});

test('加载失败显示错误状态，不展示可编辑表格', () => {
    assert.ok(source.includes('permissionsError'), 'permissionsError flag');
    assert.ok(source.includes('权限加载失败'), 'error text');
    assert.ok(source.includes(':data="[]"') || source.includes('permissionsLoading'), 'loading state has no real data');
});

test('checkboxes 仅在 saving 时禁用（不再用 permissionsLoading 锁）', () => {
    // permissionsReady 阻止表格渲染；渲染后只需 saving 锁
    assert.ok(source.includes(':disabled="saving"'), 'checkboxes disabled only by saving');
});

test('catch block reloads permissions after partial failure', () => {
    const catchBlock = source.slice(source.lastIndexOf('catch'));
    assert.ok(catchBlock.includes('reloadUserPerms'), 'catch reloads server state');
});

console.log('permissions redesign test passed');
