import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import cytoscape from 'cytoscape';
import * as d3Force from 'd3-force';

import { layoutCitationGraph } from '../chat_graph_layout.js';
import { chatGraphDegrees, projectChatGraphFocus } from '../chat_graph_exploration.js';
import {
    createEdgePanController,
    createRadialForceController,
} from '../radial_force_controller.js';

export default {
    props: {
        graph: { type: Object, required: true },
        selectedId: { type: String, default: '' },
        centerSelected: { type: Boolean, default: false },
        userZoomingEnabled: { type: Boolean, default: true },
        constrainToViewport: { type: Boolean, default: false },
        variant: { type: String, default: 'radial' },
        expandingId: { type: String, default: '' },
        expandedIds: { type: Array, default: () => [] },
    },
    emits: ['open-entity', 'focus-entity', 'open-relation', 'clear-selection'],
    setup(props, { emit }) {
        const shell = ref(null);
        const cytoscapeHost = ref(null);
        const citationProjection = ref(null);
        const citationSettingsOpen = ref(false);
        let resizeObserver = null;
        let graphView = null;
        let citationFocusId = '';
        let centerDrag = null;
        let syncingCenterDrag = false;
        let radialForceController = null;
        let edgePanController = null;
        let radialViewportFrame = null;
        let resizeFrame = null;
        let selectedNodeId = '';
        let radialPrimaryNodeId = '';
        let hoveredNodeId = '';
        let draggingNodeId = '';
        let dragStart = null;
        let didDrag = false;
        let suppressTapNodeId = '';
        let suppressTapUntil = 0;
        let suppressEdgeTapId = '';
        let suppressEdgeTapUntil = 0;
        let neighborsByNodeId = new Map();
        let edgeIdsByNodeId = new Map();

        function resizeCanvas() {
            if (resizeFrame !== null) return;
            resizeFrame = requestAnimationFrame(() => {
                resizeFrame = null;
                graphView?.resize();
                if (props.variant !== 'citation') radialForceController?.resize();
            });
        }

        function radialStyles() {
            return [
                {
                    selector: 'node',
                    style: {
                        width: 'data(size)', height: 'data(size)', label: 'data(label)',
                        'background-color': '#656B70', 'border-color': '#FFFFFF', 'border-width': 1.5,
                        color: '#27343A', 'font-family': 'Inter, Microsoft YaHei, sans-serif',
                        'font-size': 12, 'font-weight': 500, 'text-wrap': 'ellipsis',
                        'text-max-width': 160, 'text-valign': 'bottom', 'text-margin-y': 9,
                        'text-opacity': 0, 'min-zoomed-font-size': 10,
                        'overlay-opacity': 0, 'text-events': 'no',
                        'transition-property': 'background-color, border-color, border-width, opacity, text-opacity, width, height, underlay-opacity',
                        'transition-duration': '180ms', 'transition-timing-function': 'ease-out',
                    },
                },
                {
                    selector: 'node.radial-isolated',
                    style: {
                        'background-color': '#919BA3', color: '#59666D',
                        'border-color': '#F4F5F6', 'border-width': 1.5, 'font-size': 11,
                        'text-opacity': 0,
                    },
                },
                {
                    selector: 'node.radial-isolated.is-hovered, node.radial-isolated.is-selected',
                    style: { 'text-opacity': 1, 'min-zoomed-font-size': 0 },
                },
                {
                    selector: 'edge',
                    style: {
                        width: 1.2, 'curve-style': 'straight', 'line-color': '#B8C0CB',
                        'target-arrow-color': '#929CAA', 'target-arrow-shape': 'none',
                        label: 'data(label)', color: '#526168',
                        'font-family': 'Inter, Microsoft YaHei, sans-serif', 'font-size': 10,
                        'font-weight': 600, 'text-background-color': '#FFFFFF',
                        'text-background-opacity': 0.92, 'text-background-padding': 3,
                        'text-rotation': 'autorotate', 'text-opacity': 0,
                        'min-zoomed-font-size': 8, 'overlay-opacity': 0,
                        'transition-property': 'line-color, width, opacity, text-opacity',
                        'transition-duration': '180ms', 'transition-timing-function': 'ease-out',
                    },
                },
                { selector: 'edge.radial-directed', style: { 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8 } },
                {
                    selector: 'node.is-selected, node.is-hovered',
                    style: {
                        width: 'mapData(degree, 0, 20, 23, 38)',
                        height: 'mapData(degree, 0, 20, 23, 38)',
                        'background-color': '#7C5CE7', 'border-color': '#C4B5FD', 'border-width': 3,
                        'underlay-color': '#7C5CE7', 'underlay-opacity': 0.18, 'underlay-padding': 9,
                        color: '#382A70', 'font-weight': 600, 'text-opacity': 1, 'min-zoomed-font-size': 0,
                    },
                },
                {
                    selector: 'edge:selected, edge.is-hovered',
                    style: {
                        width: 2.4, 'line-color': '#7C5CE7', 'target-arrow-color': '#7C5CE7',
                        color: '#5A3CC4', 'text-opacity': 1, 'min-zoomed-font-size': 0,
                    },
                },
                {
                    selector: 'node.is-neighbor',
                    style: { 'border-color': '#B7A8ED', 'border-width': 2, 'text-opacity': 1 },
                },
                {
                    selector: 'edge.is-related',
                    style: {
                        width: 1.8, 'line-color': '#A996E8', 'target-arrow-color': '#A996E8',
                        color: '#5A3CC4', 'text-opacity': 1, 'min-zoomed-font-size': 0,
                    },
                },
                { selector: 'node.is-dimmed', style: { opacity: 0.56, 'text-opacity': 0 } },
                { selector: 'edge.is-dimmed', style: { opacity: 0.42, 'text-opacity': 0 } },
                {
                    selector: 'node.is-dragging',
                    style: {
                        'background-color': '#7C5CE7', 'border-color': '#C4B5FD', 'border-width': 3,
                        'underlay-color': '#7C5CE7', 'underlay-opacity': 0.2, 'underlay-padding': 11,
                        color: '#382A70', 'font-weight': 600, 'text-opacity': 1, 'min-zoomed-font-size': 0,
                    },
                },
            ];
        }

        function fitRadialGraph(padding = 68) {
            const visible = graphView?.elements().filter((element) => element.visible());
            if (!visible?.length) return;
            graphView.fit(visible, padding);
            if (graphView.zoom() > 1.2) graphView.zoom(1.2);
            graphView.center(visible);
        }

        function buildAdjacencyIndexes() {
            neighborsByNodeId = new Map(props.graph.nodes.map((node) => [node.id, new Set()]));
            edgeIdsByNodeId = new Map(props.graph.nodes.map((node) => [node.id, new Set()]));
            for (const relation of props.graph.relations) {
                if (!neighborsByNodeId.has(relation.source_entity_id)
                    || !neighborsByNodeId.has(relation.target_entity_id)) continue;
                neighborsByNodeId.get(relation.source_entity_id).add(relation.target_entity_id);
                neighborsByNodeId.get(relation.target_entity_id).add(relation.source_entity_id);
                edgeIdsByNodeId.get(relation.source_entity_id).add(relation.id);
                edgeIdsByNodeId.get(relation.target_entity_id).add(relation.id);
            }
        }

        function applyGraphFocus() {
            if (!graphView) return;
            const persistentId = selectedNodeId || radialPrimaryNodeId;
            const focusId = draggingNodeId || hoveredNodeId || persistentId;
            graphView.batch(() => {
                graphView.elements().removeClass('is-selected is-hovered is-neighbor is-related is-dimmed is-dragging');
                const focus = focusId ? graphView.getElementById(focusId) : null;
                if (!focus?.length) return;
                graphView.elements().addClass('is-dimmed');
                focus.removeClass('is-dimmed').addClass(
                    draggingNodeId ? 'is-dragging' : hoveredNodeId ? 'is-hovered' : 'is-selected',
                );
                if (persistentId && persistentId !== focusId) {
                    graphView.getElementById(persistentId)
                        .removeClass('is-dimmed')
                        .addClass('is-selected');
                }
                for (const id of neighborsByNodeId.get(focusId) || []) {
                    graphView.getElementById(id).removeClass('is-dimmed').addClass('is-neighbor');
                }
                for (const id of edgeIdsByNodeId.get(focusId) || []) {
                    graphView.getElementById(id).removeClass('is-dimmed').addClass('is-related');
                }
            });
        }

        function hasRadialLayoutViewport() {
            const rect = cytoscapeHost.value?.getBoundingClientRect();
            return Boolean(
                rect
                && rect.width > 1
                && rect.height > 1
                && graphView?.width() > 1
                && graphView?.height() > 1,
            );
        }

        function scheduleRadialViewportAction(expectedGraph, action, attempts = 30) {
            if (radialViewportFrame !== null) cancelAnimationFrame(radialViewportFrame);
            const runWhenSized = () => {
                radialViewportFrame = requestAnimationFrame(() => {
                    radialViewportFrame = null;
                    if (graphView !== expectedGraph || props.variant === 'citation') return;
                    expectedGraph.resize();
                    if (hasRadialLayoutViewport()) {
                        action();
                        return;
                    }
                    attempts -= 1;
                    if (attempts > 0) runWhenSized();
                });
            };
            runWhenSized();
        }

        function animateToNode(node) {
            const position = node?.renderedPosition();
            if (!graphView || !position) return;
            graphView.stop();
            graphView.animate({ center: { eles: node } }, { duration: 340, easing: 'ease-out' });
        }

        function focusRadialNode(nodeId) {
            if (!graphView || props.variant === 'citation' || !nodeId) return;
            const expectedGraph = graphView;
            scheduleRadialViewportAction(expectedGraph, () => {
                const node = expectedGraph.getElementById(nodeId);
                if (node.length) animateToNode(node);
            });
        }

        function focusSelectedRadialNode() {
            focusRadialNode(props.selectedId);
        }

        function syncRadialGraph() {
            if (props.variant === 'citation') return;
            if (!graphView || !radialForceController) {
                renderRadialGraph();
                return;
            }
            buildAdjacencyIndexes();
            const result = radialForceController.syncGraph(
                props.graph,
                selectedNodeId || props.selectedId || '',
            );
            radialPrimaryNodeId = result.primaryNodeId;
            applyGraphFocus();
        }

        function releaseRadialNode(event) {
            const id = event.target.id();
            if (!dragStart || draggingNodeId !== id) return;
            radialForceController?.freeNode(id, event.target.position());
            if (didDrag) {
                suppressTapNodeId = id;
                suppressTapUntil = Date.now() + 220;
            }
            draggingNodeId = '';
            dragStart = null;
            didDrag = false;
            applyGraphFocus();
        }

        function finishEdgeGesture() {
            const result = edgePanController?.finish();
            if (!result) return;
            if (result.panned) {
                suppressEdgeTapId = result.edgeId;
                suppressEdgeTapUntil = Date.now() + 220;
            }
            if (cytoscapeHost.value) cytoscapeHost.value.style.cursor = 'grab';
        }

        function renderRadialGraph() {
            if (props.variant === 'citation' || !cytoscapeHost.value || graphView) return;
            selectedNodeId = props.selectedId || '';
            hoveredNodeId = '';
            draggingNodeId = '';
            dragStart = null;
            didDrag = false;
            graphView = cytoscape({
                container: cytoscapeHost.value,
                elements: [],
                style: radialStyles(),
                layout: { name: 'preset' },
                minZoom: 0.15,
                maxZoom: 4,
                boxSelectionEnabled: false,
                panningEnabled: !props.constrainToViewport,
                userPanningEnabled: !props.constrainToViewport,
                zoomingEnabled: true,
                userZoomingEnabled: props.userZoomingEnabled && !props.constrainToViewport,
                autoungrabify: false,
                autounselectify: false,
            });
            radialForceController = createRadialForceController({
                graphView,
                forceApi: d3Force,
                bounds: () => (props.constrainToViewport ? graphView?.extent() : null),
            });
            edgePanController = createEdgePanController(graphView);

            graphView.on('tap', 'node', (event) => {
                if (event.target.id() === suppressTapNodeId && Date.now() < suppressTapUntil) return;
                draggingNodeId = '';
                dragStart = null;
                didDrag = false;
                selectedNodeId = event.target.id();
                if (props.centerSelected) {
                    radialPrimaryNodeId = selectedNodeId;
                    radialForceController?.setPrimaryNode(selectedNodeId);
                }
                applyGraphFocus();
                const node = props.graph.nodes.find((item) => item.id === event.target.id());
                if (node && !node.library_center) emit('open-entity', node);
                if (props.centerSelected) focusRadialNode(event.target.id());
            });
            graphView.on('tap', 'edge', (event) => {
                if (event.target.id() === suppressEdgeTapId && Date.now() < suppressEdgeTapUntil) return;
                const relation = props.graph.relations.find((item) => item.id === event.target.id());
                if (relation) emit('open-relation', relation);
            });
            graphView.on('tap', (event) => {
                if (event.target !== graphView) return;
                selectedNodeId = '';
                hoveredNodeId = '';
                applyGraphFocus();
                emit('clear-selection');
            });
            graphView.on('mouseover', 'node', (event) => {
                if (draggingNodeId) return;
                hoveredNodeId = event.target.id();
                applyGraphFocus();
                if (cytoscapeHost.value) cytoscapeHost.value.style.cursor = 'pointer';
            });
            graphView.on('mouseout', 'node', (event) => {
                if (hoveredNodeId === event.target.id()) hoveredNodeId = '';
                applyGraphFocus();
                if (cytoscapeHost.value) cytoscapeHost.value.style.cursor = 'grab';
            });
            graphView.on('position', 'node', (event) => {
                if (!props.centerSelected || draggingNodeId
                    || event.target.id() !== selectedNodeId) return;
                graphView.center(event.target);
            });
            graphView.on('grab', 'node', (event) => {
                const position = event.target.position();
                draggingNodeId = event.target.id();
                dragStart = { position: { ...position } };
                didDrag = false;
                hoveredNodeId = '';
                radialForceController?.grabNode(draggingNodeId, position);
                applyGraphFocus();
            });
            graphView.on('drag', 'node', (event) => {
                if (!dragStart || draggingNodeId !== event.target.id()) return;
                radialForceController?.dragNode(event.target.id(), event.position);
                const position = event.target.position();
                const renderedDistance = Math.hypot(
                    position.x - dragStart.position.x,
                    position.y - dragStart.position.y,
                ) * graphView.zoom();
                if (renderedDistance > 5) didDrag = true;
            });
            graphView.on('dragfree', 'node', releaseRadialNode);
            graphView.on('free', 'node', releaseRadialNode);

            graphView.on('mousedown touchstart', 'edge', (event) => {
                if (props.constrainToViewport) return;
                edgePanController?.start(event.target.id(), event.renderedPosition);
            });
            graphView.on('mousemove touchmove', (event) => {
                if (props.constrainToViewport) return;
                if (!edgePanController?.isActive()) return;
                if (edgePanController.move(event.renderedPosition) && cytoscapeHost.value) {
                    cytoscapeHost.value.style.cursor = 'grabbing';
                }
            });
            graphView.on('mouseup touchend', finishEdgeGesture);

            graphView.ready(() => {
                const expectedGraph = graphView;
                scheduleRadialViewportAction(expectedGraph, () => {
                    radialForceController?.resize();
                    syncRadialGraph();
                });
            });
        }

        function citationElements(citationLayout, visibleGraph, previousPositions = new Map(), growthOrigin = null) {
            const byId = new Map(citationLayout.nodes.map((node) => [node.id, node]));
            const degrees = chatGraphDegrees(visibleGraph);
            const expandedIds = new Set(props.expandedIds);
            return [
                ...citationLayout.nodes.map((node, index) => {
                    const degree = degrees.get(node.id) || 0;
                    const size = Math.min(42, 15 + (Math.sqrt(Math.max(1, degree)) * 6));
                    const angle = (index * 2.399963229728653) - (Math.PI / 2);
                    const position = previousPositions.get(node.id) || (growthOrigin
                        ? {
                            x: growthOrigin.x + (Math.cos(angle) * 18),
                            y: growthOrigin.y + (Math.sin(angle) * 18),
                        }
                        : { x: node.x, y: node.y });
                    const classes = [
                        node.role === 'focus' ? 'citation-focus' : 'citation-neighbor',
                        props.expandingId === node.id ? 'is-expanding' : '',
                        expandedIds.has(node.id) ? 'is-expanded' : '',
                    ].filter(Boolean).join(' ');
                    return {
                        group: 'nodes',
                        data: {
                            id: node.id,
                            label: node.label,
                            entityType: node.entityTypeLabel,
                            size: node.role === 'focus' ? Math.max(34, size) : Math.min(18, size),
                            degree,
                            focusDepth: node.focusDepth,
                        },
                        position,
                        classes,
                    };
                }),
                ...citationLayout.relations.map((relation) => ({
                    group: 'edges',
                    data: {
                        id: relation.id,
                        source: relation.sourceId,
                        target: relation.targetId,
                        label: relation.label,
                        directed: relation.direction === 'directed' ? 'yes' : 'no',
                    },
                    classes: relation.direction === 'directed' ? 'citation-directed' : '',
                })).filter((edge) => byId.has(edge.data.source) && byId.has(edge.data.target)),
            ];
        }

        function citationStyles() {
            return [
                {
                    selector: 'node',
                    style: {
                        shape: 'ellipse',
                        width: 'data(size)',
                        height: 'data(size)',
                        label: 'data(label)',
                        color: '#20343A',
                        'font-family': 'Inter, Microsoft YaHei, sans-serif',
                        'font-size': 12,
                        'font-weight': 500,
                        'text-wrap': 'ellipsis',
                        'text-max-width': 150,
                        'text-valign': 'bottom',
                        'text-halign': 'center',
                        'text-margin-y': 8,
                        'background-color': '#63696C',
                        'border-color': '#FFFFFF',
                        'border-width': 1.5,
                        'text-opacity': 1,
                        'overlay-opacity': 0,
                    },
                },
                {
                    selector: 'node.citation-focus',
                    style: {
                        color: '#38434A',
                        'background-color': '#7C5CE7',
                        'border-color': '#FFFFFF',
                        'border-width': 2.5,
                        'font-size': 13,
                        'text-opacity': 1,
                    },
                },
                {
                    selector: 'edge',
                    style: {
                        width: 1,
                        'curve-style': 'straight',
                        'line-color': '#C9CED8',
                        'target-arrow-color': '#9FA7B5',
                        'target-arrow-shape': 'none',
                        label: 'data(label)',
                        color: '#5A4E86',
                        'font-family': 'Inter, Microsoft YaHei, sans-serif',
                        'font-size': 10,
                        'font-weight': 600,
                        'text-background-color': '#FFFFFF',
                        'text-background-opacity': 0.94,
                        'text-background-padding': 3,
                        'text-rotation': 'autorotate',
                        'text-opacity': 0,
                        'overlay-opacity': 0,
                        'arrow-scale': 0.9,
                    },
                },
                {
                    selector: 'edge.citation-directed',
                    style: { 'target-arrow-shape': 'none' },
                },
                {
                    selector: 'node:selected',
                    style: {
                        'background-color': '#63696C',
                        'border-color': '#A38CF4',
                        'border-width': 3,
                        'underlay-color': '#7C5CE7',
                        'underlay-opacity': 0.16,
                        'underlay-padding': 8,
                        'text-opacity': 1,
                    },
                },
                {
                    selector: 'node.citation-focus:selected',
                    style: { 'background-color': '#7C5CE7' },
                },
                {
                    selector: 'edge:selected',
                    style: {
                        width: 2.5,
                        'line-color': '#7C5CE7',
                        'target-arrow-color': '#7C5CE7',
                        'target-arrow-shape': 'none',
                        color: '#5A3CC4',
                        'text-opacity': 1,
                    },
                },
                {
                    selector: 'edge.citation-directed:selected',
                    style: { 'target-arrow-shape': 'triangle' },
                },
                {
                    selector: 'node.is-hovered',
                    style: {
                        'text-opacity': 1,
                        'underlay-color': '#FFFFFF',
                        'underlay-opacity': 0.8,
                        'underlay-padding': 5,
                    },
                },
                {
                    selector: 'edge.is-hovered',
                    style: {
                        width: 2.2,
                        'line-color': '#7C5CE7',
                        'target-arrow-color': '#7C5CE7',
                        'target-arrow-shape': 'none',
                        'text-opacity': 1,
                    },
                },
                {
                    selector: 'edge.citation-directed.is-hovered',
                    style: { 'target-arrow-shape': 'triangle' },
                },
                {
                    selector: 'node.is-expanded',
                    style: { 'border-color': '#4D5A5F' },
                },
                {
                    selector: 'node.is-expanding',
                    style: {
                        'border-color': '#D78324',
                        'border-width': 3,
                        'underlay-color': '#F2B35F',
                        'underlay-opacity': 0.22,
                        'underlay-padding': 9,
                    },
                },
                {
                    selector: 'node:grabbed',
                    style: {
                        'underlay-color': '#7C5CE7',
                        'underlay-opacity': 0.12,
                        'underlay-padding': 10,
                    },
                },
            ];
        }

        function syncCitationSelection() {
            if (!graphView) return;
            graphView.elements().unselect();
            if (!props.selectedId) return;
            const selected = graphView.getElementById(props.selectedId);
            if (!selected.length) return;
            selected.select();
        }

        function fitCitationAroundFocus(padding = 76) {
            if (!graphView) return;
            const focus = graphView.getElementById(citationFocusId);
            if (!focus.length) {
                graphView.fit(graphView.elements(), padding);
                return;
            }
            const focusPosition = focus.position();
            let maxDistanceX = 1;
            let maxDistanceY = 1;
            graphView.nodes().forEach((node) => {
                const position = node.position();
                maxDistanceX = Math.max(maxDistanceX, Math.abs(position.x - focusPosition.x));
                maxDistanceY = Math.max(maxDistanceY, Math.abs(position.y - focusPosition.y));
            });
            const availableWidth = Math.max(120, graphView.width() - (padding * 2));
            const availableHeight = Math.max(120, graphView.height() - (padding * 2));
            const level = Math.max(graphView.minZoom(), Math.min(
                graphView.maxZoom(),
                1.1,
                availableWidth / ((maxDistanceX * 2) + 54),
                availableHeight / ((maxDistanceY * 2) + 54),
            ));
            graphView.zoom(level);
            graphView.center(focus);
        }

        function syncCitationFocusData() {
            if (!graphView) return;
            const projection = projectChatGraphFocus(props.graph, citationFocusId);
            citationProjection.value = projection;
            const rect = cytoscapeHost.value.getBoundingClientRect();
            const refreshedLayout = layoutCitationGraph(
                projection.graph,
                rect.width,
                rect.height,
                citationFocusId,
            );
            const depthById = new Map(refreshedLayout.nodes.map((node) => [node.id, node.focusDepth]));
            graphView.nodes().forEach((node) => {
                const degreeSize = 15 + (Math.sqrt(Math.max(1, Number(node.data('degree')) || 0)) * 6);
                const isFocus = node.id() === citationFocusId;
                node.data('focusDepth', depthById.get(node.id()) || 0);
                node.data('size', isFocus ? Math.max(34, degreeSize) : Math.min(18, degreeSize));
                node.toggleClass('citation-focus', isFocus);
                node.toggleClass('citation-neighbor', !isFocus);
            });
        }

        function runCitationLayout(duration = 650) {
            if (!graphView) return;
            const projection = projectChatGraphFocus(props.graph, citationFocusId);
            citationProjection.value = projection;
            const targetLayout = layoutCitationGraph(
                projection.graph,
                graphView.width(),
                graphView.height(),
                citationFocusId,
            );
            const positions = new Map(targetLayout.nodes.map((node) => [node.id, { x: node.x, y: node.y }]));
            const citationLayoutRun = graphView.layout({
                name: 'preset',
                positions: (node) => positions.get(node.id()) || node.position(),
                animate: true,
                animationDuration: duration,
                animationEasing: 'ease-out',
                fit: false,
            });
            citationLayoutRun.one('layoutstop', () => {
                fitCitationAroundFocus();
            });
            citationLayoutRun.run();
        }

        function renderCitationGraph() {
            if (props.variant !== 'citation' || !cytoscapeHost.value) return;
            const previousPositions = new Map();
            graphView?.nodes().forEach((node) => previousPositions.set(node.id(), { ...node.position() }));
            const growthOrigin = previousPositions.get(props.selectedId) || null;
            graphView?.destroy();
            const rect = cytoscapeHost.value.getBoundingClientRect();
            const projection = projectChatGraphFocus(props.graph, citationFocusId || props.selectedId);
            citationProjection.value = projection;
            citationFocusId = projection.focusId;
            const citationLayout = layoutCitationGraph(
                projection.graph,
                rect.width,
                rect.height,
                citationFocusId,
            );
            graphView = cytoscape({
                container: cytoscapeHost.value,
                elements: citationElements(citationLayout, projection.graph, previousPositions, growthOrigin),
                style: citationStyles(),
                layout: { name: 'preset', fit: true, padding: 76 },
                minZoom: 0.22,
                maxZoom: 2.5,
                boxSelectionEnabled: false,
                panningEnabled: true,
                userPanningEnabled: true,
                zoomingEnabled: true,
                userZoomingEnabled: props.userZoomingEnabled,
                autoungrabify: false,
                autounselectify: false,
            });
            graphView.on('tap', 'node', (event) => {
                const nodeId = event.target.id();
                const node = props.graph.nodes.find((item) => item.id === nodeId);
                if (node) {
                    emit('open-entity', node);
                }
            });
            graphView.on('dbltap', 'node', (event) => {
                const nodeId = event.target.id();
                if (nodeId === citationFocusId) return;
                const node = props.graph.nodes.find((item) => item.id === nodeId);
                if (!node) return;
                citationFocusId = nodeId;
                syncCitationFocusData();
                emit('focus-entity', node);
                runCitationLayout(520);
            });
            graphView.on('tap', 'edge', (event) => {
                const relation = props.graph.relations.find((item) => item.id === event.target.id());
                if (relation) {
                    emit('open-relation', relation);
                }
            });
            graphView.on('tap', (event) => {
                if (event.target !== graphView) return;
                graphView.elements().unselect();
                emit('clear-selection');
            });
            graphView.on('mouseover', 'node, edge', (event) => {
                event.target.addClass('is-hovered');
                if (cytoscapeHost.value) cytoscapeHost.value.style.cursor = 'pointer';
            });
            graphView.on('mouseout', 'node, edge', (event) => {
                event.target.removeClass('is-hovered');
                if (cytoscapeHost.value) cytoscapeHost.value.style.cursor = 'grab';
            });
            graphView.on('grab', 'node.citation-focus', (event) => {
                const origin = { ...event.target.position() };
                centerDrag = {
                    id: event.target.id(),
                    origin,
                    positions: new Map(graphView.nodes().filter((node) => node.id() !== event.target.id())
                        .map((node) => [node.id(), { ...node.position() }])),
                };
            });
            graphView.on('drag', 'node.citation-focus', (event) => {
                if (!centerDrag || syncingCenterDrag || centerDrag.id !== event.target.id()) return;
                const position = event.target.position();
                const delta = { x: position.x - centerDrag.origin.x, y: position.y - centerDrag.origin.y };
                syncingCenterDrag = true;
                graphView.batch(() => {
                    for (const [nodeId, origin] of centerDrag.positions) {
                        graphView.getElementById(nodeId).position({
                            x: origin.x + delta.x,
                            y: origin.y + delta.y,
                        });
                    }
                });
                syncingCenterDrag = false;
            });
            graphView.on('free', 'node.citation-focus', () => {
                centerDrag = null;
                syncingCenterDrag = false;
            });
            graphView.ready(() => {
                syncCitationSelection();
                runCitationLayout(720);
            });
        }

        function syncCitationExpansionState() {
            if (!graphView) return;
            graphView.nodes().removeClass('is-expanding is-expanded');
            if (props.expandingId) graphView.getElementById(props.expandingId).addClass('is-expanding');
            for (const id of props.expandedIds) graphView.getElementById(id).addClass('is-expanded');
        }

        function fitGraph() {
            citationSettingsOpen.value = false;
            if (props.variant === 'citation') fitCitationAroundFocus(54);
            else fitRadialGraph(60);
        }

        function relayoutGraph() {
            citationSettingsOpen.value = false;
            if (props.variant === 'citation') runCitationLayout(600);
            else radialForceController?.relayout();
        }

        function switchCitationComponent(delta) {
            const projection = citationProjection.value;
            if (!projection?.pageCount) return;
            const nextPage = Math.max(0, Math.min(
                projection.pageCount - 1,
                projection.pageIndex + delta,
            ));
            if (nextPage === projection.pageIndex) return;
            citationFocusId = projection.pageFocusIds[nextPage];
            citationSettingsOpen.value = false;
            emit('clear-selection');
            renderCitationGraph();
        }

        onMounted(() => {
            resizeObserver = new ResizeObserver(resizeCanvas);
            resizeObserver.observe(shell.value);
            if (props.variant === 'citation') renderCitationGraph();
            else renderRadialGraph();
        });
        watch(() => props.graph, () => {
            if (props.variant === 'citation') renderCitationGraph();
            else {
                syncRadialGraph();
                if (props.centerSelected) focusSelectedRadialNode();
            }
        });
        watch(() => props.selectedId, () => {
            if (props.variant === 'citation') syncCitationSelection();
            else {
                selectedNodeId = props.selectedId || '';
                applyGraphFocus();
                if (props.centerSelected) focusSelectedRadialNode();
            }
        });
        watch(() => [props.expandingId, props.expandedIds], () => {
            if (props.variant === 'citation') syncCitationExpansionState();
        }, { deep: true });
        onBeforeUnmount(() => {
            resizeObserver?.disconnect();
            resizeObserver = null;
            edgePanController?.destroy();
            edgePanController = null;
            radialForceController?.destroy();
            radialForceController = null;
            if (radialViewportFrame !== null) cancelAnimationFrame(radialViewportFrame);
            radialViewportFrame = null;
            if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
            resizeFrame = null;
            graphView?.destroy();
            graphView = null;
            centerDrag = null;
        });

        return {
            shell, cytoscapeHost, citationProjection, citationSettingsOpen,
            fitGraph, relayoutGraph, switchCitationComponent,
        };
    },
    template: `
      <div ref="shell" class="graph-explorer-canvas">
        <div ref="cytoscapeHost" class="graph-cytoscape-host"
             :aria-label="variant === 'citation' ? '引用知识图谱' : '实体关系图谱'"></div>
        <template v-if="variant === 'citation'">
          <div v-if="citationProjection?.pageCount > 1" class="graph-component-pager" aria-label="关系图谱切页">
            <button type="button" title="上一组" aria-label="上一组"
                    :disabled="citationProjection.pageIndex === 0" @click="switchCitationComponent(-1)">
              <local-icon icon="mdi:chevron-left"></local-icon>
            </button>
            <span>{{ citationProjection.pageIndex + 1 }} / {{ citationProjection.pageCount }}</span>
            <button type="button" title="下一组" aria-label="下一组"
                    :disabled="citationProjection.pageIndex + 1 >= citationProjection.pageCount"
                    @click="switchCitationComponent(1)">
              <local-icon icon="mdi:chevron-right"></local-icon>
            </button>
          </div>
        </template>
        <div class="graph-settings">
          <button type="button" class="graph-settings-toggle" title="图谱设置" aria-label="图谱设置"
                  :aria-expanded="citationSettingsOpen" @click="citationSettingsOpen = !citationSettingsOpen">
            <local-icon icon="mdi:cog-sync-outline"></local-icon>
          </button>
          <div v-if="citationSettingsOpen" class="graph-settings-menu">
            <button type="button" @click="fitGraph">适配画布</button>
            <button type="button" @click="relayoutGraph">重新布局</button>
          </div>
        </div>
      </div>
    `,
};
