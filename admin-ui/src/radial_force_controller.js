import { chatGraphRelationLabel } from './chat_graph_exploration.js';

const POSITION_EPSILON = 0.1;
const EDGE_PAN_THRESHOLD = 5;

function finitePosition(position) {
    return Number.isFinite(position?.x) && Number.isFinite(position?.y);
}

function finiteBounds(bounds) {
    return Number.isFinite(bounds?.x1) && Number.isFinite(bounds?.x2)
        && Number.isFinite(bounds?.y1) && Number.isFinite(bounds?.y2)
        && bounds.x2 > bounds.x1 && bounds.y2 > bounds.y1;
}

function relationSource(relation) {
    return String(relation?.source_entity_id || '');
}

function relationTarget(relation) {
    return String(relation?.target_entity_id || '');
}

function deterministicOffset(id, index = 0) {
    let hash = 2166136261;
    for (const character of String(id || '')) {
        hash ^= character.codePointAt(0);
        hash = Math.imul(hash, 16777619);
    }
    const angle = ((hash >>> 0) / 4294967296) * Math.PI * 2;
    const radius = 18 + ((hash >>> 8) % 37) + (index % 7) * 4;
    return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
}

function viewportModelCenter(graphView) {
    const extent = graphView?.extent?.();
    if (Number.isFinite(extent?.x1) && Number.isFinite(extent?.x2)
        && Number.isFinite(extent?.y1) && Number.isFinite(extent?.y2)) {
        return { x: (extent.x1 + extent.x2) / 2, y: (extent.y1 + extent.y2) / 2 };
    }
    return {
        x: Math.max(1, Number(graphView?.width?.()) || 1) / 2,
        y: Math.max(1, Number(graphView?.height?.()) || 1) / 2,
    };
}

function graphDegrees(graph) {
    const degrees = new Map((graph?.nodes || []).map((node) => [String(node.id), 0]));
    for (const relation of graph?.relations || []) {
        const source = relationSource(relation);
        const target = relationTarget(relation);
        if (!degrees.has(source) || !degrees.has(target)) continue;
        degrees.set(source, degrees.get(source) + 1);
        degrees.set(target, degrees.get(target) + 1);
    }
    return degrees;
}

function primaryNodeId(graph, preferredId, degrees) {
    if (preferredId && degrees.has(preferredId)) return preferredId;
    let bestId = '';
    let bestDegree = -1;
    for (const node of graph?.nodes || []) {
        const id = String(node.id);
        const degree = degrees.get(id) || 0;
        if (degree > bestDegree) {
            bestId = id;
            bestDegree = degree;
        }
    }
    return bestId;
}

function nodeElementData(node, degree) {
    return {
        id: String(node.id),
        label: node.canonical_name || node.label || String(node.id),
        degree,
        size: Math.min(38, 20 + (Math.sqrt(Math.max(1, degree)) * 4.5)),
    };
}

function edgeElementData(relation) {
    return {
        id: String(relation.id),
        source: relationSource(relation),
        target: relationTarget(relation),
        label: chatGraphRelationLabel(relation),
    };
}

function edgeClasses(relation) {
    return relation.relation_type?.direction === 'directed'
        || relation.direction === 'directed' ? 'radial-directed' : '';
}

function syncCytoscapeElements(graphView, graph, positionsById, degrees) {
    const nodes = graph?.nodes || [];
    const nodeIds = new Set(nodes.map((node) => String(node.id)));
    const relations = (graph?.relations || []).filter((relation) => (
        nodeIds.has(relationSource(relation)) && nodeIds.has(relationTarget(relation))
    ));
    const edgeIds = new Set(relations.map((relation) => String(relation.id)));

    graphView.batch(() => {
        graphView.edges().forEach((edge) => {
            if (!edgeIds.has(edge.id())) edge.remove();
        });
        graphView.nodes().forEach((node) => {
            if (!nodeIds.has(node.id())) node.remove();
        });

        for (const node of nodes) {
            const id = String(node.id);
            const data = nodeElementData(node, degrees.get(id) || 0);
            const existing = graphView.getElementById(id);
            if (existing.length) {
                existing.data({ label: data.label, degree: data.degree, size: data.size });
                existing.toggleClass('radial-isolated', data.degree === 0);
                continue;
            }
            graphView.add({
                group: 'nodes',
                data,
                position: { ...positionsById.get(id) },
                classes: data.degree === 0 ? 'radial-isolated' : '',
            });
        }

        for (const relation of relations) {
            const data = edgeElementData(relation);
            let existing = graphView.getElementById(data.id);
            if (existing.length && (existing.source().id() !== data.source
                || existing.target().id() !== data.target)) {
                existing.remove();
                existing = graphView.getElementById(data.id);
            }
            if (existing.length) {
                existing.data('label', data.label);
                existing.classes(edgeClasses(relation));
                continue;
            }
            graphView.add({ group: 'edges', data, classes: edgeClasses(relation) });
        }
    });
}

