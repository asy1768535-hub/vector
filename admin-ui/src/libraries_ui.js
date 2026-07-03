import { paginate } from './common_ui.js';

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
