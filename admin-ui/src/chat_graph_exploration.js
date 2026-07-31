export const CHAT_GRAPH_EXPANSION_NODES = 30;
export const CHAT_GRAPH_EXPANSION_RELATIONS = 50;
export const CHAT_GRAPH_TOTAL_NODES = 80;
export const CHAT_GRAPH_TOTAL_RELATIONS = 160;

const RELATION_DISPLAY_LABELS = Object.freeze({
    related_to: '关联',
    relates_to: '关联',
    belongs_to: '属于',
    part_of: '属于',
    has_part: '包含',
    contains: '包含',
    requires: '需要',
    depends_on: '依赖',
    references: '引用',
    referenced_by: '被引用',
    manages: '管理',
    managed_by: '由其管理',
    responsible_for: '负责',
    approves: '审批',
    approved_by: '由其审批',
    implements: '执行',
    implemented_by: '由其执行',
    supervises: '监督',
    applies_to: '适用于',
    produces: '产生',
    precedes: '先于',
    follows: '后于',
});

function array(value) {
    return Array.isArray(value) ? value : [];
}

export function chatGraphRelationLabel(relation) {
    const relationType = relation?.relation_type || {};
    const label = String(relationType.label || '').trim();
    if (/\p{Script=Han}/u.test(label)) return label;
    const key = String(relationType.key || label)
        .trim()
        .replace(/[\s-]+/g, '_')
        .toLowerCase();
    return RELATION_DISPLAY_LABELS[key] || '关联';
}

function evidenceCount(graph) {
    return [...array(graph?.nodes), ...array(graph?.relations)]
        .reduce((total, item) => total + array(item?.evidence).length, 0);
}

function graphCounts(graph, seedCount) {
    return {
        seeds: seedCount,
        nodes: array(graph?.nodes).length,
        relations: array(graph?.relations).length,
        evidence_locators: evidenceCount(graph),
    };
}

export function prepareChatGraph(graph) {
    if (!graph) return null;
    const seedIds = new Set(array(graph.seed_matches).map((item) => item?.entity_id).filter(Boolean));
    const nodes = array(graph.nodes).map((node) => ({
        ...node,
        display_depth: Number.isInteger(node.display_depth)
            ? node.display_depth
            : Number.isInteger(node.depth) ? node.depth : 0,
        citation_seed: seedIds.has(node.id),
    }));
    const prepared = { ...graph, nodes, relations: [...array(graph.relations)] };
    return { ...prepared, counts: graphCounts(prepared, seedIds.size) };
}

export function chatGraphExpansionRequest(graph, entityId) {
    const ontologyVersionId = graph?.publication?.ontology_version_id;
    const publicationId = graph?.publication?.id;
    if (!ontologyVersionId || !publicationId || !entityId) return null;
    const request = {
        ontology_version_id: ontologyVersionId,
        expected_publication_id: publicationId,
        seeds: [{ entity_id: entityId }],
        direction: 'both',
        relation_type_keys: [],
        max_hops: 1,
        max_nodes: CHAT_GRAPH_EXPANSION_NODES,
        max_relations: CHAT_GRAPH_EXPANSION_RELATIONS,
        include_evidence_locators: true,
    };
    return {
        request,
        identity: {
            ontologyVersionId,
            publicationId,
            seedEntityId: entityId,
            maxHops: request.max_hops,
            maxNodes: request.max_nodes,
            maxRelations: request.max_relations,
        },
    };
}

export function mergeChatGraph(current, incoming, pivotEntityId, limits = {}) {
    const maxNodes = Math.max(1, limits.maxNodes || CHAT_GRAPH_TOTAL_NODES);
    const maxRelations = Math.max(1, limits.maxRelations || CHAT_GRAPH_TOTAL_RELATIONS);
    const base = prepareChatGraph(current);
    if (!base) return { graph: null, addedNodeIds: [], addedRelationIds: [], limitReached: false };

    const pivot = base.nodes.find((node) => node.id === pivotEntityId);
    const pivotDepth = Number.isInteger(pivot?.display_depth) ? pivot.display_depth : 0;
    const nodeMap = new Map(base.nodes.map((node) => [node.id, node]));
    const addedNodeIds = [];
    let nodeLimitReached = false;
    for (const node of array(incoming?.nodes)) {
        const existing = nodeMap.get(node.id);
        if (existing) {
            const candidateDepth = node.id === pivotEntityId ? pivotDepth : pivotDepth + 1;
            nodeMap.set(node.id, {
                ...existing,
                display_depth: Math.min(existing.display_depth, candidateDepth),
            });
            continue;
        }
        if (nodeMap.size >= maxNodes) {
            nodeLimitReached = true;
            continue;
        }
        nodeMap.set(node.id, {
            ...node,
            display_depth: pivotDepth + 1,
            citation_seed: false,
        });
        addedNodeIds.push(node.id);
    }

    const relationMap = new Map(base.relations.map((relation) => [relation.id, relation]));
    const addedRelationIds = [];
    let relationLimitReached = false;
    for (const relation of array(incoming?.relations)) {
        if (relationMap.has(relation.id)) continue;
        if (!nodeMap.has(relation.source_entity_id) || !nodeMap.has(relation.target_entity_id)) continue;
        if (relationMap.size >= maxRelations) {
            relationLimitReached = true;
            continue;
        }
        relationMap.set(relation.id, relation);
        addedRelationIds.push(relation.id);
    }

    const merged = {
        ...base,
        nodes: [...nodeMap.values()],
        relations: [...relationMap.values()],
        truncated: {
            nodes: Boolean(base.truncated?.nodes || incoming?.truncated?.nodes || nodeLimitReached),
            relations: Boolean(base.truncated?.relations || incoming?.truncated?.relations || relationLimitReached),
            evidence: Boolean(base.truncated?.evidence || incoming?.truncated?.evidence),
        },
    };
    merged.counts = graphCounts(merged, array(base.seed_matches).length);
    return {
        graph: merged,
        addedNodeIds,
        addedRelationIds,
        limitReached: nodeLimitReached || relationLimitReached,
    };
}