export function createEdgePanController(graphView, threshold = EDGE_PAN_THRESHOLD) {
    let gesture = null;

    function start(edgeId, renderedPosition) {
        if (!edgeId || !finitePosition(renderedPosition)) return false;
        gesture = {
            edgeId,
            start: { ...renderedPosition },
            last: { ...renderedPosition },
            panning: false,
            userPanningEnabled: graphView.userPanningEnabled(),
        };
        return true;
    }

    function move(renderedPosition) {
        if (!gesture || !finitePosition(renderedPosition)) return false;
        if (!gesture.panning) {
            const distance = Math.hypot(
                renderedPosition.x - gesture.start.x,
                renderedPosition.y - gesture.start.y,
            );
            if (distance <= threshold) return false;
            gesture.panning = true;
            graphView.userPanningEnabled(false);
        }
        graphView.panBy({
            x: renderedPosition.x - gesture.last.x,
            y: renderedPosition.y - gesture.last.y,
        });
        gesture.last = { ...renderedPosition };
        return true;
    }

    function finish() {
        if (!gesture) return null;
        const result = { edgeId: gesture.edgeId, panned: gesture.panning };
        graphView.userPanningEnabled(gesture.userPanningEnabled);
        gesture = null;
        return result;
    }

    function destroy() {
        finish();
    }

    return { start, move, finish, destroy, isActive: () => Boolean(gesture) };
}

