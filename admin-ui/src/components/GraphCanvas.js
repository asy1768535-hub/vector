import { onBeforeUnmount, onMounted, ref, watch } from 'vue';

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
    },
    emits: ['open-entity', 'open-relation'],
    setup(props, { emit }) {
        const shell = ref(null);
        const canvas = ref(null);
        const hovered = ref(null);
        let layout = null;
        let resizeObserver = null;

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
            if (!canvas.value || !layout) return null;
            const rect = canvas.value.getBoundingClientRect();
            if (!rect.width || !rect.height) return null;
            const x = (event.clientX - rect.left) * (layout.width / rect.width);
            const y = (event.clientY - rect.top) * (layout.height / rect.height);
            return hitTestGraphLayout(layout, x, y);
        }

        function onPointerMove(event) {
            const next = eventTarget(event);
            const previousKey = hovered.value ? `${hovered.value.kind}:${hovered.value.id}` : '';
            const nextKey = next ? `${next.kind}:${next.id}` : '';
            if (previousKey === nextKey) return;
            hovered.value = next;
            if (canvas.value) canvas.value.style.cursor = next ? 'pointer' : 'default';
            draw();
        }

        function onPointerLeave() {
            if (!hovered.value) return;
            hovered.value = null;
            if (canvas.value) canvas.value.style.cursor = 'default';
            draw();
        }

        function onClick(event) {
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

        onMounted(() => {
            resizeObserver = new ResizeObserver(resizeCanvas);
            resizeObserver.observe(shell.value);
            resizeCanvas();
        });
        watch(() => [props.graph, props.selectedId], resizeCanvas);
        onBeforeUnmount(() => {
            resizeObserver?.disconnect();
            resizeObserver = null;
            layout = null;
        });

        return { shell, canvas, onPointerMove, onPointerLeave, onClick };
    },
    template: `
      <div ref="shell" class="graph-explorer-canvas">
        <canvas ref="canvas" aria-hidden="true"
                @pointermove="onPointerMove" @pointerleave="onPointerLeave" @click="onClick"></canvas>
      </div>
    `,
};
