import { paginate } from './common_ui.js';

const SLUG_ALPHABET = 'abcdefghijklmnopqrstuvwxyz';

function hashedSlugSuffix(value) {
    let left = 2166136261;
    let right = 2246822519;
    for (const char of value) {
        const codePoint = char.codePointAt(0);
        left = Math.imul(left ^ codePoint, 16777619) >>> 0;
        right = Math.imul(right ^ codePoint, 3266489917) >>> 0;
    }
    let suffix = '';
    for (let index = 0; index < 12; index += 1) {
        left = Math.imul(left ^ (right + index), 16777619) >>> 0;
        right = Math.imul(right ^ (left + index), 2246822519) >>> 0;
        suffix += SLUG_ALPHABET[((left ^ right) >>> 0) % SLUG_ALPHABET.length];
    }
    return suffix;
}

export function librarySlugFromName(name) {
    const value = String(name || '').trim();
    if (!value) return '';
    const readable = value
        .normalize('NFKD')
        .toLowerCase()
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z]+/g, '_')
        .replace(/^_+|_+$/g, '')
        .slice(0, 80);
    return readable.length >= 2 ? readable : `library_${hashedSlugSuffix(value)}`;
}

export function computeLibraryStats(libs = []) {
    return {
        total: libs.length,
        active: libs.filter((row) => !row.deleted_at).length,
        ocr: libs.filter((row) => row.ocr_enabled === true).length,
        rerank: libs.filter((row) => row.rerank_enabled === true).length,
    };
}

export function filterLibraries(libs = [], filters = {}) {
    const keyword = String(filters.keyword || '').trim().toLowerCase();
    const status = filters.status || '';
    const retrievalMode = filters.retrievalMode || '';
    return libs.filter((row) => {
        const haystack = `${row.name || ''} ${row.slug || ''}`.toLowerCase();
        if (keyword && !haystack.includes(keyword)) return false;
        if (status === 'active' && row.deleted_at) return false;
        if (status === 'deleted' && !row.deleted_at) return false;
        if (retrievalMode && row.retrieval_mode !== retrievalMode) return false;
        return true;
    });
}

export function paginateLibraries(libs = [], page = 1, pageSize = 10) {
    const result = paginate(libs, page, pageSize, 'rows');
    return { page: result.page, pageSize: Math.max(1, Number(pageSize) || 10), total: result.total, rows: result.rows };
}

export function libraryStatus(row) {
    return row?.deleted_at
        ? { label: '已删除', type: 'info' }
        : { label: '正常', type: 'success' };
}

export function srcSummary(cfg) {
    if (!cfg) return '未配置';
    const table = cfg.db_name ? `${cfg.db_name}.${cfg.table}` : cfg.table;
    return `${table || '—'} · ${cfg.key_field || '—'} → ${cfg.text_column || '—'}`;
}
