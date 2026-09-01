const REASON_LABELS = {
    organization_mismatch: '不属于同一组织',
    library_index_unready: '知识库索引未就绪',
    embedding_verification_missing: '向量化校验缺失',
    embedding_verification_stale: '向量化校验已过期',
    embedding_profile_mismatch: '向量化配置不一致',
    retrieval_profile_mismatch: '检索策略不一致',
    graph_channel_disabled: '图谱检索未启用',
    graph_ontology_missing: '结构版本（Ontology）缺失',
    graph_ontology_ambiguous: '结构版本（Ontology）状态不明确',
    graph_schema_invalid: '知识结构（Schema）无效',
    graph_publication_missing: '图谱发布缺失',
    graph_publication_unhealthy: '图谱发布状态异常',
    graph_profile_mismatch: '图谱配置不一致',
};

const REWRITE_LABELS = {
    original: '原始问题',
    rule: '规则改写',
    llm: '模型改写',
};

const SAFE_REASON = /^[a-z0-9_:-]{1,64}$/;
const SAFE_LIBRARY_SLUG = /^[a-z0-9][a-z0-9-]{1,79}$/;

export function organizationAdminMemberships(organizations) {
    const seen = new Set();
    const rows = [];
    for (const item of organizations || []) {
        const organizationId = typeof item?.organization_id === 'string'
            ? item.organization_id
            : String(item?.organization_id || '');
        if (!organizationId || item?.role !== 'organization_admin' || seen.has(organizationId)) {
            continue;
        }
        seen.add(organizationId);
        rows.push(item);
    }
    return rows;
}

export function librariesForOrganization(permissions, organizationId) {
    const expectedId = String(organizationId || '');
    if (!expectedId) return [];
    const seen = new Set();
    const rows = [];
    for (const item of permissions || []) {
        const slug = typeof item?.library_slug === 'string' ? item.library_slug : '';
        if (!slug || seen.has(slug) || String(item?.organization_id || '') !== expectedId
            || !(item?.actions || []).includes('read')) continue;
        seen.add(slug);
        rows.push({ slug, name: item.library_name || slug });
    }
    return rows;
}

export function retrievalScopeKey(organizationId, librarySlugs) {
    const normalizedId = String(organizationId || '');
    if (!normalizedId || !Array.isArray(librarySlugs)) return '';
    return JSON.stringify({
        organizationId: normalizedId,
        librarySlugs: librarySlugs.map((value) => String(value || '')),
    });
}

export function validateRetrievalTest(values) {
    if (!String(values?.organizationId || '')) return '请选择组织';
    const librarySlugs = values?.librarySlugs;
    if (!Array.isArray(librarySlugs) || librarySlugs.length < 1 || librarySlugs.length > 20) {
        return '请选择 1 到 20 个知识库';
    }
    if (librarySlugs.some((value) => !SAFE_LIBRARY_SLUG.test(String(value || '')))
        || new Set(librarySlugs).size !== librarySlugs.length) {
        return '知识库范围包含无效或重复项';
    }
    const query = String(values?.query || '').trim();
    if (!query || query.length > 4000) return '检索内容需为 1 到 4000 个字符';
    const topK = Number(values?.topK);
    if (!Number.isInteger(topK) || topK < 1 || topK > 50) {
        return '返回数需为 1 到 50';
    }
    const candidateK = Number(values?.candidateK);
    if (!Number.isInteger(candidateK) || candidateK < topK || candidateK > 100) {
        return '候选数需不小于返回数且不超过 100';
    }
    const scoreThreshold = Number(values?.scoreThreshold);
    if (!Number.isFinite(scoreThreshold) || scoreThreshold < 0 || scoreThreshold > 1) {
        return '分数阈值需在 0 到 1 之间';
    }
    return '';
}

export function compatibilityReasonLabel(code) {
    if (REASON_LABELS[code]) return REASON_LABELS[code];
    return SAFE_REASON.test(String(code || '')) ? String(code) : '未知原因';
}

export function compatibilityLibraryReadiness(library) {
    return [
        { key: 'index', label: '索引', ready: library?.index_state === 'ready' },
        { key: 'embedding', label: 'Embedding', ready: library?.embedding_ready === true },
        { key: 'retrieval', label: '检索策略', ready: library?.retrieval_ready === true },
    ];
}

export function compatibilityItems(incompatibilities) {
    if (!Array.isArray(incompatibilities)) return [];
    const conflicts = [];
    for (const item of incompatibilities.slice(0, 20)) {
        if (!SAFE_LIBRARY_SLUG.test(String(item?.library_slug || ''))
            || !Array.isArray(item?.reason_codes)) continue;
        const reasons = item.reason_codes
            .filter((code) => typeof code === 'string' && SAFE_REASON.test(code))
            .slice(0, 13)
            .map(compatibilityReasonLabel);
        if (!reasons.length) continue;
        conflicts.push({ librarySlug: item.library_slug, reasons });
    }
    return conflicts;
}

export function compatibilityConflicts(error) {
    const detail = error?.body?.detail;
    if (detail?.code !== 'federated_scope_incompatible') return [];
    return compatibilityItems(detail.incompatibilities);
}

export function formatRetrievalNumber(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return '—';
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toFixed(6) : '—';
}

export function formatRetrievalPercent(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return '—';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '—';
    return `${(numeric * 100).toFixed(1)}%`;
}

export function retrievalSourceLocation(source) {
    const parts = [];
    const page = Number(source?.page);
    if (Number.isInteger(page) && page > 0) parts.push(`第 ${page} 页`);
    const titlePath = Array.isArray(source?.title_path)
        ? source.title_path.filter((item) => typeof item === 'string' && item).slice(0, 20)
        : [];
    if (titlePath.length) parts.push(titlePath.join(' / '));
    if (parts.length) return parts.join(' · ');
    const revision = Number(source?.document_revision);
    if (Number.isInteger(revision) && revision > 0) return `Revision v${revision}`;
    return '来源位置未标注';
}

export function shortRetrievalId(value, length = 12) {
    const text = String(value || '');
    if (!text) return '—';
    return text.length > length ? `${text.slice(0, length)}…` : text;
}

export function rewriteSourceLabel(value) {
    return REWRITE_LABELS[value] || (SAFE_REASON.test(String(value || '')) ? value : '未知');
}
