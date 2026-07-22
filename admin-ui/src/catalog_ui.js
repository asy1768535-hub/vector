import { formatTime } from './common_ui.js';

const OVERALL_LABELS = {
    processing: '处理中',
    usable: '可用',
    partial: '部分可用',
    failed: '失败',
};

const OVERALL_TAGS = {
    processing: 'warning',
    usable: 'success',
    partial: 'warning',
    failed: 'danger',
};

const CAPABILITY_LABELS = {
    source: '原文',
    search: '检索',
    chat: '问答',
    summary: '摘要',
    outline: '大纲',
    classification: '分类',
    graph: '图谱',
};

const CAPABILITY_STATE_LABELS = {
    disabled: '未启用',
    unavailable: '暂无',
    processing: '处理中',
    ready: '可用',
    pending_review: '待审核',
    failed: '失败',
};

const CAPABILITY_STATE_TAGS = {
    disabled: 'info',
    unavailable: 'info',
    processing: 'warning',
    ready: 'success',
    pending_review: 'warning',
    failed: 'danger',
};

const CLASSIFICATION_LABELS = {
    unclassified: '未分类',
    pending_review: '待审核',
    classified: '已分类',
    failed: '分类失败',
};

const SOURCE_TYPE_LABELS = {
    manual: '人工',
    imported: '导入',
    extracted: '模型抽取',
};

export function catalogOverallLabel(value) {
    return OVERALL_LABELS[value] || '未知';
}

export function catalogOverallTag(value) {
    return OVERALL_TAGS[value] || 'info';
}

export function capabilityLabel(value) {
    return CAPABILITY_LABELS[value] || value || '未知';
}

export function capabilityStateLabel(value) {
    return CAPABILITY_STATE_LABELS[value] || '未知';
}

export function capabilityStateTag(value) {
    return CAPABILITY_STATE_TAGS[value] || 'info';
}

export function capabilityEntries(capabilities) {
    return Object.keys(CAPABILITY_LABELS).map((key) => ({
        key,
        label: capabilityLabel(key),
        state: capabilities?.[key] || 'unavailable',
    }));
}

export function classificationStateLabel(value) {
    return CLASSIFICATION_LABELS[value] || '未知';
}

export function classificationPrimary(classification) {
    return (classification?.labels || []).find((item) => item?.role === 'primary') || null;
}

export function classificationSecondary(classification) {
    return (classification?.labels || [])
        .filter((item) => item?.role === 'secondary')
        .sort((a, b) => Number(a?.ordinal || 0) - Number(b?.ordinal || 0));
}

export function collectCatalogLabels(items) {
    const byId = new Map();
    for (const item of items || []) {
        for (const label of item?.classification?.labels || []) {
            const id = typeof label?.id === 'string' ? label.id : String(label?.id || '');
            if (!id || byId.has(id)) continue;
            byId.set(id, {
                id,
                key: String(label?.key || ''),
                label: String(label?.label || label?.key || id),
                role: label?.role === 'primary' ? 'primary' : 'secondary',
                ordinal: Number.isInteger(label?.ordinal) ? label.ordinal : 0,
            });
        }
    }
    return [...byId.values()];
}

export function formatCatalogConfidence(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return '—';
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0 || numeric > 1) return '—';
    const percent = Math.round(numeric * 1000) / 10;
    return `${Number.isInteger(percent) ? percent.toFixed(0) : percent.toFixed(1)}%`;
}

export function formatCatalogTime(value) {
    return formatTime(value);
}

export function formatCatalogBytes(value) {
    const size = Number(value);
    if (!Number.isFinite(size) || size < 0) return '—';
    if (size < 1024) return `${size} B`;
    if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
    return `${(size / 1024 ** 3).toFixed(1)} GB`;
}

export function catalogSourceTypeLabel(value) {
    return SOURCE_TYPE_LABELS[value] || value || '未知';
}

export function catalogReviewLabel(value) {
    if (value === 'approved') return '已审核';
    if (value === 'not_required') return '无需审核';
    return value || '—';
}

export function catalogEvidenceParts(evidence) {
    const text = String(evidence?.text_window || evidence?.text_quote || '');
    if (!text) return [];
    const windowStart = Number(evidence?.window_start);
    const sourceStart = Number(evidence?.source_start);
    const sourceEnd = Number(evidence?.source_end);
    if (![windowStart, sourceStart, sourceEnd].every(Number.isFinite)) {
        return [{ text, highlight: false }];
    }
    const start = Math.max(0, Math.min(text.length, sourceStart - windowStart));
    const end = Math.max(start, Math.min(text.length, sourceEnd - windowStart));
    if (end <= start) return [{ text, highlight: false }];
    return [
        { text: text.slice(0, start), highlight: false },
        { text: text.slice(start, end), highlight: true },
        { text: text.slice(end), highlight: false },
    ].filter((part) => part.text);
}

export function catalogPageLabel(evidence) {
    const start = evidence?.page_start;
    const end = evidence?.page_end;
    if (!start) return '页码未知';
    return end && end !== start ? `第 ${start}–${end} 页` : `第 ${start} 页`;
}

export function catalogTitlePath(evidence) {
    return Array.isArray(evidence?.title_path) && evidence.title_path.length
        ? evidence.title_path.join(' / ')
        : '标题路径未知';
}

export function advanceCatalogCursor(state, nextCursor) {
    if (!nextCursor) return {
        history: [...(state?.history || [])],
        current: state?.current || null,
    };
    return {
        history: [...(state?.history || []), state?.current || null],
        current: nextCursor,
    };
}

export function retreatCatalogCursor(state) {
    const history = [...(state?.history || [])];
    if (!history.length) return { history, current: state?.current || null };
    const current = history.pop();
    return { history, current: current || null };
}

export function catalogErrorKind(error) {
    if (error?.status === 403) return 'forbidden';
    if (error?.status === 404) return 'unavailable';
    return 'error';
}

export function shortCatalogId(value, length = 12) {
    const text = String(value || '');
    return text.length > length ? `${text.slice(0, length)}…` : (text || '—');
}

export function safeCatalogAccessUrl(access) {
    const value = typeof access?.url === 'string' ? access.url.trim() : '';
    if (!value) return null;
    if (access?.access_mode === 'proxy') {
        return value.startsWith('/') && !value.startsWith('//') ? value : null;
    }
    if (access?.access_mode !== 'signed_url') return null;
    if (!/^https?:\/\//i.test(value)) return null;
    try {
        const parsed = new URL(value);
        return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
    } catch (_) {
        return null;
    }
}
