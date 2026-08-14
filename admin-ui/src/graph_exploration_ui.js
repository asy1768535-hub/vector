const UUID_RE = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const HASH_RE = /^[0-9a-f]{64}$/;
const TYPE_KEY_RE = /^[^\s]{1,128}$/;
const SOURCE_TYPES = new Set(['manual', 'imported', 'extracted']);
const RELATION_DIRECTIONS = new Set(['directed', 'undirected']);

const ERROR_MESSAGES = {
    disabled: '图谱探查功能暂未启用。',
    publication_unavailable: '当前实体没有可用的已发布图谱。',
    publication_changed: '图谱发布版本已变化，请重新选择实体后再探查。',
    relation_filter: '关系类型筛选不存在或已失效，请调整后重试。',
    timeout: '图谱探查超时，请缩小范围后重试。',
    not_found: '实体或知识库已不存在，请重新选择。',
    forbidden: '你没有读取当前知识库图谱的权限。',
    invalid: '探查条件不符合图谱约束，请检查后重试。',
    malformed: '服务返回的图谱数据不完整，请重新探查。',
    conflict: '图谱状态已变化，请重新加载后再探查。',
    error: '图谱探查服务暂时不可用，请稍后重试。',
};

function text(value) {
    return typeof value === 'string' ? value : '';
}

function validUuid(value) {
    return UUID_RE.test(text(value));
}

function validHash(value) {
    return HASH_RE.test(text(value));
}

function validType(value, includeDirection = false) {
    return Boolean(
        validUuid(value?.id)
        && TYPE_KEY_RE.test(text(value?.key))
        && text(value?.label).length >= 1
        && text(value?.label).length <= 255
        && (!includeDirection || RELATION_DIRECTIONS.has(value?.direction))
    );
}

function validConfidence(value) {
    return value === null || value === undefined
        || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1);
}

function validNullableInteger(value, minimum) {
    return value === null || value === undefined
        || (Number.isInteger(value) && value >= minimum);
}

function validEvidence(locator) {
    if (!validUuid(locator?.evidence_id)
        || !validUuid(locator?.document_id)
        || !validUuid(locator?.document_revision_id)
        || !(locator?.document_block_id === null || locator?.document_block_id === undefined
            || validUuid(locator.document_block_id))
        || !text(locator?.evidence_kind)
        || text(locator.evidence_kind).length > 32
        || !validNullableInteger(locator?.page_start, 1)
        || !validNullableInteger(locator?.page_end, 1)
        || !validNullableInteger(locator?.source_start, 0)
        || !validNullableInteger(locator?.source_end, 0)) return false;
    if (Number.isInteger(locator.page_start) && Number.isInteger(locator.page_end)
        && locator.page_end < locator.page_start) return false;
    return !(Number.isInteger(locator.source_start) && Number.isInteger(locator.source_end)
        && locator.source_end < locator.source_start);
}

function validEvidenceRows(rows) {
    return Array.isArray(rows) && rows.length <= 20 && rows.every(validEvidence);
}

function validLibrary(library) {
    return Boolean(
        validUuid(library?.id)
        && text(library?.slug).length >= 1
        && text(library?.slug).length <= 80
        && text(library?.name).length >= 1
        && text(library?.name).length <= 160
    );
}
function validSnapshotConfidence(value) {
    if (value === null || value === undefined) return true;
    if (typeof value === 'number') return validConfidence(value);
    if (typeof value !== 'string' || !value.trim()) return false;
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1;
}

function snapshotConfidence(value) {
    if (value === null || value === undefined) return null;
    const parsed = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1 ? parsed : null;
}

function validEvidenceIds(value) {
    return Array.isArray(value) && value.every(validUuid);
}

function sameUuidList(left, right) {
    return Array.isArray(left) && Array.isArray(right)
        && left.length === right.length
        && left.every((value, index) => text(value).toLowerCase() === text(right[index]).toLowerCase());
}

function validSnapshotBase(snapshot, item, identity, kind) {
    return Boolean(
        snapshot && typeof snapshot === 'object' && !Array.isArray(snapshot)
        && snapshot.manifest_version === 'v1'
        && snapshot.item_kind === kind
        && text(snapshot.library_id) === text(identity.libraryId)
        && text(snapshot.ontology_version_id) === text(identity.ontologyVersionId)
        && validHash(snapshot.properties_hash)
        && validSnapshotConfidence(snapshot.confidence)
        && SOURCE_TYPES.has(snapshot.source_type)
        && validEvidenceIds(snapshot.support_evidence_ids)
        && sameUuidList(snapshot.support_evidence_ids, item.support_evidence_ids)
    );
}

