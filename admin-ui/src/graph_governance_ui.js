import { canManageLibrary } from './menu_access.js';

const GRAPH_TABS = new Set(['entities', 'relations', 'review', 'publications']);
const MANAGEMENT_TABS = new Set(['review', 'publications']);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const HASH_RE = /^[0-9a-f]{64}$/;
const SAFE_LIBRARY_SLUG = /^[A-Za-z0-9_.:-]{1,80}$/;
const SAFE_REASON = /^[a-z][a-z0-9_:-]{0,63}$/;

const FACT_LABELS = {
    draft: '草稿',
    pending_review: '待审核',
    active: '生效',
    rejected: '已驳回',
    disabled: '已停用',
    deleted: '已删除',
};

const REVIEW_LABELS = {
    pending_review: '待审核',
    approved: '已通过',
    rejected: '已驳回',
    not_required: '无需审核',
};

const PUBLICATION_LABELS = {
    planned: '待发布',
    activating: '发布中',
    active: '当前生效',
    degraded: '状态异常',
    superseded: '已被替代',
    cancelled: '已取消',
    failed: '发布失败',
    published: '已发布',
    staged: '待发布',
};

const ACTION_LABELS = {
    entity_create: '新增实体',
    relation_create: '新增关系',
    entity_correct: '修正实体',
    relation_correct: '修正关系',
    alias_add: '新增别名',
    relation_review: '审核关系',
    entity_disable: '停用实体',
    entity_restore: '恢复实体',
    relation_disable: '停用关系',
    relation_restore: '恢复关系',
    alias_disable: '停用别名',
    entity_merge: '合并实体',
};

const CONFLICT_REASON_LABELS = {
    organization_mismatch: '不属于同一组织',
    graph_channel_disabled: '图谱检索未启用',
    graph_ontology_missing: '缺少可用 Ontology',
    graph_ontology_ambiguous: 'Ontology 状态不明确',
    graph_schema_invalid: 'Schema 无效',
    graph_publication_missing: '缺少图谱发布版本',
    graph_publication_unhealthy: '图谱发布状态异常',
    graph_profile_mismatch: '图谱配置不一致',
};

const ERROR_MESSAGES = {
    forbidden: '你没有访问或管理当前知识库的权限。',
    unavailable: '知识图谱功能暂未启用或当前内容不存在。',
    conflict: '数据已发生变化，请重新加载后再操作。',
    invalid: '提交内容不符合当前图谱约束，请检查后重试。',
    malformed: '服务返回的数据不完整，请重新加载。',
    error: '知识图谱服务暂时不可用，请稍后重试。',
};

function text(value) {
    return typeof value === 'string' ? value : String(value || '');
}

function validUuid(value) {
    return UUID_RE.test(text(value));
}

function uniqueStrings(values, limit = 100) {
    if (!Array.isArray(values) || values.length > limit) return null;
    const result = values.map(text);
    return result.every(Boolean) && new Set(result).size === result.length ? result : null;
}

function organizationNames(organizations) {
    const names = new Map();
    for (const item of organizations || []) {
        const id = text(item?.organization_id || item?.id);
        if (!id || names.has(id)) continue;
        names.set(id, text(item?.organization_name || item?.name || item?.slug || id));
    }
    return names;
}

export function graphLibraryCapabilities(permissions, organizations, librarySlug) {
    const slug = text(librarySlug);
    const rows = (permissions || []).filter((item) => item?.library_slug === slug);
    const actions = new Set(rows.flatMap((item) => item?.actions || []));
    const organizationIds = new Set(rows.map((item) => text(item?.organization_id)).filter(Boolean));
    return {
        read: actions.has('read'),
        insert: actions.has('insert'),
        manage: canManageLibrary(permissions, organizations, slug),
        organizationId: organizationIds.size === 1 ? [...organizationIds][0] : '',
    };
}