export function createRadialForceController({
    graphView,
    forceApi,
    bounds = null,
    requestFrame = (callback) => requestAnimationFrame(callback),
    cancelFrame = (frame) => cancelAnimationFrame(frame),
}) {
    const simulationNodesById = new Map();
    const simulationLinksById = new Map();
    const lastAppliedPositions = new Map();
    let center = viewportModelCenter(graphView);
    let pendingFrame = null;
    let destroyed = false;
    let isApplyingSimulationTick = false;
    let currentPrimaryNodeId = '';

    const linkForce = forceApi.forceLink([])
        .id((node) => node.id)
        .distance(104)
        .strength(0.16);
    const xForce = forceApi.forceX(() => center.x)
        .strength((node) => (node.primary ? 0.11 : 0.025));
    const yForce = forceApi.forceY(() => center.y)
        .strength((node) => (node.primary ? 0.11 : 0.025));
    const simulation = forceApi.forceSimulation([])
        .force('link', linkForce)
        .force('charge', forceApi.forceManyBody().strength((node) => (
            node.degree === 0 ? -45 : -220
        )).distanceMax(900))
        .force('collision', forceApi.forceCollide((node) => (node.size / 2) + 7).strength(0.9))
        .force('x', xForce)
        .force('y', yForce)
        .alphaDecay(0.035)
        .velocityDecay(0.42)
        .stop();

    function constrainPosition(position, node = {}) {
        const viewportBounds = typeof bounds === 'function' ? bounds() : bounds;
        if (!finitePosition(position) || !finiteBounds(viewportBounds)) return { ...position };
        const width = viewportBounds.x2 - viewportBounds.x1;
        const height = viewportBounds.y2 - viewportBounds.y1;
        const horizontalPadding = Math.min((Number(node.size) || 20) / 2 + 72, width / 2);
        const verticalPadding = Math.min((Number(node.size) || 20) / 2 + 28, height / 2);
        return {
            x: Math.min(viewportBounds.x2 - horizontalPadding,
                Math.max(viewportBounds.x1 + horizontalPadding, position.x)),
            y: Math.min(viewportBounds.y2 - verticalPadding,
                Math.max(viewportBounds.y1 + verticalPadding, position.y)),
        };
    }

    function constrainSimulationNode(node) {
        const position = constrainPosition(node, node);
        node.x = position.x;
        node.y = position.y;
        if (Number.isFinite(node.fx)) node.fx = position.x;
        if (Number.isFinite(node.fy)) node.fy = position.y;
        return position;
    }

    function applySimulationPositions() {
        pendingFrame = null;
        if (destroyed) return;
        isApplyingSimulationTick = true;
        try {
            graphView.batch(() => {
                for (const simulationNode of simulationNodesById.values()) {
                    if (!finitePosition(simulationNode)) continue;
                    const graphNode = graphView.getElementById(simulationNode.id);
                    if (!graphNode.length || graphNode.grabbed()) continue;
                    const nextPosition = constrainSimulationNode(simulationNode);
                    const previous = lastAppliedPositions.get(simulationNode.id)
                        || graphNode.position();
                    if (Math.abs(previous.x - nextPosition.x) < POSITION_EPSILON
                        && Math.abs(previous.y - nextPosition.y) < POSITION_EPSILON) continue;
                    graphNode.position(nextPosition);
                    lastAppliedPositions.set(simulationNode.id, nextPosition);
                }
            });
        } finally {
            isApplyingSimulationTick = false;
        }
    }

    function scheduleSimulationPositions() {
        if (destroyed || pendingFrame !== null) return;
        pendingFrame = requestFrame(applySimulationPositions);
    }

    simulation.on('tick.radial-canvas', scheduleSimulationPositions);

    function newNodePosition(id, adjacentIds, index) {
        const neighbors = [...(adjacentIds.get(id) || [])]
            .map((neighborId) => simulationNodesById.get(neighborId))
            .filter(finitePosition);
        const origin = neighbors.length ? {
            x: neighbors.reduce((sum, node) => sum + node.x, 0) / neighbors.length,
            y: neighbors.reduce((sum, node) => sum + node.y, 0) / neighbors.length,
        } : center;
        const offset = deterministicOffset(id, index);
        return { x: origin.x + offset.x, y: origin.y + offset.y };
    }

    function updateForceAccessors() {
        xForce.x(() => center.x).strength((node) => (node.primary ? 0.11 : 0.025));
        yForce.y(() => center.y).strength((node) => (node.primary ? 0.11 : 0.025));
    }

    function syncGraph(graph, preferredPrimaryId = '') {
        const wasEmpty = simulationNodesById.size === 0;
        const nodes = graph?.nodes || [];
        const nodeIds = new Set(nodes.map((node) => String(node.id)));
        const relations = (graph?.relations || []).filter((relation) => (
            nodeIds.has(relationSource(relation)) && nodeIds.has(relationTarget(relation))
        ));
        const degrees = graphDegrees({ nodes, relations });
        const nextPrimaryNodeId = primaryNodeId(graph, preferredPrimaryId, degrees);
        const adjacentIds = new Map(nodes.map((node) => [String(node.id), new Set()]));
        for (const relation of relations) {
            adjacentIds.get(relationSource(relation)).add(relationTarget(relation));
            adjacentIds.get(relationTarget(relation)).add(relationSource(relation));
        }

        let topologyChanged = simulationNodesById.size !== nodeIds.size
            || simulationLinksById.size !== relations.length;
        for (const id of [...simulationNodesById.keys()]) {
            if (nodeIds.has(id)) continue;
            simulationNodesById.delete(id);
            lastAppliedPositions.delete(id);
            topologyChanged = true;
        }

        nodes.forEach((node, index) => {
            const id = String(node.id);
            let simulationNode = simulationNodesById.get(id);
            if (!simulationNode) {
                const position = newNodePosition(id, adjacentIds, index);
                simulationNode = { id, x: position.x, y: position.y, vx: 0, vy: 0, fx: null, fy: null };
                simulationNodesById.set(id, simulationNode);
                topologyChanged = true;
            }
            simulationNode.degree = degrees.get(id) || 0;
            simulationNode.size = nodeElementData(node, simulationNode.degree).size;
            simulationNode.primary = id === nextPrimaryNodeId;
            constrainSimulationNode(simulationNode);
        });

        const relationIds = new Set();
        for (const relation of relations) {
            const id = String(relation.id);
            const sourceId = relationSource(relation);
            const targetId = relationTarget(relation);
            relationIds.add(id);
            let simulationLink = simulationLinksById.get(id);
            if (!simulationLink || simulationLink.sourceId !== sourceId
                || simulationLink.targetId !== targetId) {
                simulationLink = { id, sourceId, targetId, source: sourceId, target: targetId };
                simulationLinksById.set(id, simulationLink);
                topologyChanged = true;
            }
        }
        for (const id of [...simulationLinksById.keys()]) {
            if (relationIds.has(id)) continue;
            simulationLinksById.delete(id);
            topologyChanged = true;
        }

        currentPrimaryNodeId = nextPrimaryNodeId;
        updateForceAccessors();
        syncCytoscapeElements(graphView, { nodes, relations }, simulationNodesById, degrees);

        if (topologyChanged) {
            const simulationNodes = [...simulationNodesById.values()];
            const simulationLinks = [...simulationLinksById.values()];
            for (const link of simulationLinks) {
                link.source = link.sourceId;
                link.target = link.targetId;
            }
            simulation.nodes(simulationNodes);
            linkForce.links(simulationLinks);
            if (simulationNodes.length) {
                simulation.alpha(wasEmpty ? 1 : Math.max(simulation.alpha(), 0.28))
                    .alphaTarget(0)
                    .restart();
            } else {
                simulation.stop();
            }
        }
        return { topologyChanged, primaryNodeId: currentPrimaryNodeId };
    }

    function setPrimaryNode(id) {
        const nextId = simulationNodesById.has(id) ? id : currentPrimaryNodeId;
        if (nextId === currentPrimaryNodeId) return;
        currentPrimaryNodeId = nextId;
        for (const node of simulationNodesById.values()) node.primary = node.id === nextId;
        updateForceAccessors();
    }

    function grabNode(id, position) {
        const node = simulationNodesById.get(id);
        if (!node || !finitePosition(position)) return false;
        const nextPosition = constrainPosition(position, node);
        node.fx = nextPosition.x;
        node.fy = nextPosition.y;
        node.x = nextPosition.x;
        node.y = nextPosition.y;
        node.vx = 0;
        node.vy = 0;
        simulation.alphaTarget(0.2).restart();
        return true;
    }

    function dragNode(id, position) {
        const node = simulationNodesById.get(id);
        if (!node || !finitePosition(position)) return false;
        const nextPosition = constrainPosition(position, node);
        node.fx = nextPosition.x;
        node.fy = nextPosition.y;
        graphView.getElementById(id).position(nextPosition);
        return true;
    }

    function freeNode(id, position) {
        const node = simulationNodesById.get(id);
        if (!node || !finitePosition(position)) return false;
        const nextPosition = constrainPosition(position, node);
        node.x = nextPosition.x;
        node.y = nextPosition.y;
        graphView.getElementById(id).position(nextPosition);
        node.fx = null;
        node.fy = null;
        simulation.alphaTarget(0);
        return true;
    }

    function resize() {
        center = viewportModelCenter(graphView);
        updateForceAccessors();
        for (const node of simulationNodesById.values()) constrainSimulationNode(node);
        scheduleSimulationPositions();
    }

    function relayout() {
        for (const node of simulationNodesById.values()) {
            node.fx = null;
            node.fy = null;
            node.vx = 0;
            node.vy = 0;
        }
        simulation.alpha(1).alphaTarget(0).restart();
    }

    function destroy() {
        destroyed = true;
        simulation.stop();
        simulation.on('tick.radial-canvas', null);
        if (pendingFrame !== null) cancelFrame(pendingFrame);
        pendingFrame = null;
    }

    return {
        syncGraph,
        setPrimaryNode,
        grabNode,
        dragNode,
        freeNode,
        resize,
        relayout,
        destroy,
        simulation,
        simulationNodesById,
        simulationLinksById,
        get primaryNodeId() { return currentPrimaryNodeId; },
        get isApplyingSimulationTick() { return isApplyingSimulationTick; },
    };
}
