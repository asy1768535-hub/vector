const STATUS_LABELS = {
    pending_review: '待审核',
    blocked_manual: '人工结果保护中',
};

const STATUS_TAGS = {
    pending_review: 'warning',
    blocked_manual: 'danger',
};

const ROLE_LABELS = {
    primary: '主分类',
    secondary: '辅助分类',
};

const REASON_LABELS = {
    primary_confidence_below_threshold: '主分类置信度不足',
    primary_margin_below_threshold: '主分类区分度不足',
    secondary_confidence_below_threshold: '辅助分类置信度不足',
    label_not_enabled: '分类未在当前知识库启用',
    unknown_label: '发现未知分类建议',
    missing_primary: '缺少主分类建议',
    duplicate_label: '分类建议重复',
    label_disabled: '分类已停用',
    secondary_limit_exceeded: '辅助分类数量超限',
    blocked_manual: '已有人工分类结果',
};

const SAFE_CODE = /^[a-z0-9_:-]{1,64}$/;
const REVIEW_STATUSES = new Set(Object.keys(STATUS_LABELS));

function availableIdSet(page) {
    return new Set((page?.available_labels || []).map((label) => String(label?.id || '')));
}

export function reviewStatusLabel(value) {
    return STATUS_LABELS[value] || '未知状态';
}

export function reviewStatusTag(value) {
    return STATUS_TAGS[value] || 'info';
}

export function reviewRoleLabel(value) {
    return ROLE_LABELS[value] || '分类建议';
}

export function reviewReasonLabel(value) {
    if (REASON_LABELS[value]) return REASON_LABELS[value];
    return SAFE_CODE.test(String(value || '')) ? String(value) : '需要人工确认';
}

export function formatReviewConfidence(value) {
    if (!Number.isInteger(value) || value < 0 || value > 1_000_000) return '—';
    return `${(value / 10_000).toFixed(1)}%`;
}

export function reviewProposalLabel(proposal) {
    if (proposal?.label_id && proposal?.label) return String(proposal.label);
    if (proposal?.proposed_label) return String(proposal.proposed_label);
    if (proposal?.proposed_key) return String(proposal.proposed_key);
    return '未知分类';
}

export function reviewCanAccept(run, page) {
    if (!REVIEW_STATUSES.has(run?.status)
        || String(run?.taxonomy_version_id || '') !== String(page?.taxonomy_version_id || '')) {
        return false;
    }
    const proposals = Array.isArray(run?.proposals) ? run.proposals : [];
    const primary = proposals.filter((proposal) => proposal?.role === 'primary');
    const enabled = availableIdSet(page);
    return proposals.length > 0
        && primary.length === 1
        && proposals.every((proposal) => (
            proposal?.label_id && enabled.has(String(proposal.label_id))
        ));
}

export function initialReviewSelection(run, page) {
    const enabled = availableIdSet(page);
    const proposals = Array.isArray(run?.proposals) ? run.proposals : [];
    const primary = proposals
        .filter((proposal) => proposal?.role === 'primary')
        .sort((left, right) => Number(left?.rank || 0) - Number(right?.rank || 0))
        .find((proposal) => enabled.has(String(proposal?.label_id || '')));
    const secondaryLabelIds = proposals
        .filter((proposal) => proposal?.role === 'secondary')
        .sort((left, right) => Number(left?.rank || 0) - Number(right?.rank || 0))
        .map((proposal) => String(proposal?.label_id || ''))
        .filter((id, index, values) => id && enabled.has(id) && values.indexOf(id) === index)
        .filter((id) => id !== String(primary?.label_id || ''))
        .slice(0, 8);
    return {
        primaryLabelId: String(primary?.label_id || ''),
        secondaryLabelIds,
    };
}

export function validateReviewSelection(primaryLabelId, secondaryLabelIds, labels) {
    const enabled = new Set((labels || []).map((label) => String(label?.id || '')));
    const primary = String(primaryLabelId || '');
    if (!primary || !enabled.has(primary)) return '请选择当前知识库已启用的主分类';
    if (!Array.isArray(secondaryLabelIds) || secondaryLabelIds.length > 8) {
        return '辅助分类最多选择 8 个';
    }
    const normalized = secondaryLabelIds.map((value) => String(value || ''));
    if (new Set(normalized).size !== normalized.length) return '辅助分类不能重复';
    if (normalized.includes(primary)) return '主分类不能同时作为辅助分类';
    if (normalized.some((id) => !enabled.has(id))) return '辅助分类必须来自当前启用范围';
    return '';
}

export function reviewPageMatches(value, identity) {
    if (!value || !Array.isArray(value.items) || !Array.isArray(value.available_labels)
        || value.limit !== identity?.limit || value.offset !== identity?.offset
        || !Number.isInteger(value.total) || value.total < 0) return false;
    const taxonomyVersionId = String(value.taxonomy_version_id || '');
    const availableLabelIds = new Set();
    for (const label of value.available_labels) {
        const labelId = String(label?.id || '');
        if (!taxonomyVersionId || !labelId || availableLabelIds.has(labelId)
            || String(label?.taxonomy_version_id || '') !== taxonomyVersionId) return false;
        availableLabelIds.add(labelId);
    }
    const libraryIds = new Set();
    for (const item of value.items) {
        if (!item?.id || !item?.document_id || !item?.document_revision_id
            || !REVIEW_STATUSES.has(item?.status) || !Array.isArray(item?.proposals)
            || !Array.isArray(item?.effective_decisions)) return false;
        libraryIds.add(String(item.library_id || ''));
    }
    return !libraryIds.has('') && libraryIds.size <= 1;
}

export function classificationReviewErrorKind(error) {
    if (error?.status === 401) return 'unauthorized';
    if (error?.status === 403) return 'forbidden';
    if (error?.status === 404) return 'unavailable';
    if (error?.status === 409) return 'conflict';
    if (error?.status === 422) return 'invalid';
    return 'error';
}

export function classificationReviewErrorMessage(kind) {
    const messages = {
        unauthorized: '登录状态已失效，请重新登录',
        forbidden: '你没有管理这个知识库分类的权限',
        unavailable: '分类审核功能暂未启用',
        conflict: '文档或分类状态已变化，审核队列已重新加载',
        invalid: '审核选择不符合当前分类规则',
        malformed: '审核队列返回的数据不完整，请刷新重试',
        error: '分类审核请求失败，请稍后重试',
    };
    return messages[kind] || messages.error;
}

export function currentPrimaryDecision(run) {
    return (run?.effective_decisions || []).find((item) => item?.role === 'primary') || null;
}

export function currentSecondaryDecisions(run) {
    return (run?.effective_decisions || [])
        .filter((item) => item?.role === 'secondary')
        .sort((left, right) => Number(left?.ordinal || 0) - Number(right?.ordinal || 0));
}
