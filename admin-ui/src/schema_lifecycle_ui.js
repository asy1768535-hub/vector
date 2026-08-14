const TABS = new Set(['overview', 'entities', 'relations', 'attributes', 'constraints', 'impact']);
const HASH_RE = /^[0-9a-f]{64}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const VERSION_STATUSES = new Set(['draft', 'active', 'disabled']);
const ITEM_STATUSES = new Set(['draft', 'active', 'disabled']);
const ISSUE_KINDS = new Set([
    'ontology_version', 'entity_type', 'relation_type', 'attribute', 'constraint',
]);

export function normalizeSchemaRoute(query = {}) {
    const librarySlug = typeof query.library === 'string' ? query.library.trim() : '';
    const versionId = typeof query.version === 'string' ? query.version.trim() : '';
    const tab = TABS.has(query.tab) ? query.tab : 'overview';
    return { librarySlug, versionId, tab };
}

export function schemaRouteQuery(scope = {}) {
    const query = {};
    if (scope.librarySlug) query.library = scope.librarySlug;
    if (scope.versionId) query.version = scope.versionId;
    query.tab = TABS.has(scope.tab) ? scope.tab : 'overview';
    return query;
}

function validVersion(value, identity) {
    return Boolean(value && typeof value === 'object'
        && UUID_RE.test(String(value.id || ''))
        && (!identity.versionId || String(value.id) === String(identity.versionId))
        && UUID_RE.test(String(value.library_id || ''))
        && String(value.library_slug || '') === String(identity.librarySlug || '')
        && (!identity.libraryId || String(value.library_id || '') === String(identity.libraryId))
        && HASH_RE.test(String(value.state_hash || ''))
        && typeof value.version_key === 'string' && value.version_key.length > 0
        && Number.isInteger(value.version_no) && value.version_no > 0
        && VERSION_STATUSES.has(value.status)
        && ['entity_type_count', 'relation_type_count', 'attribute_count', 'constraint_count']
            .every((key) => Number.isInteger(value[key]) && value[key] >= 0));
}

export function schemaVersionListMatches(value, identity) {
    if (!value || typeof value !== 'object' || !Array.isArray(value.versions)) return false;
    if (String(value.library_slug || '') !== String(identity.librarySlug || '')) return false;
    if (identity.libraryId && String(value.library_id || '') !== String(identity.libraryId)) return false;
    return value.versions.length <= 100 && value.versions.every((item) => validVersion(item, {
        libraryId: value.library_id,
        librarySlug: value.library_slug,
        versionId: item?.id,
    }));
}

export function schemaVersionDetailMatches(value, identity) {
    if (!validVersion(value, identity)) return false;
    const collections = [
        ['entity_types', 500, 'entity_type_count'],
        ['relation_types', 500, 'relation_type_count'],
        ['attributes', 1000, 'attribute_count'],
        ['constraints', 1000, 'constraint_count'],
    ];
    return collections.every(([key, limit, countKey]) => (
        Array.isArray(value[key])
        && value[key].length <= limit
        && value[key].length === value[countKey]
        && value[key].every((item) => item && typeof item === 'object'
            && UUID_RE.test(String(item.id || ''))
            && String(item.library_id || '') === String(value.library_id)
            && String(item.ontology_version_id || '') === String(value.id)
            && ITEM_STATUSES.has(item.status)
            && HASH_RE.test(String(item.state_hash || '')))
    ));
}

export function schemaVersionDeletionMatches(value, identity) {
    return Boolean(value && typeof value === 'object'
        && UUID_RE.test(String(value.action_id || ''))
        && typeof value.reused === 'boolean'
        && String(value.library_id || '') === String(identity.libraryId || '')
        && String(value.ontology_version_id || '') === String(identity.versionId || '')
        && value.status === 'deleted');
}

function validDiffGroup(value) {
    return Boolean(value && ['added', 'removed', 'changed'].every((key) => (
        Array.isArray(value[key]) && value[key].length <= 500
        && value[key].every((item) => typeof item === 'string')
    )));
}

function validReferences(value) {
    return Boolean(value
        && Number.isInteger(value.source_version_count) && value.source_version_count >= 0
        && Number.isInteger(value.draft_version_count) && value.draft_version_count >= 0
        && ['source_current_ids', 'draft_current_ids'].every((key) => (
            Array.isArray(value[key]) && value[key].length <= 20
            && value[key].every((item) => UUID_RE.test(String(item || '')))
        )));
}

