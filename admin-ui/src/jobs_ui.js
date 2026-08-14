import { formatTime, paginate } from './common_ui.js';

export const STATUS_LABEL = { pending: '待处理', processing: '处理中', done: '已完成', failed: '已失败', cancelled: '已取消', superseded: '已覆盖' };
export const STATUS_TAG = { pending: 'warning', processing: '', done: 'success', failed: 'danger', cancelled: 'info', superseded: 'info' };
export const STATUS_ICON = { pending: 'status:pending', processing: 'status:processing', done: 'status:success', failed: 'status:failed', cancelled: 'status:skipped', superseded: 'status:skipped' };

export const TASK_TYPE_LABEL = {
    import: '文件导入',
    embedding: '向量化',
    graph: '知识图谱',
};

export const STAGE_LABEL = {
    uploading: '上传文件',
    queued: '等待处理',
    validating: '校验文件',
    parsing: '解析文档',
    chunking: '生成切片',
    embedding: '生成向量',
    graph: '准备图谱',
    awaiting_graph: '等待创建图谱任务',
    preparing: '准备抽取',
    building_context: '构建上下文',
    extracting: '抽取实体与关系',
    binding_evidence: '绑定证据',
    aggregating: '聚合结果',
    scoring: '置信度评分',
    materializing: '写入图谱草稿',
    finalizing: '完成抽取',
    completed: '任务完成',
};

export const RETRY_REASON_LABEL = {
    supported: '可重试',
    not_needed: '已成功，无需重试',
    exhausted: '尝试次数耗尽',
    unsupported: '当前任务类型暂不支持',
    cancelled: '已取消',
    superseded: '已覆盖',
    unavailable: '当前不可重试',
    stale: '旧版本或非 production 任务不可重试',
};

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
    if (filters.task_type) list = list.filter((j) => j.task_type === filters.task_type);
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

export function jobErrorText(row) {
    if (row?.last_error) return row.last_error;
    if (row?.status !== 'failed') return '—';
    if (row.task_type === 'graph') return '图谱抽取失败，未记录详细错误';
    if (row.task_type === 'import') return '文件导入失败，未记录详细错误';
    if (row.task_type === 'embedding') return '向量化失败，未记录详细错误';
    return '历史任务未记录错误';
}

export function jobStageLabel(row) {
    const stage = row?.stage;
    if (row?.status === 'failed' && stage === 'finalizing') return '收尾阶段失败';
    if (row?.status === 'failed' && row?.task_type === 'graph' && stage === 'extracting') return '图谱抽取失败';
    return STAGE_LABEL[stage] || stage || '—';
}

export function retryReasonLabel(row) {
    return RETRY_REASON_LABEL[row?.retry_capability] || row?.retry_reason || '未提供重试信息';
}

export function retryTargetKey(row) {
    if (!row?.retry_target_type || !row?.retry_target_id) return null;
    return `${row.retry_target_type}:${row.retry_target_id}`;
}

export function isRetrySelectable(row) {
    return row?.status === 'failed' && row?.retryable === true && !!retryTargetKey(row);
}

export function uniqueRetryRows(rows) {
    const seen = new Set();
    return (rows || []).filter((row) => {
        if (!isRetrySelectable(row)) return false;
        const key = retryTargetKey(row);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

export function retryItem(row) {
    return {
        task_type: row.retry_target_type,
        job_id: row.retry_target_id,
        observed_generation: row.retry_generation ?? row.attempt_count ?? 0,
    };
}

export function retryTypeSummary(rows) {
    const counts = { embedding: 0, graph: 0, import: 0 };
    uniqueRetryRows(rows).forEach((row) => {
        counts[row.retry_target_type] = (counts[row.retry_target_type] || 0) + 1;
    });
    return `向量 ${counts.embedding || 0} 条、图谱 ${counts.graph || 0} 条、导入 ${counts.import || 0} 条`;
}

export function statsStatusTotal(stats) {
    return ['pending', 'processing', 'done', 'failed', 'cancelled', 'superseded']
        .reduce((sum, key) => sum + Number(stats?.[key] || 0), 0);
}
