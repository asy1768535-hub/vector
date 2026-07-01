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

export function documentType(row) {
    const name = String(row?.title || row?.external_id || '').toLowerCase();
    const ext = name.includes('.') ? name.split('.').pop() : '';
    if (ext === 'pdf') return 'pdf';
    if (ext === 'doc' || ext === 'docx') return 'word';
    if (ext === 'xls' || ext === 'xlsx') return 'excel';
    if (ext === 'md' || ext === 'markdown') return 'markdown';
    if (ext === 'txt') return 'text';
    return 'other';
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

export function paginateDocuments(rows, requestedPage = 1, requestedSize = 10) {
    const total = rows.length;
    const size = Number(requestedSize) > 0 ? Number(requestedSize) : 10;
    const pageCount = Math.max(1, Math.ceil(total / size));
    const page = Math.min(Math.max(1, Number(requestedPage) || 1), pageCount);
    const start = (page - 1) * size;
    return { items: rows.slice(start, start + size), total, page, pageCount };
}

export function formatDocumentTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', { hour12: false });
}
