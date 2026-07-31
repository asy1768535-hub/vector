import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import cytoscape from 'cytoscape';

import { layoutCitationGraph } from '../chat_graph_layout.js';
import { chatGraphDegrees, projectChatGraphFocus } from '../chat_graph_exploration.js';
import { hitTestGraphLayout, layoutGraphRadially } from '../graph_exploration_ui.js';

const NODE_COLORS = {
    0: { fill: '#176b57', stroke: '#0f5545', text: '#ffffff' },
    1: { fill: '#e8f4ef', stroke: '#4f9581', text: '#163d34' },
    2: { fill: '#fff4d8', stroke: '#bd8730', text: '#5f4317' },
};

function clippedLabel(value, limit = 12) {
    const label = typeof value === 'string' ? value : '';
    return label.length > limit ? `${label.slice(0, limit - 1)}…` : label;
}

function nodeMark(value) {
    const label = typeof value === 'string' ? value.trim() : '';
    return label.slice(0, 3) || '实体';
}

export default {
    props: {
        graph: { type: Object, required: true },
        selectedId: { type: String, default: '' },
        variant: { type: String, default: 'radial' },
        expandingId: { type: String, default: '' },
        expandedIds: { type: Array, default: () => [] },
    },
    emits: ['open-entity', 'focus-entity', 'open-relation', 'clear-selection'],
    setup(props, { emit }) {
        const shell = ref(null);
        const canvas = ref(null);
        const cytoscapeHost = ref(null);
        const citationProjection = ref(null);
        const citationSettingsOpen = ref(false);
        const hovered = ref(null);
        let layout = null;
        let resizeObserver = null;
        let graphView = null;
        let citationFocusId = '';
        let centerDrag = null;
        let syncingCenterDrag = false;

        function drawArrowhead(context, relation) {
            const angle = Math.atan2(relation.y2 - relation.y1, relation.x2 - relation.x1);
            const targetGap = 22;
            const arrowLength = 9;
            const x = relation.x2 - Math.cos(angle) * targetGap;
            const y = relation.y2 - Math.sin(angle) * targetGap;
            context.beginPath();
            context.moveTo(x, y);
            context.lineTo(
                x - Math.cos(angle - Math.PI / 6) * arrowLength,
                y - Math.sin(angle - Math.PI / 6) * arrowLength,
            );
            context.lineTo(
                x - Math.cos(angle + Math.PI / 6) * arrowLength,
                y - Math.sin(angle + Math.PI / 6) * arrowLength,
            );
            context.closePath();
            context.fill();
        }

        function draw() {
            if (!canvas.value || !layout) return;
            const context = canvas.value.getContext('2d');
            if (!context) return;
            const ratio = Math.max(1, window.devicePixelRatio || 1);
            context.setTransform(ratio, 0, 0, ratio, 0, 0);
            context.clearRect(0, 0, layout.width, layout.height);
            context.fillStyle = '#ffffff';
            context.fillRect(0, 0, layout.width, layout.height);

            for (const relation of layout.relations) {
                const active = props.selectedId === relation.id || hovered.value?.id === relation.id;
                context.strokeStyle = active ? '#b36512' : '#9aa9a4';
                context.fillStyle = active ? '#b36512' : '#7f908a';
                context.lineWidth = active ? 2.6 : 1.4;
                context.beginPath();
                context.moveTo(relation.x1, relation.y1);
                context.lineTo(relation.x2, relation.y2);
                context.stroke();
                if (relation.direction === 'directed') drawArrowhead(context, relation);
            }

            for (const node of layout.nodes) {
                const palette = NODE_COLORS[node.depth] || NODE_COLORS[2];
                const active = props.selectedId === node.id || hovered.value?.id === node.id;
                context.beginPath();
                context.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
                context.fillStyle = palette.fill;
                context.fill();
                context.lineWidth = active ? 4 : 2;
                context.strokeStyle = active ? '#d58722' : palette.stroke;
                context.stroke();
                context.fillStyle = palette.text;
                context.font = node.depth === 0 ? '600 11px sans-serif' : '600 10px sans-serif';
                context.textAlign = 'center';
                context.textBaseline = 'middle';
                context.fillText(nodeMark(node.label), node.x, node.y, node.radius * 1.55);
                context.fillStyle = '#29443d';
                context.font = '500 10px sans-serif';
                context.textBaseline = 'top';
                context.fillText(
                    clippedLabel(node.label, 11),
                    node.x,
                    node.y + node.radius + 6,
                    126,
                );
            }
        }

        function resizeCanvas() {
            if (props.variant === 'citation') {
                graphView?.resize();
                if (graphView) fitCitationAroundFocus(54);
                return;
            }
            if (!shell.value || !canvas.value) return;
            const rect = shell.value.getBoundingClientRect();
            const width = Math.max(240, Math.round(rect.width));
            const height = Math.max(220, Math.round(rect.height));
            const ratio = Math.max(1, window.devicePixelRatio || 1);
            canvas.value.width = Math.round(width * ratio);
            canvas.value.height = Math.round(height * ratio);
            layout = layoutGraphRadially(props.graph, width, height);
            draw();
        }

        function eventTarget(event) {
            if (props.variant === 'citation') return null;
            if (!canvas.value || !layout) return null;
            const rect = canvas.value.getBoundingClientRect();
            if (!rect.width || !rect.height) return null;
            const x = (event.clientX - rect.left) * (layout.width / rect.width);
            const y = (event.clientY - rect.top) * (layout.height / rect.height);
            return hitTestGraphLayout(layout, x, y);
        }

        function onPointerMove(event) {
            if (props.variant === 'citation') return;
            const next = eventTarget(event);
            const previousKey = hovered.value ? `${hovered.value.kind}:${hovered.value.id}` : '';
            const nextKey = next ? `${next.kind}:${next.id}` : '';
            if (previousKey === nextKey) return;
            hovered.value = next;
            if (canvas.value) canvas.value.style.cursor = next ? 'pointer' : 'default';
            draw();
        }

        function onPointerLeave() {
            if (props.variant === 'citation') return;
            if (!hovered.value) return;
            hovered.value = null;
            if (canvas.value) canvas.value.style.cursor = 'default';
            draw();
        }

        function onClick(event) {
            if (props.variant === 'citation') return;
            const target = eventTarget(event);
            if (!target) return;
            if (target.kind === 'node') {
                const node = props.graph.nodes.find((item) => item.id === target.id);
                if (node) emit('open-entity', node);
                return;
            }
            const relation = props.graph.relations.find((item) => item.id === target.id);
            if (relation) emit('open-relation', relation);
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
                wheelSensitivity: 0.14,
                boxSelectionEnabled: false,
                panningEnabled: true,
                userPanningEnabled: true,
                zoomingEnabled: true,
                userZoomingEnabled: true,
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

        function fitCitation() {
            citationSettingsOpen.value = false;
            fitCitationAroundFocus(54);
        }

        function relayoutCitation() {
            citationSettingsOpen.value = false;
            runCitationLayout(600);
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
            else resizeCanvas();
        });
        watch(() => props.graph, () => {
            if (props.variant === 'citation') renderCitationGraph();
            else resizeCanvas();
        });
        watch(() => props.selectedId, () => {
            if (props.variant === 'citation') syncCitationSelection();
            else resizeCanvas();
        });
        watch(() => [props.expandingId, props.expandedIds], () => {
            if (props.variant === 'citation') syncCitationExpansionState();
        }, { deep: true });
        onBeforeUnmount(() => {
            resizeObserver?.disconnect();
            resizeObserver = null;
            graphView?.destroy();
            graphView = null;
            layout = null;
        });

        return {
            shell, canvas, cytoscapeHost, citationProjection, citationSettingsOpen,
            onPointerMove, onPointerLeave, onClick,
            fitCitation, relayoutCitation, switchCitationComponent,
        };
    },
    template: `
      <div ref="shell" class="graph-explorer-canvas">
        <template v-if="variant === 'citation'">
          <div ref="cytoscapeHost" class="graph-cytoscape-host" aria-label="引用知识图谱"></div>
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
          <div class="graph-settings">
            <button type="button" class="graph-settings-toggle" title="图谱设置" aria-label="图谱设置"
                    :aria-expanded="citationSettingsOpen" @click="citationSettingsOpen = !citationSettingsOpen">
              <local-icon icon="mdi:cog-sync-outline"></local-icon>
            </button>
            <div v-if="citationSettingsOpen" class="graph-settings-menu">
              <button type="button" @click="fitCitation">适配画布</button>
              <button type="button" @click="relayoutCitation">重新布局</button>
            </div>
          </div>
        </template>
        <canvas v-else ref="canvas" aria-hidden="true"
                @pointermove="onPointerMove" @pointerleave="onPointerLeave" @click="onClick"></canvas>
      </div>
    `,
};
