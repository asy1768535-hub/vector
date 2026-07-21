import { formatTime, paginate } from './common_ui.js';

// ── Filter ─────────────────────────────────────────────────
export function filterUsers(users, { keyword = '', role = '', status = '' } = {}) {
    const q = (keyword || '').toLowerCase();
    let list = users;
    if (q) {
        list = list.filter((u) =>
            (u.email || '').toLowerCase().includes(q) ||
            (u.username || '').toLowerCase().includes(q) ||
            (u.display_name || '').toLowerCase().includes(q)
        );
    }
    if (role === 'superuser') list = list.filter((u) => u.is_superuser);
    else if (role === 'normal') list = list.filter((u) => !u.is_superuser);
    if (status === 'active') list = list.filter((u) => u.is_active);
    else if (status === 'disabled') list = list.filter((u) => !u.is_active);
    return list;
}

// ── Pagination ─────────────────────────────────────────────
export function paginateUsers(users, page, size = 10) {
    return paginate(users, page, size);
}

// ── Time ───────────────────────────────────────────────────
export function formatUserTime(iso) {
    return formatTime(iso);
}

// ── Avatar initial ─────────────────────────────────────────
export function userInitial(user) {
    const s = user.display_name || user.username || user.email || '';
    return s.charAt(0).toUpperCase();
}

// ── Labels ─────────────────────────────────────────────────
export function userRoleLabel(user) {
    return user.is_superuser ? '超级管理员' : '普通用户';
}

export function userStatusLabel(user) {
    return user.is_active ? '启用' : '停用';
}