function validPublicationItem(item, identity, kind) {
    if (!validUuid(item?.id)
        || text(item?.publication_id) !== text(identity.publicationId)
        || item?.item_kind !== kind
        || item?.status !== 'active'
        || !validHash(item?.item_hash)
        || !validEvidenceIds(item?.support_evidence_ids)) return false;

    const snapshot = item.fact_snapshot;
    if (!validSnapshotBase(snapshot, item, identity, kind)) return false;
    if (kind === 'entity') {
        return text(item.entity_id) === text(snapshot.entity_id)
            && validUuid(item.entity_id)
            && validUuid(snapshot.entity_type_id)
            && text(snapshot.canonical_name).length >= 1
            && text(snapshot.canonical_name).length <= 512
            && text(snapshot.normalized_name).length >= 1
            && text(snapshot.normalized_name).length <= 512;
    }
    return text(item.relation_id) === text(snapshot.relation_id)
        && validUuid(item.relation_id)
        && validUuid(snapshot.relation_type_id)
        && validUuid(snapshot.source_entity_id)
        && validUuid(snapshot.target_entity_id);
}

export function graphPublicationReadMatches(value, identity) {
    return Boolean(
        validUuid(value?.id)
        && text(value.id) === text(identity?.publicationId)
        && validUuid(value?.library_id)
        && text(value.library_id) === text(identity?.libraryId)
        && validUuid(value?.ontology_version_id)
        && text(value.ontology_version_id) === text(identity?.ontologyVersionId)
        && value.status === 'active'
        && value.healthy === true
        && value.publication_enabled === true
        && value.manifest_version === 'v1'
        && validHash(value.manifest_hash)
        && Number.isInteger(value.entity_count) && value.entity_count >= 0
        && Number.isInteger(value.relation_count) && value.relation_count >= 0
        && typeof value.activated_at === 'string'
        && Number.isFinite(Date.parse(value.activated_at))
    );
}

export function graphPublicationItemPageMatches(value, identity, kind) {
    const expectedTotal = kind === 'entity'
        ? identity?.entityCount
        : identity?.relationCount;
    if (!['entity', 'relation'].includes(kind)
        || !Number.isInteger(identity?.page)
        || !Number.isInteger(identity?.pageSize)
        || identity.page < 1 || identity.pageSize < 1 || identity.pageSize > 500
        || !Number.isInteger(expectedTotal) || expectedTotal < 0
        || !Number.isInteger(value?.page) || value.page !== identity.page
        || !Number.isInteger(value?.page_size) || value.page_size !== identity.pageSize
        || !Number.isInteger(value?.total) || value.total !== expectedTotal
        || !Array.isArray(value?.items) || value.items.length > identity.pageSize) return false;
    const ids = new Set();
    for (const item of value.items) {
        if (ids.has(item?.id) || !validPublicationItem(item, identity, kind)) return false;
        ids.add(item.id);
    }
    return true;
}

function snapshotType(id, prefix) {
    const value = text(id);
    return { id: value, key: value, label: prefix };
}