export function graphScopeOptions(permissions, organizations = []) {
    const names = organizationNames(organizations);
    const groups = new Map();
    const seenLibraries = new Set();
    for (const item of permissions || []) {
        const slug = text(item?.library_slug);
        const organizationId = text(item?.organization_id);
        if (!slug || !organizationId || seenLibraries.has(slug)
            || !(item?.actions || []).includes('read')) continue;
        seenLibraries.add(slug);
        if (!groups.has(organizationId)) {
            groups.set(organizationId, {
                id: organizationId,
                name: names.get(organizationId) || organizationId,
                libraries: [],
            });
        }
        const capabilities = graphLibraryCapabilities(permissions, organizations, slug);
        groups.get(organizationId).libraries.push({
            slug,
            name: text(item?.library_name || slug),
            insert: capabilities.insert,
            manage: capabilities.manage,
        });
    }
    return [...groups.values()];
}

export function resolveGraphScope(query, permissions, organizations = []) {
    const groups = graphScopeOptions(permissions, organizations);
    const requestedTab = text(query?.tab);
    const tab = GRAPH_TABS.has(requestedTab) ? requestedTab : 'entities';
    const requestedOrganizationId = text(query?.organization);
    let organization = groups.find((item) => item.id === requestedOrganizationId) || groups[0] || null;
    if (organization && MANAGEMENT_TABS.has(tab)) {
        const manageable = organization.libraries.filter((item) => item.manage);
        if (!manageable.length) {
            organization = groups.find((item) => item.libraries.some((library) => library.manage)) || organization;
        }
    }
    const allowed = organization?.libraries || [];
    const allowedSlugs = new Set(allowed.map((item) => item.slug));
    const requestedLibraries = text(query?.libraries)
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
    const selected = [];
    for (const slug of requestedLibraries) {
        if (selected.length >= 20 || selected.includes(slug) || !allowedSlugs.has(slug)) continue;
        selected.push(slug);
    }
    const eligible = MANAGEMENT_TABS.has(tab)
        ? allowed.filter((item) => item.manage)
        : allowed;
    let librarySlugs = selected.filter((slug) => (
        !MANAGEMENT_TABS.has(tab) || eligible.some((item) => item.slug === slug)
    ));
    if (!librarySlugs.length && eligible.length) librarySlugs = [eligible[0].slug];
    if (MANAGEMENT_TABS.has(tab)) librarySlugs = librarySlugs.slice(0, 1);
    const entityId = tab === 'entities' && validUuid(query?.entity) ? text(query.entity) : '';
    const relationId = tab === 'relations' && validUuid(query?.relation) ? text(query.relation) : '';
    return {
        tab,
        organizationId: organization?.id || '',
        librarySlugs,
        entityId,
        relationId,
    };
}

export function graphRouteQuery(scope) {
    const query = {
        tab: GRAPH_TABS.has(scope?.tab) ? scope.tab : 'entities',
    };
    if (scope?.organizationId) query.organization = text(scope.organizationId);
    if (Array.isArray(scope?.librarySlugs) && scope.librarySlugs.length) {
        query.libraries = scope.librarySlugs.slice(0, 20).map(text).join(',');
    }
    if (query.tab === 'entities' && validUuid(scope?.entityId)) query.entity = text(scope.entityId);
    if (query.tab === 'relations' && validUuid(scope?.relationId)) query.relation = text(scope.relationId);
    return query;
}

export function advanceGraphCursor(state, nextCursor) {
    const history = [...(state?.history || [])];
    if (!nextCursor) return { history, current: state?.current || null };
    return { history: [...history, state?.current || null], current: nextCursor };
}

export function retreatGraphCursor(state) {
    const history = [...(state?.history || [])];
    if (!history.length) return { history, current: state?.current || null };
    return { history, current: history.pop() || null };
}

export function graphFactLabel(value) {
    return FACT_LABELS[value] || '未知状态';
}

export function graphReviewLabel(value) {
    return REVIEW_LABELS[value] || '未知状态';
}

export function graphPublicationLabel(value) {
    return PUBLICATION_LABELS[value] || '未知状态';
}

export function graphActionLabel(value) {
    return ACTION_LABELS[value] || '未知操作';
}

function graphCompatibilityConflicts(error) {
    const detail = error?.body?.detail;
    if (detail?.code !== 'graph_catalog_scope_incompatible'
        || !Array.isArray(detail?.incompatibilities)) return [];
    const result = [];
    for (const item of detail.incompatibilities.slice(0, 20)) {
        const slug = text(item?.library_slug);
        if (!SAFE_LIBRARY_SLUG.test(slug) || !Array.isArray(item?.reason_codes)) continue;
        const reasons = item.reason_codes
            .filter((code) => typeof code === 'string' && SAFE_REASON.test(code))
            .slice(0, 13)
            .map((code) => CONFLICT_REASON_LABELS[code] || code);
        if (reasons.length) result.push({ librarySlug: slug, reasons });
    }
    return result;
}

