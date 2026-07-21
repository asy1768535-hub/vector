import { paginate } from './common_ui.js';

const ACTIONS = ['read', 'insert', 'delete'];

export function computePermissionDiff(matrix, initial) {
    const grants = {};
    const revokes = {};
    for (const slug of Object.keys(matrix)) {
        const cur = matrix[slug] || {};
        const snap = initial[slug] || {};
        for (const a of ACTIONS) {
            if (cur[a] && !snap[a]) (grants[slug] || (grants[slug] = [])).push(a);
            if (!cur[a] && snap[a]) (revokes[slug] || (revokes[slug] = [])).push(a);
        }
    }
    return { grants, revokes };
}

export function countUnsaved(matrix, initial) {
    let count = 0;
    for (const slug of Object.keys(matrix)) {
        const cur = matrix[slug] || {};
        const snap = initial[slug] || {};
        for (const a of ACTIONS) if (cur[a] !== snap[a]) count++;
    }
    return count;
}

export function permissionSummary(row) {
    const parts = [];
    if (row.read) parts.push('读取');
    if (row.insert) parts.push('写入');
    if (row.delete) parts.push('删除');
    return parts.length ? parts.join(' / ') : '—';
}

export function filterPermissions(libs, keyword) {
    if (!keyword) return libs;
    const q = keyword.toLowerCase();
    return libs.filter((l) =>
        (l.name || '').toLowerCase().includes(q) ||
        (l.slug || '').toLowerCase().includes(q)
    );
}

export function paginatePermissions(libs, page, pageSize) {
    return paginate(libs, page, pageSize, 'rows');
}

export function userMeta(user) {
    if (!user) return { role: '—', roleType: 'info', status: '—', statusType: 'info' };
    return {
        role: user.is_superuser ? '超级管理员' : '普通用户',
        roleType: user.is_superuser ? 'danger' : 'info',
        status: user.is_active ? '启用' : '停用',
        statusType: user.is_active ? 'success' : 'info',
    };
}
