import { chatGraphRelationLabel } from './chat_graph_exploration.js';

function text(value) {
    return typeof value === 'string' ? value : '';
}

function compareNodes(left, right) {
    return text(left.entity_type?.label).localeCompare(text(right.entity_type?.label), 'zh-CN')
        || text(left.canonical_name).localeCompare(text(right.canonical_name), 'zh-CN')
        || text(left.id).localeCompare(text(right.id));
}

function stableUnit(value, salt = 0) {
    let hash = (2166136261 ^ salt) >>> 0;
    for (const character of String(value)) {
        hash ^= character.codePointAt(0);
        hash = Math.imul(hash, 16777619) >>> 0;
    }
    hash ^= hash >>> 16;
    hash = Math.imul(hash, 0x7FEB352D) >>> 0;
    hash ^= hash >>> 15;
    hash = Math.imul(hash, 0x846CA68B) >>> 0;
    hash ^= hash >>> 16;
    return hash / 0xFFFFFFFF;
}

export function layoutCitationGraph(value, viewportWidth, viewportHeight, requestedFocusId = '') {
    const sourceNodes = [...(value?.nodes || [])].sort(compareNodes);
    const relations = [...(value?.relations || [])].sort((left, right) => (
        text(left.id).localeCompare(text(right.id))
    ));
    const focus = sourceNodes.find((node) => node.id === requestedFocusId) || sourceNodes[0];
    const adjacency = new Map(sourceNodes.map((node) => [node.id, new Set()]));
    for (const relation of relations) {
        if (!adjacency.has(relation.source_entity_id) || !adjacency.has(relation.target_entity_id)) continue;
        adjacency.get(relation.source_entity_id).add(relation.target_entity_id);
        adjacency.get(relation.target_entity_id).add(relation.source_entity_id);
    }
    const distances = new Map(focus ? [[focus.id, 0]] : []);
    const queue = focus ? [focus.id] : [];
    while (queue.length) {
        const current = queue.shift();
        for (const neighborId of adjacency.get(current) || []) {
            if (distances.has(neighborId)) continue;
            distances.set(neighborId, distances.get(current) + 1);
            queue.push(neighborId);
        }
    }
    const neighborsByDepth = new Map();
    for (const node of sourceNodes) {
        if (node.id === focus?.id) continue;
        const depth = distances.get(node.id) || 1;
        if (!neighborsByDepth.has(depth)) neighborsByDepth.set(depth, []);
        neighborsByDepth.get(depth).push(node);
    }

    const width = Math.max(320, Number(viewportWidth) || 320);
    const height = Math.max(320, Number(viewportHeight) || 320);
    const centerX = width / 2;
    const centerY = height / 2;
    const neighborRadius = Math.max(142, Math.min(width, height) * 0.29);

    const toNode = (node, position, role) => ({
        id: node.id,
        depth: node.depth,
        label: text(node.canonical_name),
        entityTypeKey: text(node.entity_type?.key),
        entityTypeLabel: text(node.entity_type?.label) || text(node.entity_type?.key) || 'Entity',
        role,
        focusDepth: distances.get(node.id) || 0,
        x: position.x,
        y: position.y,
        size: role === 'focus' ? 30 : 16,
    });
    const nodes = [
        ...(focus ? [toNode(focus, { x: centerX, y: centerY }, 'focus')] : []),
        ...[...neighborsByDepth.entries()].flatMap(([depth, depthNodes]) => {
            const baseRadius = neighborRadius
                + ((depth - 1) * 118)
                + (Math.max(0, depthNodes.length - 9) * 5);
            const angleStep = (Math.PI * 2) / Math.max(1, depthNodes.length);
            return depthNodes.map((node, index) => {
                const radiusJitter = 0.82 + (stableUnit(node.id, depth) * 0.34);
                const angleJitter = (stableUnit(node.id, depth + 97) - 0.5)
                    * Math.min(0.32, angleStep * 0.45);
                const angle = (-Math.PI / 2.3) + (depth * 0.21) + (angleStep * index) + angleJitter;
                return toNode(node, {
                    x: centerX + (Math.cos(angle) * baseRadius * radiusJitter),
                    y: centerY + (Math.sin(angle) * baseRadius * radiusJitter),
                }, 'neighbor');
            });
        }),
    ];
    const nodeIds = new Set(nodes.map((node) => node.id));
    const resultRelations = relations.flatMap((relation) => {
        if (!nodeIds.has(relation.source_entity_id) || !nodeIds.has(relation.target_entity_id)) return [];
        return [{
            id: relation.id,
            sourceId: relation.source_entity_id,
            targetId: relation.target_entity_id,
            direction: relation.relation_type?.direction,
            label: chatGraphRelationLabel(relation),
        }];
    });
    return { width, height, nodes, relations: resultRelations };
}