export function graphErrorProjection(error) {
    const conflicts = graphCompatibilityConflicts(error);
    let kind = 'error';
    if (error?.status === 403) kind = 'forbidden';
    else if (error?.status === 404) kind = 'unavailable';
    else if (error?.status === 409) kind = 'conflict';
    else if (error?.status === 422) kind = 'invalid';
    return { kind, message: ERROR_MESSAGES[kind], conflicts };
}

function graphPageMatches(value, identity, contractVersion) {
    if (value?.contract_version !== contractVersion || !Array.isArray(value?.items)
        || value.items.length > 100) return false;
    const slugs = uniqueStrings(identity?.librarySlugs, 20);
    if (!slugs?.length) return false;
    const allowed = new Set(slugs);
    const ids = [];
    for (const item of value.items) {
        const id = text(item?.id);
        const slug = text(item?.library?.slug);
        if (!validUuid(id) || !allowed.has(slug)) return false;
        ids.push(id);
    }
    return new Set(ids).size === ids.length
        && (value.next_cursor === null || value.next_cursor === undefined
            || (typeof value.next_cursor === 'string' && value.next_cursor.length <= 4096));
}

export function graphEntityPageMatches(value, identity) {
    return graphPageMatches(value, identity, 'graph-catalog-entities-v1');
}

export function graphRelationPageMatches(value, identity) {
    return graphPageMatches(value, identity, 'graph-catalog-relations-v1');
}

export function graphEntityDetailMatches(value, identity) {
    return Boolean(
        value?.contract_version === 'graph-catalog-entity-detail-v1'
        && text(value?.entity?.id) === text(identity?.entityId)
        && text(value?.entity?.library?.slug) === text(identity?.librarySlug)
        && (!identity?.ontologyVersionId
            || text(value?.entity?.ontology_version_id) === text(identity.ontologyVersionId))
    );
}

export function graphRelationDetailMatches(value, identity) {
    return Boolean(
        value?.contract_version === 'graph-catalog-relation-detail-v1'
        && text(value?.relation?.id) === text(identity?.relationId)
        && text(value?.relation?.library?.slug) === text(identity?.librarySlug)
        && (!identity?.ontologyVersionId
            || text(value?.relation?.ontology_version_id) === text(identity.ontologyVersionId))
    );
}

export function graphEvidenceMatches(value, identity) {
    if (value?.contract_version !== 'catalog-evidence-v1'
        || text(value?.evidence_id) !== text(identity?.evidenceId)
        || text(value?.library_id) !== text(identity?.libraryId)
        || text(value?.document_id) !== text(identity?.documentId)
        || text(value?.document_revision_id) !== text(identity?.revisionId)) return false;
    if (!identity?.factId) return true;
    return Array.isArray(value?.fact_refs) && value.fact_refs.some((item) => (
        text(item?.fact_id) === text(identity.factId)
        && (!identity?.factKind || item?.item_kind === identity.factKind)
        && (!identity?.chunkId || text(item?.chunk_id) === text(identity.chunkId))
    ));
}

export function graphPropertyRows(properties) {
    if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return [];
    const rows = [];
    for (const [key, value] of Object.entries(properties).slice(0, 100)) {
        const label = text(key).slice(0, 128);
        if (!label) continue;
        let display = '';
        if (value === null) display = '空值';
        else if (['string', 'number', 'boolean'].includes(typeof value)) display = text(value).slice(0, 1000);
        else if (Array.isArray(value) && value.every((item) => (
            item === null || ['string', 'number', 'boolean'].includes(typeof item)
        ))) {
            display = value.slice(0, 20).map((item) => item === null ? '空值' : text(item)).join('、').slice(0, 1000);
        } else display = '结构化值';
        rows.push({ key: label, value: display });
    }
    return rows;
}