export function chatGraphDegrees(graph) {
    const degrees = new Map(array(graph?.nodes).map((node) => [node.id, 0]));
    for (const relation of array(graph?.relations)) {
        if (degrees.has(relation.source_entity_id)) {
            degrees.set(relation.source_entity_id, degrees.get(relation.source_entity_id) + 1);
        }
        if (degrees.has(relation.target_entity_id)) {
            degrees.set(relation.target_entity_id, degrees.get(relation.target_entity_id) + 1);
        }
    }
    return degrees;
}

function compareFocusCandidates(left, right, seedIds, degrees) {
    return Number(seedIds.has(right.id)) - Number(seedIds.has(left.id))
        || (degrees.get(right.id) || 0) - (degrees.get(left.id) || 0)
        || String(left.canonical_name || '').localeCompare(String(right.canonical_name || ''), 'zh-CN')
        || String(left.id).localeCompare(String(right.id));
}

export function projectChatGraphFocus(graph, requestedFocusId = '') {
    const nodes = array(graph?.nodes);
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const validRelations = array(graph?.relations).filter((relation) => (
        nodeById.has(relation.source_entity_id) && nodeById.has(relation.target_entity_id)
    ));
    if (!nodes.length) {
        return {
            graph: { ...graph, nodes: [], relations: [] },
            focusId: '',
            hiddenNodeCount: 0,
            hiddenComponentCount: 0,
            pageIndex: 0,
            pageCount: 0,
            pageFocusIds: [],
        };
    }

    const adjacency = new Map(nodes.map((node) => [node.id, new Set()]));
    for (const relation of validRelations) {
        adjacency.get(relation.source_entity_id).add(relation.target_entity_id);
        adjacency.get(relation.target_entity_id).add(relation.source_entity_id);
    }
    const seedIds = new Set(array(graph?.seed_matches).map((item) => item?.entity_id).filter(Boolean));
    for (const node of nodes) {
        if (node.citation_seed || node.depth === 0) seedIds.add(node.id);
    }
    const degrees = chatGraphDegrees({ nodes, relations: validRelations });
    const collectComponent = (startId, visited) => {
        const component = new Set([startId]);
        const queue = [startId];
        visited.add(startId);
        while (queue.length) {
            const current = queue.shift();
            for (const neighborId of adjacency.get(current) || []) {
                if (visited.has(neighborId)) continue;
                visited.add(neighborId);
                component.add(neighborId);
                queue.push(neighborId);
            }
        }
        return component;
    };

    const visited = new Set();
    const components = [];
    for (const node of nodes) {
        if (visited.has(node.id)) continue;
        const ids = collectComponent(node.id, visited);
        const focus = [...ids]
            .map((id) => nodeById.get(id))
            .sort((left, right) => compareFocusCandidates(left, right, seedIds, degrees))[0];
        components.push({ ids, focus });
    }
    components.sort((left, right) => compareFocusCandidates(
        left.focus,
        right.focus,
        seedIds,
        degrees,
    ));
    const requestedPageIndex = nodeById.has(requestedFocusId)
        ? components.findIndex((component) => component.ids.has(requestedFocusId))
        : -1;
    const pageIndex = requestedPageIndex >= 0 ? requestedPageIndex : 0;
    const visibleIds = components[pageIndex].ids;
    const focusId = visibleIds.has(requestedFocusId)
        ? requestedFocusId
        : components[pageIndex].focus.id;
    const visibleNodes = nodes.filter((node) => visibleIds.has(node.id));
    const visibleRelations = validRelations.filter((relation) => (
        visibleIds.has(relation.source_entity_id) && visibleIds.has(relation.target_entity_id)
    ));
    return {
        graph: { ...graph, nodes: visibleNodes, relations: visibleRelations },
        focusId,
        hiddenNodeCount: nodes.length - visibleNodes.length,
        hiddenComponentCount: components.length - 1,
        pageIndex,
        pageCount: components.length,
        pageFocusIds: components.map((component) => component.focus.id),
    };
}
