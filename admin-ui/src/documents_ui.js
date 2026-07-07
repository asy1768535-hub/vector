import { formatTime, paginate } from './common_ui.js';

const STATUS_LABEL = {
    pending: '等待中',
    processing: '处理中',
    ready: '完成',
    failed: '失败',
    deleted: '已删除',
};

const STATUS_TAG = {
    pending: 'info',
    processing: 'warning',
    ready: 'success',
    failed: 'danger',
    deleted: 'info',
};

const TYPE_LABEL = {
    pdf: 'PDF',
    word: 'Word',
    excel: 'Excel',
    markdown: 'Markdown',
    text: '文本',
    json: 'JSON',
    csv: 'CSV',
    other: '其他',
};

export function documentType(row) {
    const name = String(row?.title || row?.external_id || '').toLowerCase();
    const ext = name.includes('.') ? name.split('.').pop() : '';
    if (ext === 'pdf') return 'pdf';
    if (ext === 'doc' || ext === 'docx') return 'word';
    if (ext === 'xls' || ext === 'xlsx') return 'excel';
    if (ext === 'md' || ext === 'markdown') return 'markdown';
    if (ext === 'txt') return 'text';
    if (ext === 'json') return 'json';
    if (ext === 'csv') return 'csv';
    return 'other';
}

export function documentTypeLabel(row) {
    return TYPE_LABEL[documentType(row)] || TYPE_LABEL.other;
}

const FILE_TYPE_ASSETS = {
    pdf:      './assets/file-types/pdf.svg',
    word:     './assets/file-types/docx.svg',
    excel:    './assets/file-types/xlsx.svg',
    markdown: './assets/file-types/md.svg',
    text:     './assets/file-types/txt.svg',
    json:     './assets/file-types/json.svg',
    csv:      './assets/file-types/csv.svg',
};

/**
 * Return asset path for known file types, null for generic/unknown.
 * Callers use the built-in mdi:file-document-outline icon when null.
 */
export function documentTypeIcon(row) {
    const t = documentType(row);
    return FILE_TYPE_ASSETS[t] || null;
}

export function documentDisplayName(row) {
    if (row?.title) return row.title;
    if (row?.external_id) return row.external_id;
    if (row?.id) return `${String(row.id).slice(0, 8)}…`;
    return '未命名文档';
}

export function documentStatusLabel(status) {
    return STATUS_LABEL[status] || status || '未知';
}

export function documentStatusTag(status) {
    return STATUS_TAG[status] || 'info';
}

export function filterDocuments(rows, filters = {}) {
    const keyword = String(filters.keyword || '').trim().toLowerCase();
    const [startText, endText] = Array.isArray(filters.dateRange) ? filters.dateRange : [];
    const start = startText ? new Date(`${startText}T00:00:00`) : null;
    const end = endText ? new Date(`${endText}T23:59:59.999`) : null;
    if ((start && Number.isNaN(start.getTime())) || (end && Number.isNaN(end.getTime()))) return [];
    return (rows || []).filter((row) => {
        const haystack = [row.title, row.external_id, row.id].filter(Boolean).join(' ').toLowerCase();
        if (keyword && !haystack.includes(keyword)) return false;
        if (filters.status && row.status !== filters.status) return false;
        if (filters.type && documentType(row) !== filters.type) return false;
        if (start || end) {
            const updated = new Date(row.updated_at);
            if (Number.isNaN(updated.getTime())) return false;
            if (start && updated < start) return false;
            if (end && updated > end) return false;
        }
        return true;
    });
}

export function latestDocumentJob(jobs = []) {
    return [...(jobs || [])].sort((a, b) => {
        const at = new Date(a?.created_at || a?.finished_at || 0).getTime() || 0;
        const bt = new Date(b?.created_at || b?.finished_at || 0).getTime() || 0;
        return bt - at;
    })[0] || null;
}

export function paginateDocuments(rows, requestedPage = 1, requestedSize = 10) {
    return paginate(rows, requestedPage, requestedSize);
}

export function formatDocumentTime(value) {
    return formatTime(value);
}