export function graphGovernanceContextMatches(value, identity) {
    if (value?.contract_version !== 'graph-governance-context-v1'
        || !validUuid(value?.library_id)
        || (identity?.libraryId && text(value?.library_id) !== text(identity.libraryId))
        || text(value?.library_slug) !== text(identity?.librarySlug)
        || !Array.isArray(value?.ontology_versions)
        || value.ontology_versions.length > 20) return false;
    const ontologyIds = [];
    for (const ontology of value.ontology_versions) {
        const ontologyId = text(ontology?.id);
        if (!validUuid(ontologyId) || !text(ontology?.version_key)
            || !Number.isInteger(ontology?.version_no) || ontology.version_no < 1
            || !Array.isArray(ontology?.entity_types) || ontology.entity_types.length > 100
            || !Array.isArray(ontology?.relation_types) || ontology.relation_types.length > 100) {
            return false;
        }
        ontologyIds.push(ontologyId);
        const typeIds = [];
        for (const type of [...ontology.entity_types, ...ontology.relation_types]) {
            if (!validUuid(type?.id) || !text(type?.key) || !text(type?.label)) return false;
            typeIds.push(text(type.id));
        }
        if (new Set(typeIds).size !== typeIds.length) return false;
        if (ontology.relation_types.some((type) => (
            !['directed', 'undirected'].includes(type?.direction)
            || !['auto_active', 'pending_review', 'manual_only'].includes(type?.default_review_policy)
            || typeof type?.requires_evidence !== 'boolean'
        ))) return false;
    }
    return new Set(ontologyIds).size === ontologyIds.length;
}

export function graphActionPageMatches(value, identity) {
    if (!value || !Array.isArray(value.items) || value.items.length > 100
        || !Number.isInteger(value.total) || value.total < value.items.length
        || value.limit !== identity?.limit || value.offset !== identity?.offset) return false;
    const ids = [];
    for (const action of value.items) {
        if (!validUuid(action?.id)
            || text(action?.library_id) !== text(identity?.libraryId)
            || !validUuid(action?.ontology_version_id)
            || !Object.hasOwn(ACTION_LABELS, action?.action_kind)) return false;
        ids.push(text(action.id));
    }
    return new Set(ids).size === ids.length;
}

export function graphActionSummary(action) {
    const payload = action?.payload || {};
    const fields = [];
    const add = (key, label) => {
        const value = payload[key];
        if (typeof value === 'string' && value) fields.push({ key, label, value: value.slice(0, 512) });
        else if (Number.isInteger(value) && value >= 0) fields.push({ key, label, value: String(value) });
    };
    switch (action?.action_kind) {
    case 'entity_create':
        add('canonical_name', '实体名称');
        add('entity_type_id', '实体类型');
        break;
    case 'relation_create':
        add('source_entity_id', '源实体');
        add('relation_type_id', '关系类型');
        add('target_entity_id', '目标实体');
        break;
    case 'entity_correct':
        add('canonical_name', '实体名称');
        break;
    case 'relation_correct':
        add('source_entity_id', '源实体');
        add('target_entity_id', '目标实体');
        break;
    case 'alias_add':
        add('alias', '别名');
        add('entity_id', '实体');
        break;
    case 'relation_review':
        add('decision', '审核决定');
        break;
    case 'entity_disable':
        add('related_relation_count', '关联关系数');
        break;
    case 'entity_merge':
        add('effect_count', '变更项数');
        add('conflict_count', '冲突数');
        break;
    default:
        break;
    }
    return { label: graphActionLabel(action?.action_kind), fields };
}

export function approvedUnboundGraphActions(actions, ontologyVersionId = '') {
    return (actions || []).filter((action) => (
        action?.status === 'approved'
        && !action?.planned_publication_id
        && validUuid(action?.id)
        && validUuid(action?.ontology_version_id)
        && (!ontologyVersionId || text(action.ontology_version_id) === text(ontologyVersionId))
    ));
}

export function graphActionSelection(actions, actionIds) {
    const ids = uniqueStrings(actionIds, 100);
    if (!ids?.length || ids.some((id) => !validUuid(id))) return null;
    const byId = new Map(approvedUnboundGraphActions(actions).map((action) => [text(action.id), action]));
    const selected = ids.map((id) => byId.get(id));
    if (selected.some((item) => !item)) return null;
    const ontologyIds = new Set(selected.map((item) => text(item.ontology_version_id)));
    if (ontologyIds.size !== 1) return null;
    return { actionIds: ids, ontologyVersionId: [...ontologyIds][0] };
}

