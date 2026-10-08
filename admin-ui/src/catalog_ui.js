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

const PROCESSING_STAGE_LABELS = {
    summary: '摘要',
    outline: '大纲',
    classification: '分类',
    graph: '图谱抽取',
};

const PROCESSING_STATUS_LABELS = {
    not_started: '尚未开始',
    queued: '等待处理',
    processing: '处理中',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
    superseded: '已过期',
    partially_succeeded: '部分完成',
};

const PROCESSING_STATUS_TAGS = {
    not_started: 'info',
    queued: 'warning',
    processing: 'warning',
    succeeded: 'success',
    failed: 'danger',
    cancelled: 'info',
    superseded: 'info',
    partially_succeeded: 'warning',
};

const PROCESSING_ERROR_LABELS = {
    attempt_limit_exceeded: '已达到最大尝试次数',
    cancelled_by_user: '任务已被取消',
    claim_lost: '处理任务已失去执行权',
    historical_revision: '任务对应的文档版本已过期',
    invalid_provider_json: '模型返回格式无效',
    invalid_provider_payload: '模型返回内容无效',
    job_identity_stale: '任务配置已发生变化',
    job_input_stale: '任务输入已发生变化',
    lease_expired: '处理任务执行超时',
    lease_expired_attempt_limit: '处理超时且已达到尝试上限',
    provider_http_error: '模型服务返回失败',
    provider_network_error: '模型服务网络异常',
    provider_timeout: '模型服务响应超时',
    revision_content_changed: '文档内容已发生变化',
    revision_not_ready: '当前文档版本尚未就绪',
    security_level_denied: '当前文档安全级别不允许处理',
    security_level_missing: '当前文档缺少安全级别',
    stage_failed: '该阶段处理失败',
};

const PARSING_COVERAGE_LABELS = {
    complete: '完整识别',
    partial: '部分识别',
    unknown: '覆盖未知',
};

const PARSING_COVERAGE_TAGS = {
    complete: 'success',
    partial: 'warning',
    unknown: 'info',
};

const PARSING_COVERAGE_REASON_LABELS = {
    visual_content_not_ingested: '图片或图形内容未进入检索',
    visual_content_without_ocr: '页面含未识别的视觉内容',
};

export function isPdfCatalogFile(file) {
    const contentType = String(file?.content_type || '').split(';', 1)[0].trim().toLowerCase();
    if (contentType) return contentType === 'application/pdf';
    return String(file?.file_name || '').trim().toLowerCase().endsWith('.pdf');
}


export function parsingCoverageLabel(value) {
    return PARSING_COVERAGE_LABELS[value] || PARSING_COVERAGE_LABELS.unknown;
}

export function parsingCoverageTag(value) {
    return PARSING_COVERAGE_TAGS[value] || PARSING_COVERAGE_TAGS.unknown;
}

