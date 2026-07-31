import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    filterUsers, paginateUsers, formatUserTime, userInitial, userRoleLabel, userStatusLabel,
} from './src/users_ui.js';

const source = readFileSync(new URL('./src/views/Users.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── Helpers ──
function u(overrides = {}) {
    return { id: '1', email: 'a@b.com', username: 'alice', display_name: 'Alice', is_superuser: false, is_active: true, is_verified: true, created_at: '2026-01-01T00:00:00Z', ...overrides };
}

// ════════════════════════════════════════════════════════════
//  users_ui.js pure function tests
// ════════════════════════════════════════════════════════════

test('filterUsers: keyword search across email, username, display_name', () => {
    const users = [u({ email: 'x@y.com' }), u({ username: 'bob' }), u({ display_name: 'Charlie' })];
    assert.equal(filterUsers(users, { keyword: 'x@y' }).length, 1);
    assert.equal(filterUsers(users, { keyword: 'bob' }).length, 1);
    assert.equal(filterUsers(users, { keyword: 'char' }).length, 1);
    assert.equal(filterUsers(users, { keyword: 'none' }).length, 0);
});

test('filterUsers: role filter', () => {
    const users = [u({ is_superuser: true }), u({ is_superuser: false }), u({ is_superuser: false })];
    assert.equal(filterUsers(users, { role: 'superuser' }).length, 1);
    assert.equal(filterUsers(users, { role: 'normal' }).length, 2);
    assert.equal(filterUsers(users, { role: '' }).length, 3);
});

test('filterUsers: status filter', () => {
    const users = [u({ is_active: true }), u({ is_active: false }), u({ is_active: true })];
    assert.equal(filterUsers(users, { status: 'active' }).length, 2);
    assert.equal(filterUsers(users, { status: 'disabled' }).length, 1);
});

test('paginateUsers: basic pagination', () => {
    const users = Array.from({ length: 25 }, (_, i) => u({ id: String(i) }));
    const p = paginateUsers(users, 1, 10);
    assert.equal(p.items.length, 10);
    assert.equal(p.total, 25);
    assert.equal(p.pageCount, 3);
    assert.equal(paginateUsers(users, 99, 10).page, 3);
});

test('formatUserTime: valid and null', () => {
    assert.ok(formatUserTime('2026-01-15T08:30:00Z').includes('2026'));
    assert.equal(formatUserTime(null), '—');
});

test('userInitial: first char uppercase', () => {
    assert.equal(userInitial(u({ display_name: 'Alice' })), 'A');
    assert.equal(userInitial(u({ display_name: '', username: 'bob' })), 'B');
    assert.equal(userInitial(u({ display_name: '', username: '', email: 'x@y.com' })), 'X');
});

test('userRoleLabel and userStatusLabel', () => {
    assert.equal(userRoleLabel(u({ is_superuser: true })), '超级管理员');
    assert.equal(userRoleLabel(u({ is_superuser: false })), '普通用户');
    assert.equal(userStatusLabel(u({ is_active: true })), '启用');
    assert.equal(userStatusLabel(u({ is_active: false })), '停用');
});

// ════════════════════════════════════════════════════════════
//  Users.js source checks
// ════════════════════════════════════════════════════════════

test('API calls unchanged', () => {
    for (const fn of ['api.listUsers', 'api.createUser', 'api.updateUser', 'api.disableUser', 'api.adminResetUserPassword', 'api.listLibraries', 'api.grantPerms']) {
        assert.ok(source.includes(fn), `preserves ${fn}`);
    }
    assert.ok(source.includes("include_deleted: 'false'"), 'listUsers excludes deleted');
    assert.ok(source.includes("limit: 500"), 'listUsers limit 500');
});

test('no fake features: batch import, phone, last_login', () => {
    for (const fake of ['批量导入', 'phone', 'last_login', '手机号', '最后登录']) {
        assert.ok(!source.includes(fake), `no fake feature: ${fake}`);
    }
});

test('self-protection: disables only super/delete dropdown items, edit always open', () => {
    assert.ok(source.includes('selfId'), 'selfId computed');
    assert.ok(source.includes('不能操作当前登录用户'), 'warning message in JS guard');
    assert.ok(source.includes(':disabled="row.id === selfId"'), 'dropdown items individually disabled');
    // Edit item: openEdit has no :disabled
    const openEditIdx = source.indexOf('openEdit(row)');
    const beforeOpenEdit = source.slice(Math.max(0, openEditIdx - 80), openEditIdx);
    assert.ok(!beforeOpenEdit.includes(':disabled'), 'edit item not disabled');
});

test('frontend pagination with 10/20/50 sizes', () => {
    assert.ok(source.includes('page-sizes'), 'page-sizes config');
    assert.ok(source.includes('[10, 20, 50]'), '10/20/50 options');
    assert.ok(source.includes('paginateUsers'), 'paginateUsers used');
});

test('filter toolbar with keyword, role, status', () => {
    assert.ok(source.includes("filters.keyword"), 'keyword filter');
    assert.ok(source.includes("filters.role"), 'role filter');
    assert.ok(source.includes("filters.status"), 'status filter');
});

test('zero inline style attributes in template', () => {
    const tpl = source.slice(source.indexOf('template:'));
    const count = (tpl.match(/style="/g) || []).length;
    assert.equal(count, 0, `zero inline style="...", got ${count}`);
});

test('uses illustration-empty-wrapper for empty table', () => {
    assert.ok(source.includes('illustration-empty-wrapper'), 'empty wrapper used');
});

test('read failures do not fall through to the successful empty state', () => {
    assert.ok(source.includes("usersReadState === 'fatal'"), 'fatal state is durable');
    assert.ok(source.includes("usersReadState === 'empty'"), 'successful empty state is explicit');
    assert.ok(source.includes("usersReadState === 'refresh-error'"), 'refresh failure preserves prior data');
    assert.ok(source.includes('usersRequestFence.isCurrent(requestToken)'), 'stale responses are fenced');
    assert.ok(source.includes('@click="loadUsers(true)">重试'), 'retry invokes the real read path');
});

test('CSS defines users-* classes', () => {
    for (const c of ['users-workspace', 'users-header', 'users-toolbar', 'users-table-card', 'users-avatar', 'users-status-dot']) {
        const e = c.replace(/-/g, '\\-');
        assert.match(css, new RegExp('\\.' + e + '\\s*\\{'), `${c} defined`);
    }
});

test('CSS includes users responsive at 899px', () => {
    assert.match(css, /899px[\s\S]*users-toolbar/, 'users toolbar responsive');
});

console.log('users redesign test passed');