export function staticPublicationPanorama(publication, entityItems, relationItems, library) {
    const identity = {
        publicationId: publication?.id,
        libraryId: publication?.library_id,
        ontologyVersionId: publication?.ontology_version_id,
    };
    if (!graphPublicationReadMatches(publication, identity)
        || !Array.isArray(entityItems) || !Array.isArray(relationItems)
        || entityItems.length !== publication.entity_count
        || relationItems.length !== publication.relation_count) return null;

    const entityIds = new Set();
    const nodes = entityItems.map((item) => {
        if (entityIds.has(item.entity_id) || !validPublicationItem(item, identity, 'entity')) return null;
        entityIds.add(item.entity_id);
        const snapshot = item.fact_snapshot;
        return {
            id: item.entity_id,
            canonical_name: snapshot.canonical_name,
            normalized_name: snapshot.normalized_name,
            entity_type: snapshotType(snapshot.entity_type_id, 'Entity'),
            library: library || { id: publication.library_id, slug: '' },
            ontology_version_id: publication.ontology_version_id,
            source_type: snapshot.source_type,
            confidence: snapshotConfidence(snapshot.confidence),
            item_hash: item.item_hash,
            evidence: item.support_evidence_ids.map((evidence_id) => ({ evidence_id })),
            publication_id: publication.id,
            manifest_hash: publication.manifest_hash,
            depth: 1,
        };
    });
    if (nodes.some((node) => !node)) return null;

    const relationIds = new Set();
    const relations = relationItems.map((item) => {
        if (relationIds.has(item.relation_id) || !validPublicationItem(item, identity, 'relation')) return null;
        relationIds.add(item.relation_id);
        const snapshot = item.fact_snapshot;
        if (!entityIds.has(snapshot.source_entity_id) || !entityIds.has(snapshot.target_entity_id)) return null;
        return {
            id: item.relation_id,
            source_entity_id: snapshot.source_entity_id,
            target_entity_id: snapshot.target_entity_id,
            relation_type: snapshotType(snapshot.relation_type_id, 'Relation'),
            library: library || { id: publication.library_id, slug: '' },
            ontology_version_id: publication.ontology_version_id,
            source_type: snapshot.source_type,
            confidence: snapshotConfidence(snapshot.confidence),
            item_hash: item.item_hash,
            evidence: item.support_evidence_ids.map((evidence_id) => ({ evidence_id })),
            publication_id: publication.id,
            manifest_hash: publication.manifest_hash,
            depth: 1,
        };
    });
    if (relations.some((relation) => !relation)) return null;

    const connectedIds = new Set(relations.flatMap((relation) => [
        relation.source_entity_id,
        relation.target_entity_id,
    ]));
    return {
        panorama: true,
        publication,
        nodes,
        relations,
        entity_count: nodes.length,
        relation_count: relations.length,
        isolated_count: nodes.filter((node) => !connectedIds.has(node.id)).length,
        evidence_count: [...nodes, ...relations].reduce((total, item) => total + item.evidence.length, 0),
        library_count: 1,
    };
}

export function explorationSeedSearchRowValid(seed) {
    const publicationValid = seed?.publication_state === 'staged'
        ? seed?.publication === null
        : Boolean(
            seed?.publication_state === 'published'
            && validUuid(seed?.publication?.id)
            && ['active', 'degraded'].includes(seed?.publication?.status)
            && validHash(seed?.publication?.item_hash)
        );
    return Boolean(
        validUuid(seed?.id)
        && validLibrary(seed?.library)
        && validUuid(seed?.ontology_version_id)
        && validType(seed?.entity_type)
        && text(seed?.canonical_name).length >= 1
        && text(seed?.canonical_name).length <= 512
        && text(seed?.normalized_name).length >= 1
        && text(seed?.normalized_name).length <= 512
        && publicationValid
    );
}

export function explorationSeedEligible(seed) {
    return Boolean(
        explorationSeedSearchRowValid(seed)
        && seed.publication_state === 'published'
        && seed.publication.status === 'active'
    );
}

export function explorationSeedKey(seed) {
    const libraryId = validUuid(seed?.library?.id) ? seed.library.id : '';
    const entityId = validUuid(seed?.id) ? seed.id : '';
    return libraryId && entityId ? `${libraryId}:${entityId}` : '';
}

function validPublication(publication, identity) {
    return Boolean(
        text(publication?.id) === text(identity?.publicationId)
        && text(publication?.ontology_version_id) === text(identity?.ontologyVersionId)
        && publication?.manifest_version === 'v1'
        && validHash(publication?.manifest_hash)
        && typeof publication?.activated_at === 'string'
        && Number.isFinite(Date.parse(publication.activated_at))
    );
}

function validNode(node, identity, seedEntityId) {
    return Boolean(
        validUuid(node?.id)
        && validHash(node?.item_hash)
        && validType(node?.entity_type)
        && text(node?.canonical_name).length >= 1
        && text(node?.canonical_name).length <= 512
        && text(node?.normalized_name).length >= 1
        && text(node?.normalized_name).length <= 512
        && SOURCE_TYPES.has(node?.source_type)
        && validConfidence(node?.confidence)
        && Number.isInteger(node?.depth)
        && node.depth >= 0
        && node.depth <= identity.maxHops
        && (node.id !== seedEntityId || node.depth === 0)
        && (node.id === seedEntityId || node.depth > 0)
        && validEvidenceRows(node?.evidence)
    );
}

function validRelation(relation, identity, nodeIds) {
    return Boolean(
        validUuid(relation?.id)
        && validHash(relation?.item_hash)
        && validType(relation?.relation_type, true)
        && validUuid(relation?.source_entity_id)
        && validUuid(relation?.target_entity_id)
        && nodeIds.has(relation.source_entity_id)
        && nodeIds.has(relation.target_entity_id)
        && SOURCE_TYPES.has(relation?.source_type)
        && validConfidence(relation?.confidence)
        && Number.isInteger(relation?.depth)
        && relation.depth >= 1
        && relation.depth <= identity.maxHops
        && validEvidenceRows(relation?.evidence)
    );
}

