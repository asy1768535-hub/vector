// ── Service labels (shared by Dashboard & RuntimeStatus) ──
export const SERVICE_LABELS = {
    api: 'API',
    embedding_worker: '向量化处理服务（Worker）',
    cleanup_worker: '清理服务（Worker）',
};

export const STATUS_TAG = { online: 'success', degraded: 'warning', offline: 'danger' };
export const STATUS_TEXT = { online: '在线', degraded: '降级', offline: '离线' };

/** Seconds-since-epoch → relative Chinese text. Negative treated as 0. */
export function relTime(seconds) {
    if (seconds == null) return '—';
    if (seconds < 0) seconds = 0;
    if (seconds < 60) return `${seconds} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分前`;
    return `${Math.floor(seconds / 3600)} 小时前`;
}

/** Aggregate runtime summary for the top overview card.
 *  Returns null fields when data is absent — never fakes 0. */
export function computeRuntimeSummary(data) {
    if (!data) return {
        onlineServices: null, abnormalServices: null,
        embeddingPending: null, cleanupPending: null, rebuilding: null,
    };
    const svc = data.services || [];
    const online = svc.filter(s => s.status === 'online').length;
    const abnormal = svc.filter(s => s.status === 'degraded' || s.status === 'offline').length;
    const jobs = data.embedding_jobs || {};
    const outbox = data.cleanup_outbox || {};
    const libs = data.libraries || {};
    return {
        onlineServices: online,
        abnormalServices: abnormal,
        embeddingPending: jobs.pending != null ? jobs.pending : null,
        cleanupPending: outbox.pending != null ? outbox.pending : null,
        rebuilding: libs.rebuilding != null ? libs.rebuilding : null,
    };
}

/** Format an ISO timestamp for zh-CN, 24h. Returns '—' for empty/invalid input. */
export function formatOperationTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString('zh-CN', { hour12: false });
}

/** Map rebuild operation status → label + el-tag type. */
export function rebuildStatusMeta(status) {
    if (status === 'preparing') return { label: '准备中', type: 'warning' };
    if (status === 'running') return { label: '进行中', type: 'primary' };
    return { label: status, type: 'info' };
}
