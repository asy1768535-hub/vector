export function paginate(items, page, pageSize, itemKey = 'items') {
    const size = Math.max(1, Number(pageSize) || 10);
    const total = items.length;
    const pageCount = Math.max(1, Math.ceil(total / size));
    const current = Math.min(Math.max(1, Number(page) || 1), pageCount);
    const pageItems = items.slice((current - 1) * size, current * size);
    return { [itemKey]: pageItems, total, page: current, pageCount };
}

export function formatTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', { hour12: false });
}