export function graphTraversalResponseMatches(value, identity) {
    const maxHops = identity?.maxHops;
    const maxNodes = identity?.maxNodes;
    const maxRelations = identity?.maxRelations;
    const seedEntityId = text(identity?.seedEntityId);
    if (value?.contract_version !== 'v1'
        || !validUuid(identity?.ontologyVersionId)
        || !validUuid(identity?.publicationId)
        || !validUuid(seedEntityId)
        || ![1, 2].includes(maxHops)
        || !Number.isInteger(maxNodes) || maxNodes < 1 || maxNodes > 100
        || !Number.isInteger(maxRelations) || maxRelations < 1 || maxRelations > 200
        || !validPublication(value?.publication, identity)
        || !Array.isArray(value?.seed_matches) || value.seed_matches.length !== 1
        || value.seed_matches[0]?.input_index !== 0
        || text(value.seed_matches[0]?.entity_id) !== seedEntityId
        || !Array.isArray(value?.nodes) || value.nodes.length < 1 || value.nodes.length > maxNodes
        || !Array.isArray(value?.relations) || value.relations.length > maxRelations) return false;

    const nodeIds = new Set();
    for (const node of value.nodes) {
        if (!validNode(node, identity, seedEntityId) || nodeIds.has(node.id)) return false;
        nodeIds.add(node.id);
    }
    if (!nodeIds.has(seedEntityId)) return false;

    const relationIds = new Set();
    for (const relation of value.relations) {
        if (!validRelation(relation, identity, nodeIds) || relationIds.has(relation.id)) return false;
        relationIds.add(relation.id);
    }

    const evidenceCount = [...value.nodes, ...value.relations]
        .reduce((total, item) => total + item.evidence.length, 0);
    const counts = value?.counts;
    if (counts?.seeds !== 1
        || counts?.nodes !== value.nodes.length
        || counts?.relations !== value.relations.length
        || counts?.evidence_locators !== evidenceCount) return false;
    return Boolean(
        value?.truncated
        && typeof value.truncated.nodes === 'boolean'
        && typeof value.truncated.relations === 'boolean'
        && typeof value.truncated.evidence === 'boolean'
    );
}

function errorCode(error) {
    return typeof error?.body?.detail === 'string' ? error.body.detail : '';
}

export function graphExplorationErrorProjection(error) {
    const code = errorCode(error);
    let kind = 'error';
    if (error?.status === 403) kind = 'forbidden';
    else if (code === 'publication_changed') kind = 'publication_changed';
    else if (code === 'graph_retrieval_disabled') kind = 'disabled';
    else if (code === 'graph_publication_unavailable'
        || code === 'graph_publication_invariant_failed') kind = 'publication_unavailable';
    else if (code === 'relation_type_not_found') kind = 'relation_filter';
    else if (code === 'graph_retrieval_timeout' || error?.status === 504) kind = 'timeout';
    else if (error?.status === 404 || code === 'seed_not_found'
        || code === 'library_not_found') kind = 'not_found';
    else if (error?.status === 409) kind = 'conflict';
    else if (error?.status === 422 || code === 'graph_retrieval_invalid_request'
        || code === 'graph_retrieval_limit_exceeded') kind = 'invalid';
    return { kind, message: ERROR_MESSAGES[kind] };
}

export function malformedGraphExplorationError() {
    return { kind: 'malformed', message: ERROR_MESSAGES.malformed };
}

export function graphTraversalSummary(value) {
    const truncationLabels = [];
    if (value?.truncated?.nodes) truncationLabels.push('实体');
    if (value?.truncated?.relations) truncationLabels.push('关系');
    if (value?.truncated?.evidence) truncationLabels.push('证据');
    return {
        nodes: Number.isInteger(value?.counts?.nodes)
            ? value.counts.nodes
            : Number.isInteger(value?.entity_count)
                ? value.entity_count
                : Array.isArray(value?.nodes) ? value.nodes.length : 0,
        relations: Number.isInteger(value?.counts?.relations)
            ? value.counts.relations
            : Number.isInteger(value?.relation_count)
                ? value.relation_count
                : Array.isArray(value?.relations) ? value.relations.length : 0,
        evidence: Number.isInteger(value?.counts?.evidence_locators)
            ? value.counts.evidence_locators
            : Number.isInteger(value?.evidence_count) ? value.evidence_count : 0,
        truncationLabels,
    };
}