export function parsingCoverageMessage(coverage) {
    const blankPages = Array.isArray(coverage?.confirmed_blank_pages)
        ? coverage.confirmed_blank_pages : [];
    const noEffectivePages = Array.isArray(coverage?.no_effective_content_pages)
        ? coverage.no_effective_content_pages : [];
    const assessments = Array.isArray(coverage?.page_assessments) ? coverage.page_assessments : [];
    const hasConfirmedBlanks = blankPages.length > 0;
    const blankDetail = hasConfirmedBlanks
        ? `确认空白页：${blankPages.join('、')}（${blankPages.length}页）`
        : '';
    const noEffectiveBySource = new Map(assessments
        .filter((item) => item?.status === 'no_effective_content' && Number.isInteger(item.page))
        .map((item) => [item.page, item.classification_source]));
    const reviewedNoEffective = noEffectivePages.filter((page) => noEffectiveBySource.get(page) === 'human_review');
    const automaticNoEffective = noEffectivePages.filter((page) => noEffectiveBySource.get(page) === 'automatic');
    const unmarkedNoEffective = noEffectivePages.filter((page) => !noEffectiveBySource.has(page));
    const noEffectiveDetails = [];
    if (reviewedNoEffective.length) {
        noEffectiveDetails.push(`人工复核无有效可提取内容页：${reviewedNoEffective.join('、')}`);
    }
    if (automaticNoEffective.length) {
        noEffectiveDetails.push(`自动判定无有效可提取内容页：${automaticNoEffective.join('、')}`);
    }
    if (unmarkedNoEffective.length) {
        noEffectiveDetails.push(`来源未记录的无有效可提取内容页：${unmarkedNoEffective.join('、')}`);
    }
    const status = coverage?.status;
    if (!coverage || status === 'unknown') {
        const pages = Array.isArray(coverage?.processed_pages) ? coverage.processed_pages : [];
        const details = [];
        if (blankDetail) details.push(blankDetail);
        details.push(...noEffectiveDetails);
        if (status === 'unknown' && pages.length) details.push(`已处理页：${pages.join('、')}；其余范围无法确认`);
        else if (!details.length) return '当前版本没有可核验的解析覆盖记录';
        return details.join('；');
    }
    if (coverage.status === 'complete') {
        if (hasConfirmedBlanks || noEffectiveDetails.length) {
            const total = Number.isInteger(coverage.total_pages) ? `共${coverage.total_pages}页，` : '';
            const details = [blankDetail, ...noEffectiveDetails].filter(Boolean).join('；');
            return `${total}${details}；页面覆盖完整，不代表文字识别准确`;
        }
        return Number.isInteger(coverage.total_pages)
            ? `已处理全部 ${coverage.total_pages} 页`
            : '已处理全部可确认内容';
    }
    const details = [];
    if (blankDetail) details.push(blankDetail);
    details.push(...noEffectiveDetails);
    const pages = Array.isArray(coverage.unprocessed_visual_pages)
        ? coverage.unprocessed_visual_pages : [];
    if (!assessments.length) {
        if (pages.length) details.push(`视觉未覆盖页：${pages.join('、')}`);
    } else {
        const failedPages = new Set(assessments
            .filter((item) => item?.status === 'failed' && Number.isInteger(item.page))
            .map((item) => item.page));
        const failed = pages.filter((page) => failedPages.has(page));
        const uncertain = pages.filter((page) => !failedPages.has(page));
        if (uncertain.length) details.push(`待确认视觉内容页：${uncertain.join('、')}`);
        if (failed.length) details.push(`处理异常页：${failed.join('、')}`);
    }
    const skipped = Number(coverage.skipped_visual_block_count);
    if (Number.isInteger(skipped) && skipped > 0) details.push(`跳过视觉块：${skipped}`);
    for (const reason of coverage.reasons || []) {
        const label = PARSING_COVERAGE_REASON_LABELS[reason];
        if (label && !details.includes(label)) details.push(label);
    }
    return details.join('；') || '部分内容未被解析，请结合源文件核对';
}
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

export function processingStageLabel(value) {
    return PROCESSING_STAGE_LABELS[value] || '未知阶段';
}

export function processingStatusLabel(value) {
    return PROCESSING_STATUS_LABELS[value] || '未知状态';
}

export function processingStatusTag(value) {
    return PROCESSING_STATUS_TAGS[value] || 'info';
}

export function processingErrorLabel(value) {
    return PROCESSING_ERROR_LABELS[value] || '该阶段处理失败';
}

export function processingErrorKind(error) {
    if (error?.status === 403) return 'forbidden';
    if (error?.status === 404) return 'unavailable';
    if (error?.status === 409) return 'conflict';
    return 'error';
}

export function processingStageRetryable(stage) {
    return Boolean(
        Object.hasOwn(PROCESSING_STAGE_LABELS, stage?.stage)
        && stage?.availability === 'enabled'
        && stage?.retryable === true
        && stage?.job_id
        && Number.isInteger(stage?.retry_generation)
        && stage.retry_generation >= 0
    );
}

export function processingResponseMatches(value, identity) {
    return Boolean(
        value
        && String(value.library_id || '') === String(identity?.libraryId || '')
        && String(value.document_id || '') === String(identity?.documentId || '')
        && String(value.document_revision_id || '') === String(identity?.revisionId || '')
    );
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
