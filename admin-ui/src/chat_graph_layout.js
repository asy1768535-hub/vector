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

function layoutLibraryPanorama(sourceNodes, relations, width, height) {
    const entities = sourceNodes;
    const entityIds = new Set(entities.map((node) => node.id));
    const connectedIds = new Set();
    for (const relation of relations) {
        if (!entityIds.has(relation.source_entity_id) || !entityIds.has(relation.target_entity_id)) continue;
        connectedIds.add(relation.source_entity_id);
        connectedIds.add(relation.target_entity_id);
    }
    const connectedEntities = entities.filter((node) => connectedIds.has(node.id));
    const isolatedEntities = entities.filter((node) => !connectedIds.has(node.id)).sort(compareNodes);
    const clusterWidth = isolatedEntities.length ? width * 0.72 : width;
    const libraryKey = (node) => text(node.library?.id) || text(node.library?.slug) || 'default';
    const libraryKeys = [...new Set(entities.map(libraryKey))];
    const aspect = clusterWidth / height;
    const columns = Math.max(1, Math.ceil(Math.sqrt(libraryKeys.length * aspect)));
    const rows = Math.max(1, Math.ceil(libraryKeys.length / columns));
    const cellWidth = clusterWidth / columns;
    const cellHeight = height / rows;
    const entitiesByLibrary = new Map(libraryKeys.map((key) => [key, []]));
    for (const entity of connectedEntities) {
        const group = entitiesByLibrary.get(libraryKey(entity));
        if (group) group.push(entity);
    }

    const componentsFor = (group) => {
        const groupIds = new Set(group.map((node) => node.id));
        const adjacency = new Map(group.map((node) => [node.id, new Set()]));
        for (const relation of relations) {
            if (!groupIds.has(relation.source_entity_id) || !groupIds.has(relation.target_entity_id)) continue;
            adjacency.get(relation.source_entity_id).add(relation.target_entity_id);
            adjacency.get(relation.target_entity_id).add(relation.source_entity_id);
        }
        const byId = new Map(group.map((node) => [node.id, node]));
        const remaining = new Set(groupIds);
        const components = [];
        while (remaining.size) {
            const first = remaining.values().next().value;
            const queue = [first];
            const component = [];
            remaining.delete(first);
            while (queue.length) {
                const id = queue.shift();
                component.push(byId.get(id));
                for (const neighborId of adjacency.get(id) || []) {
                    if (!remaining.delete(neighborId)) continue;
                    queue.push(neighborId);
                }
            }
            components.push(component.sort(compareNodes));
        }
        return components.sort((left, right) => right.length - left.length || compareNodes(left[0], right[0]));
    };

    const toNode = (node, x, y, role) => ({
        id: node.id,
        depth: node.depth,
        label: text(node.canonical_name),
        entityTypeKey: text(node.entity_type?.key),
        entityTypeLabel: text(node.entity_type?.label) || text(node.entity_type?.key) || 'Entity',
        role,
        focusDepth: role === 'focus' ? 0 : 1,
        x,
        y,
        size: role === 'focus' ? 30 : 16,
    });

    const clusteredNodes = libraryKeys.flatMap((key, centerIndex) => {
        const column = centerIndex % columns;
        const row = Math.floor(centerIndex / columns);
        const cellLeft = column * cellWidth;
        const centerY = (row + 0.5) * cellHeight;
        const group = (entitiesByLibrary.get(key) || []).sort(compareNodes);
        const components = componentsFor(group);
        const pairComponents = components.filter((component) => component.length === 2);
        const complexComponents = components.filter((component) => component.length !== 2);
        const pairWidth = pairComponents.length ? Math.min(260, cellWidth * 0.34) : 0;
        const mainWidth = cellWidth - pairWidth;
        const centerX = cellLeft + pairWidth + (mainWidth / 2);
        const maxRadius = Math.max(90, Math.min(mainWidth, cellHeight) * 0.43);
        const pairNodes = pairComponents.flatMap((component, pairIndex) => {
            const y = (row * cellHeight) + (((pairIndex + 0.5) * cellHeight) / pairComponents.length);
            return component.map((node, nodeIndex) => toNode(
                node,
                cellLeft + (pairWidth * (nodeIndex ? 0.72 : 0.28)),
                y,
                'pair',
            ));
        });
        const complexNodes = complexComponents.flatMap((component, componentIndex) => {
            const angle = ((Math.PI * 2 * componentIndex) / Math.max(1, complexComponents.length)) - (Math.PI / 2);
            const ring = Math.floor(componentIndex / Math.max(1, Math.ceil(Math.sqrt(complexComponents.length))));
            const anchorRadius = Math.min(maxRadius - 36, 92 + (ring * 76));
            const anchorX = centerX + (Math.cos(angle) * anchorRadius);
            const anchorY = centerY + (Math.sin(angle) * anchorRadius);
            if (component.length === 1) {
                return [toNode(component[0], anchorX, anchorY, 'neighbor')];
            }
            const componentRadius = Math.min(64, 28 + (Math.sqrt(component.length) * 9));
            return component.map((node, index) => {
                const nodeAngle = angle + ((Math.PI * 2 * index) / component.length);
                return toNode(
                    node,
                    anchorX + (Math.cos(nodeAngle) * componentRadius),
                    anchorY + (Math.sin(nodeAngle) * componentRadius),
                    'neighbor',
                );
            });
        });
        return [...pairNodes, ...complexNodes];
    });
    const isolatedWidth = width - clusterWidth;
    const isolatedColumns = Math.max(2, Math.floor(isolatedWidth / 46));
    const isolatedRows = Math.max(1, Math.ceil(isolatedEntities.length / isolatedColumns));
    const isolatedNodes = isolatedEntities.map((node, index) => {
        const column = index % isolatedColumns;
        const row = Math.floor(index / isolatedColumns);
        return toNode(
            node,
            clusterWidth + ((column + 0.5) * isolatedWidth / isolatedColumns),
            ((row + 0.5) * height / isolatedRows),
            'isolated',
        );
    });
    const nodes = [...clusteredNodes, ...isolatedNodes];
    const nodeIds = new Set(nodes.map((node) => node.id));
    return {
        width,
        height,
        nodes,
        relations: relations.filter((relation) => (
            nodeIds.has(relation.source_entity_id) && nodeIds.has(relation.target_entity_id)
        )).map((relation) => ({
            id: relation.id,
            sourceId: relation.source_entity_id,
            targetId: relation.target_entity_id,
            direction: relation.relation_type?.direction,
            label: chatGraphRelationLabel(relation),
        })),
    };
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
    if (value?.panorama) {
        return layoutLibraryPanorama(sourceNodes, relations, width, height);
    }
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
