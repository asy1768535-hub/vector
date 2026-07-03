import { formatTime, paginate } from './common_ui.js';

// ── Time formatting ────────────────────────────────────────
export function formatKeyTime(iso) {
    return formatTime(iso);
}

// ── Key status ─────────────────────────────────────────────
// Priority: revoked > expired > active
export function keyStatus(key) {
    if (key.revoked_at) return 'revoked';
    if (key.expires_at && new Date(key.expires_at) < new Date()) return 'expired';
    return 'active';
}

export const STATUS_LABEL = {
    active: '启用',
    revoked: '已撤销',
    expired: '已过期',
};

export const STATUS_TAG = {
    active: 'success',
    revoked: 'info',
    expired: 'warning',
};

// ── Statistics (computed from full keys array) ─────────────
export function computeStats(keys) {
    let active = 0;
    let revoked = 0;
    let expired = 0;
    for (const k of keys) {
        const s = keyStatus(k);
        if (s === 'active') active++;
        else if (s === 'revoked') revoked++;
        else expired++;
    }
    return { total: keys.length, active, revoked, expired };
}

// ── Frontend pagination ────────────────────────────────────
export function paginateKeys(keys, page, size = 10) {
    return paginate(keys, page, size);
}
