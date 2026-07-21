import { formatTime, paginate } from './common_ui.js';

export const STATUS_LABEL = { pending: '待处理', processing: '处理中', done: '已完成', failed: '已失败', superseded: '已覆盖' };
export const STATUS_TAG = { pending: 'warning', processing: '', done: 'success', failed: 'danger', superseded: 'info' };
export const STATUS_ICON = { pending: 'status:pending', processing: 'status:processing', done: 'status:success', failed: 'status:failed', superseded: 'status:skipped' };

export function formatJobTime(iso) {
    return formatTime(iso);
}

export function jobDuration(row) {
    const startRaw = row.claimed_at || row.created_at;
    if (!startRaw) return '—';
    const start = new Date(startRaw).getTime();
    if (Number.isNaN(start)) return '—';
    let end;
    if (row.finished_at) {
        const t = new Date(row.finished_at).getTime();
        if (Number.isNaN(t)) return '—';
        end = t;
    } else {
        end = Date.now();
    }
    const ms = Math.max(0, end - start);
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + (s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

export function shortId(id) {
    if (!id) return '—';
    const s = String(id);
    return s.length > 10 ? s.slice(0, 8) + '…' : s;
}

export function filterJobs(jobs, filters) {
    let list = jobs;
    if (filters.status) list = list.filter((j) => j.status === filters.status);
    if (filters.library_id) list = list.filter((j) => j.library_id === filters.library_id);
    if (filters.worker_id) list = list.filter((j) => (j.worker_id || '').toLowerCase().includes(filters.worker_id.toLowerCase()));
    if (filters.document_id) list = list.filter((j) => (j.document_id || '').toLowerCase().includes(filters.document_id.toLowerCase()));
    if (filters.dateFrom) list = list.filter((j) => j.created_at && j.created_at >= filters.dateFrom);
    if (filters.dateTo) {
        const toEnd = filters.dateTo + 'T23:59:59';
        list = list.filter((j) => j.created_at && j.created_at <= toEnd);
    }
    return list;
}

export function paginateJobs(jobs, page, pageSize) {
    return paginate(jobs, page, pageSize);
}

export function libraryName(libs, libraryId) {
    const lib = (libs || []).find((l) => l.id === libraryId);
    return lib ? `${lib.name} (${lib.slug})` : (libraryId ? shortId(libraryId) : '—');
}