export function schemaImpactMatches(value, identity) {
    return Boolean(value && typeof value === 'object'
        && String(value.library_id || '') === String(identity.libraryId || '')
        && String(value.ontology_version_id || '') === String(identity.versionId || '')
        && String(value.version_state_hash || '') === String(identity.stateHash || '')
        && ['entity_types', 'relation_types', 'attributes', 'constraints']
            .every((key) => validDiffGroup(value[key]))
        && validReferences(value.extraction_jobs)
        && validReferences(value.publications)
        && Array.isArray(value.compatibility) && value.compatibility.length <= 20
        && value.compatibility.every((item) => item && typeof item === 'object'
            && UUID_RE.test(String(item.library_id || ''))
            && typeof item.library_slug === 'string' && item.library_slug.length > 0
            && ['match', 'mismatch', 'unavailable'].includes(item.result))
        && value.historical_rows_migrated === false
        && value.retrieval_scope_changed === false);
}

export function schemaValidationMatches(value, identity) {
    return Boolean(value && typeof value === 'object'
        && String(value.library_id || '') === String(identity.libraryId || '')
        && String(value.ontology_version_id || '') === String(identity.versionId || '')
        && String(value.version_state_hash || '') === String(identity.stateHash || '')
        && typeof value.valid === 'boolean'
        && Array.isArray(value.issues) && value.issues.length <= 100
        && value.issues.every((item) => item && typeof item === 'object'
            && /^[a-z][a-z0-9_]{0,63}$/.test(String(item.code || ''))
            && ISSUE_KINDS.has(item.item_kind)
            && (item.item_id == null || UUID_RE.test(String(item.item_id)))));
}

export function schemaCanEdit(version) {
    return version?.status === 'draft';
}

export function schemaStatusLabel(status) {
    return ({ draft: '草稿', active: '已激活', disabled: '已停用' })[status] || '未知状态';
}

export function schemaStatusTag(status) {
    return ({ draft: 'warning', active: 'success', disabled: 'info' })[status] || 'info';
}

const ISSUE_LABELS = {
    ontology_version_not_draft: '当前版本不是可编辑草稿',
    item_scope_invalid: 'Schema 项不属于当前知识库版本',
    item_status_invalid: 'Schema 项状态无效',
    entity_type_required: '至少需要一个启用的实体类型',
    type_key_invalid: '类型标识格式无效',
    type_label_required: '类型名称不能为空',
    type_key_duplicate: '类型标识重复',
    properties_schema_invalid: '属性结构定义无效',
    relation_direction_invalid: '关系方向无效',
    relation_review_policy_invalid: '关系审核策略无效',
    attribute_key_duplicate: '属性标识重复',
    attribute_owner_kind_invalid: '属性所属类型无效',
    attribute_owner_missing: '属性所属类型不可用',
    attribute_key_invalid: '属性标识格式无效',
    attribute_value_type_invalid: '属性值类型无效',
    attribute_enum_values_required: '枚举属性必须设置可选值',
    attribute_enum_values_forbidden: '非枚举属性不能设置枚举值',
    attribute_validation_schema_invalid: '属性校验规则无效',
    constraint_duplicate: '关系约束重复',
    constraint_relation_missing: '约束引用的关系类型不可用',
    constraint_source_missing: '约束引用的源实体类型不可用',
    constraint_target_missing: '约束引用的目标实体类型不可用',
    constraint_cardinality_invalid: '关系基数无效',
};

export function schemaIssueLabel(issue) {
    return ISSUE_LABELS[issue?.code] || 'Schema 校验未通过';
}

export function schemaErrorKind(error) {
    if (error?.status === 403) return 'forbidden';
    if (error?.status === 409
        && error?.body?.detail === 'schema_lifecycle_dependency_conflict') return 'dependency';
    if (error?.status === 409) return 'conflict';
    if (error?.status === 422) return 'invalid';
    if (error?.status === 404 || error?.status === 503) return 'unavailable';
    return 'failed';
}

export function schemaErrorMessage(kind) {
    return ({
        forbidden: '当前账号没有管理这个知识库 Schema 的权限',
        dependency: '该 Schema 仍被图谱数据、任务或其他版本引用，不能删除或停用',
        conflict: 'Schema 已发生变化，请检查最新版本后重新操作',
        invalid: '提交内容不符合 Schema 约束',
        unavailable: 'Schema 生命周期功能当前不可用',
        malformed: '服务器返回的 Schema 数据不完整',
        failed: '操作未完成，请稍后重试',
    })[kind] || '操作未完成，请稍后重试';
}

export function schemaIntentKey(prefix = 'schema') {
    const identity = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${identity}`.slice(0, 128);
}

export function schemaOwnerOptions(detail) {
    if (!detail) return [];
    return [
        ...(detail.entity_types || []).filter((item) => item.status === 'draft').map((item) => ({
            kind: 'entity_type', id: String(item.id), label: `实体 · ${item.label}`,
        })),
        ...(detail.relation_types || []).filter((item) => item.status === 'draft').map((item) => ({
            kind: 'relation_type', id: String(item.id), label: `关系 · ${item.label}`,
        })),
    ];
}
