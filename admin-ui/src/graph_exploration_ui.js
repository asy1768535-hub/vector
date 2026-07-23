const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
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

export function selectExplorationSeed(current, seed, limit = 4) {
    const selected = Array.isArray(current) ? [...current] : [];
    const key = explorationSeedKey(seed);
    if (!key || !explorationSeedEligible(seed)
        || selected.length >= Math.min(4, Math.max(1, limit))
        || selected.some((item) => explorationSeedKey(item) === key)) return selected;
    selected.push(seed);
    return selected;
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
        nodes: Number.isInteger(value?.counts?.nodes) ? value.counts.nodes : 0,
        relations: Number.isInteger(value?.counts?.relations) ? value.counts.relations : 0,
        evidence: Number.isInteger(value?.counts?.evidence_locators)
            ? value.counts.evidence_locators
            : 0,
        truncationLabels,
    };
}

function sortedNodes(value) {
    return [...(value?.nodes || [])].sort((left, right) => (
        left.depth - right.depth
        || text(left.normalized_name).localeCompare(text(right.normalized_name), 'zh-CN')
        || text(left.id).localeCompare(text(right.id))
    ));
}

export function layoutGraphRadially(value, width, height) {
    const safeWidth = Math.max(240, Number(width) || 240);
    const safeHeight = Math.max(220, Number(height) || 220);
    const centerX = safeWidth / 2;
    const centerY = safeHeight / 2;
    const ringBase = Math.max(54, Math.min(safeWidth, safeHeight));
    const radii = { 0: 0, 1: ringBase * 0.24, 2: ringBase * 0.41 };
    const resultNodes = [];
    const byId = new Map();
    const nodes = sortedNodes(value);
    for (const depth of [0, 1, 2]) {
        const ring = nodes.filter((item) => item.depth === depth);
        ring.forEach((item, index) => {
            const angle = depth === 0 ? 0 : (-Math.PI / 2) + ((Math.PI * 2 * index) / ring.length);
            const layoutNode = {
                id: item.id,
                depth,
                label: item.canonical_name,
                x: centerX + Math.cos(angle) * radii[depth],
                y: centerY + Math.sin(angle) * radii[depth],
                radius: depth === 0 ? 20 : 17,
            };
            resultNodes.push(layoutNode);
            byId.set(item.id, layoutNode);
        });
    }
    const relations = [...(value?.relations || [])]
        .sort((left, right) => text(left.id).localeCompare(text(right.id)))
        .map((relation) => {
            const source = byId.get(relation.source_entity_id);
            const target = byId.get(relation.target_entity_id);
            return {
                id: relation.id,
                direction: relation.relation_type.direction,
                label: relation.relation_type.label,
                x1: source.x,
                y1: source.y,
                x2: target.x,
                y2: target.y,
            };
        });
    return { width: safeWidth, height: safeHeight, nodes: resultNodes, relations };
}

function pointSegmentDistance(x, y, line) {
    const dx = line.x2 - line.x1;
    const dy = line.y2 - line.y1;
    if (dx === 0 && dy === 0) return Math.hypot(x - line.x1, y - line.y1);
    const ratio = Math.max(0, Math.min(1, (
        ((x - line.x1) * dx) + ((y - line.y1) * dy)
    ) / ((dx * dx) + (dy * dy))));
    return Math.hypot(x - (line.x1 + ratio * dx), y - (line.y1 + ratio * dy));
}

export function hitTestGraphLayout(layout, x, y) {
    for (const node of layout?.nodes || []) {
        if (Math.hypot(x - node.x, y - node.y) <= node.radius + 5) {
            return { kind: 'node', id: node.id };
        }
    }
    for (const relation of layout?.relations || []) {
        if (pointSegmentDistance(x, y, relation) <= 7) {
            return { kind: 'relation', id: relation.id };
        }
    }
    return null;
}