export function graphPublicationPreviewMatches(value, identity) {
    return Boolean(
        value
        && validUuid(value.publication_id)
        && HASH_RE.test(text(value.manifest_hash))
        && HASH_RE.test(text(value.action_set_hash))
        && value.dry_run === true
        && Number.isInteger(value.entity_count) && value.entity_count >= 0
        && Number.isInteger(value.relation_count) && value.relation_count >= 0
        && text(value.parent_publication_id) === text(identity?.parentPublicationId)
        && (!identity?.publicationId || text(value.publication_id) === text(identity.publicationId))
        && (!identity?.manifestHash || value.manifest_hash === identity.manifestHash)
        && (!identity?.actionSetHash || value.action_set_hash === identity.actionSetHash)
    );
}

export function graphPublicationCommitMatches(value, preview) {
    return Boolean(
        value
        && validUuid(value.publication_id)
        && text(value.publication_id) === text(preview?.publication_id)
        && value.status === 'planned'
        && value.dry_run === false
        && Number.isInteger(value.entity_count) && value.entity_count >= 0
        && Number.isInteger(value.relation_count) && value.relation_count >= 0
        && value.manifest_hash === preview?.manifest_hash
        && value.action_set_hash === preview?.action_set_hash
        && text(value.parent_publication_id) === text(preview?.parent_publication_id)
    );
}

export function graphPublicationReadMatches(value, identity) {
    const statuses = new Set([
        'planned',
        'activating',
        'active',
        'degraded',
        'superseded',
        'cancelled',
        'failed',
    ]);
    const sourceModes = new Set([
        'initial_seed',
        'manual_plan',
        'rollback',
        'coordinated_purge',
    ]);
    const optionalIds = [
        value?.parent_publication_id,
        value?.rollback_target_publication_id,
        value?.superseded_by_publication_id,
    ];
    return Boolean(
        value
        && validUuid(value.id)
        && text(value.library_id) === text(identity?.libraryId)
        && text(value.ontology_version_id) === text(identity?.ontologyVersionId)
        && statuses.has(value.status)
        && sourceModes.has(value.source_mode)
        && typeof value.publication_enabled === 'boolean'
        && HASH_RE.test(text(value.manifest_hash))
        && Number.isInteger(value.entity_count) && value.entity_count >= 0
        && Number.isInteger(value.relation_count) && value.relation_count >= 0
        && optionalIds.every((item) => item === null || item === undefined || validUuid(item))
        && (!identity?.publicationId || text(value.id) === text(identity.publicationId))
        && (!identity?.status || value.status === identity.status)
        && (!identity?.manifestHash || value.manifest_hash === identity.manifestHash)
    );
}

export function graphPublicationListMatches(value, identity) {
    if (!value || !Array.isArray(value.items) || value.items.length > 500
        || !Number.isInteger(value.total) || value.total < value.items.length
        || value.page !== identity?.page || value.page_size !== identity?.pageSize
        || typeof value.publication_enabled !== 'boolean') return false;
    const ids = [];
    for (const item of value.items) {
        if (item?.publication_enabled !== value.publication_enabled
            || !graphPublicationReadMatches(item, identity)) return false;
        ids.push(text(item.id));
    }
    return new Set(ids).size === ids.length;
}

export function graphRollbackPreviewMatches(value, identity) {
    return Boolean(
        value
        && validUuid(value.id)
        && text(value.library_id) === text(identity?.libraryId)
        && text(value.ontology_version_id) === text(identity?.ontologyVersionId)
        && text(value.rollback_target_publication_id) === text(identity?.targetPublicationId)
        && value.source_mode === 'rollback'
        && value.status === 'planned'
        && value.dry_run === identity?.dryRun
        && HASH_RE.test(text(value.manifest_hash))
    );
}

export function createGraphIntentKey(intent, uuidFactory = null) {
    const prefix = text(intent).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').slice(0, 40)
        || 'graph-action';
    const factory = uuidFactory || (() => globalThis.crypto.randomUUID());
    return `${prefix}:${factory()}`.slice(0, 128);
}
