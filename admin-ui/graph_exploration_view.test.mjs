import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import cytoscape from './vendor/cytoscape.esm.min.mjs';
import * as d3Force from './vendor/d3-force.bundle.mjs';

import {
    createEdgePanController,
    createRadialForceController,
} from './src/radial_force_controller.js';

const explorer = readFileSync(new URL('./src/components/GraphExplorer.js', import.meta.url), 'utf8');
const browser = readFileSync(new URL('./src/components/GraphKnowledgeBrowser.js', import.meta.url), 'utf8');
const canvas = readFileSync(new URL('./src/components/GraphCanvas.js', import.meta.url), 'utf8');
const parent = readFileSync(new URL('./src/views/GraphGovernance.js', import.meta.url), 'utf8');
const helper = readFileSync(new URL('./src/graph_governance_ui.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('renders only browse and publications at the top level', () => {
    assert.equal(parent.includes('name="explore"'), false);
    assert.equal((parent.match(/<el-tab-pane /g) || []).length, 2);
    assert.ok(parent.includes('name="browse"'));
    assert.ok(parent.includes('name="publications"'));
    assert.ok(parent.includes('<graph-knowledge-browser'));
    assert.ok(parent.includes('<graph-explorer'));
    assert.ok(parent.includes('entity-preview'));
});

test('normalizes legacy exploration and entity/relation routes into browse', () => {
    assert.ok(helper.includes("const LEGACY_BROWSE_TABS = new Set(['explore', 'entities', 'relations'])"));
    assert.ok(helper.includes("? 'browse'"));
    assert.ok(explorer.includes('selectedEntityId'));
    assert.ok(browser.includes('selectedEntityId'));
    assert.ok(parent.includes('graphRouteQuery'));
});

test('uses one selected entity for directory selection, stable highlighting and free detail traversal', () => {
    for (const token of [
        "emit('select-entity', row)",
        ':selected-entity-id="selectedEntityId"',
        ':selected-seed="selected"',
        ':selected-id="selectedEntityId"',
        'selectedSeed',
        'entityPreview',
        'loadTraversal',
        ':center-selected="false"',
    ]) assert.ok(`${browser}\n${explorer}\n${canvas}`.includes(token), `missing selection token ${token}`);
});

test('forwards node hits to the entity dialog and relation hits to the existing inspector', () => {
    assert.ok(explorer.includes("emit('open-entity'"));
    assert.ok(explorer.includes("emit('open-relation'"));
    assert.ok(canvas.includes("emit('open-entity', node)"));
    assert.ok(canvas.includes("emit('open-relation', relation)"));
    assert.ok(parent.includes('@open-entity="loadEntityDetail($event, false)"'));
    assert.ok(parent.includes('@open-relation="loadRelationDetail"'));
    assert.ok(parent.includes('class="graph-entity-dialog"'));
    assert.ok(parent.includes('graph-detail-inspector'));

    const entityPreview = parent.slice(
        parent.indexOf('<section class="graph-entity-network">'),
        parent.indexOf('<section class="graph-detail-section">', parent.indexOf('<section class="graph-entity-network">')),
    );
    assert.doesNotMatch(entityPreview, /@open-relation=/);
});

test('main-canvas selection does not switch range, reload the graph, recenter or issue traversal', () => {
    assert.equal(explorer.includes('stopSelectionWatch'), false);
    assert.equal(explorer.includes("range.value = props.selectedSeed ? 'related' : 'panorama'"), false);
    assert.equal(explorer.includes('relatedRequestKey'), false);
    const selectedWatch = canvas.slice(
        canvas.indexOf('watch(() => props.selectedId'),
        canvas.indexOf('watch(() => [props.expandingId', canvas.indexOf('watch(() => props.selectedId')),
    );
    assert.match(selectedWatch, /applyGraphFocus\(\)/);
    assert.match(selectedWatch, /if \(props\.centerSelected\) focusSelectedRadialNode\(\)/);
    assert.doesNotMatch(selectedWatch, /syncRadialGraph|loadTraversal/);
    assert.ok(parent.includes('entity-preview'));
    assert.equal(parent.includes('showSelectedRelations'), false);
    assert.ok(explorer.includes(':constrain-to-viewport="false"'));
    assert.ok(explorer.includes(':user-zooming-enabled="true"'));
    assert.ok(explorer.includes('previewExpanded'));
    assert.ok(explorer.includes('mdi:fullscreen'));
    assert.ok(css.includes('height: clamp(360px, 58vh, 680px);'));
    assert.ok(canvas.includes('constrainToViewport: { type: Boolean, default: false }'));
});

test('entity preview loads only bounded traversal, caches validated results and fences stale seeds', () => {
    const loadScope = explorer.slice(
        explorer.indexOf('async function loadScope'),
        explorer.indexOf('async function changeRange'),
    );
    assert.match(loadScope, /if \(!props\.entityPreview\)[\s\S]*loadOverview\(\)/);
    assert.match(loadScope, /await loadTraversal\(props\.selectedSeed\)/);
    assert.ok(explorer.includes('TRAVERSAL_CACHE_LIMIT'));
    assert.ok(explorer.includes('traversalIdentityKey'));
    for (const token of [
        'organizationId:', 'libraryId:', 'librarySlug:', 'publicationId:',
        'ontologyVersionId:', 'entityId:', 'refreshKey,',
    ]) assert.ok(explorer.includes(token), `missing preview cache identity ${token}`);
    assert.ok(explorer.indexOf('graphTraversalResponseMatches(data, identity)')
        < explorer.indexOf('cacheTraversal(cacheKey, data)'));
    assert.match(explorer, /if \(seq !== requestSeq/);
    assert.ok(parent.includes('v-if="entityDetail.data.relation_count > 0"'));
    assert.ok(parent.includes('该实体暂无关系，无法显示局部关系图'));
});

test('keeps directory requests independent from panorama failures and fences stale scopes', () => {
    for (const token of [
        'api.searchGraphEntities',
        'directorySeq',
        'scopeKey',
        'requestSeq',
        'Promise.allSettled',
        'graph.partialError',
        'onBeforeUnmount',
        'stopScopeWatch',
    ]) assert.ok(`${browser}\n${explorer}`.includes(token), `missing stale-scope token ${token}`);
    assert.ok(explorer.includes('if (!panoramas.length)'));
    assert.ok(explorer.includes('graph.error'));
    assert.ok(browser.includes('暂无可浏览的实体'));
});

test('explains that a degraded publication is being refreshed automatically', () => {
    assert.ok(explorer.includes("图谱正在根据源文档变更自动清理并刷新，请稍后重试。"));
    assert.ok(explorer.includes("kind: 'publication_unavailable'"));
});

test('bounds full panorama loading before fetching Publication items', () => {
    for (const token of [
        'PANORAMA_ENTITY_LIMIT',
        'PANORAMA_RELATION_LIMIT',
        'PANORAMA_TOTAL_ENTITY_LIMIT',
        'PANORAMA_TOTAL_RELATION_LIMIT',
        'publication.entity_count',
        'publication.relation_count',
        "panoramaFailure('too_large')",
        'listAllItems',
        'page_size: ITEM_PAGE_SIZE',
        'loadTraversal',
        'maxNodes: 80',
        'maxRelations: 120',
    ]) assert.ok(explorer.includes(token), `missing graph bound ${token}`);
});

test('keeps permission, empty, partial-failure and isolated-node states visible', () => {
    for (const token of [
        ':can-write="canWrite"',
        'v-if="canWrite"',
        '暂无可浏览的实体',
        'graph.partialError',
        '显示无关系实体',
        'type="warning"',
    ]) assert.ok(`${browser}\n${explorer}\n${parent}`.includes(token), `missing state ${token}`);
    assert.ok(explorer.includes('{{ summary.nodes }} 个实体 · {{ summary.relations }} 条关系'));
    assert.ok(explorer.includes('显示无关系实体（{{ graph.data.isolated_count }}）'));
});

test('provides a stable canvas-first desktop workbench with collapsible directory and inspector', () => {
    for (const token of [
        '.graph-browser',
        '.graph-browser-directory',
        '.graph-browser-canvas',
        '.graph-explorer-range',
        '.graph-detail-inspector',
        '.graph-browser-canvas .graph-explorer-canvas',
        '@media screen and (max-width: 899px)',
        '@media screen and (max-width: 520px)',
    ]) assert.ok(css.includes(token), `missing graph workbench CSS ${token}`);
    assert.match(css, /\.graph-browser\s*\{[\s\S]*grid-template-columns:\s*280px minmax\(0, 1fr\)/);
    assert.match(css, /\.graph-workspace\.has-inspector\s*\{[\s\S]*clamp\(360px, 28vw, 420px\)/);
    assert.match(css, /\.graph-browser\.is-directory-collapsed\s*\{[\s\S]*44px minmax\(0, 1fr\)/);
});

function radialFixture(extraNodes = [], extraRelations = []) {
    return {
        nodes: [
            { id: 'a', canonical_name: 'Alpha' },
            { id: 'b', canonical_name: 'Beta' },
            ...extraNodes,
        ],
        relations: [
            {
                id: 'ab', source_entity_id: 'a', target_entity_id: 'b',
                relation_type: { label: 'links', direction: 'undirected' },
            },
            ...extraRelations,
        ],
    };
}

function frameHarness() {
    let nextId = 0;
    const callbacks = new Map();
    return {
        request(callback) {
            nextId += 1;
            callbacks.set(nextId, callback);
            return nextId;
        },
        cancel(id) { callbacks.delete(id); },
        flush() {
            const queued = [...callbacks.values()];
            callbacks.clear();
            for (const callback of queued) callback();
        },
    };
}

function headlessGraph() {
    return cytoscape({
        headless: true,
        styleEnabled: true,
        elements: [],
        layout: { name: 'preset' },
        minZoom: 0.15,
        maxZoom: 4,
    });
}

function createBehaviorController(graphView) {
    const frames = frameHarness();
    const controller = createRadialForceController({
        graphView,
        forceApi: d3Force,
        requestFrame: frames.request,
        cancelFrame: frames.cancel,
    });
    return { controller, frames };
}

test('keeps constrained detail graph nodes inside the viewport boundary', () => {
    const graphView = headlessGraph();
    const frames = frameHarness();
    const boundedController = createRadialForceController({
        graphView,
        forceApi: d3Force,
        bounds: () => ({ x1: 0, y1: 0, x2: 300, y2: 180 }),
        requestFrame: frames.request,
        cancelFrame: frames.cancel,
    });
    boundedController.syncGraph(radialFixture(), 'a');
    boundedController.simulation.stop();
    assert.equal(boundedController.dragNode('b', { x: 999, y: -999 }), true);
    const node = boundedController.simulationNodesById.get('b');
    const graphNode = graphView.getElementById('b');
    assert.ok(node.fx < 300 && node.fx > 0);
    assert.ok(node.fy < 180 && node.fy > 0);
    assert.ok(graphNode.position('x') < 300 && graphNode.position('x') > 0);
    assert.ok(graphNode.position('y') < 180 && graphNode.position('y') > 0);
    assert.equal(boundedController.freeNode('b', { x: -999, y: 999 }), true);
    assert.ok(node.x < 300 && node.x > 0);
    assert.ok(node.y < 180 && node.y > 0);
    assert.ok(graphNode.position('x') < 300 && graphNode.position('x') > 0);
    assert.ok(graphNode.position('y') < 180 && graphNode.position('y') > 0);
    boundedController.destroy();
    graphView.destroy();
});

test('keeps Cytoscape dragging in model coordinates and releases nodes naturally', () => {
    const graphView = headlessGraph();
    graphView.zoom(2);
    graphView.pan({ x: 83, y: -41 });
    const { controller } = createBehaviorController(graphView);
    controller.syncGraph(radialFixture(), 'a');
    controller.simulation.stop();
    const simulationNode = controller.simulationNodesById.get('b');
    const grabPosition = { x: simulationNode.x, y: simulationNode.y };
    assert.equal(controller.grabNode('b', grabPosition), true);
    assert.deepEqual({ x: simulationNode.fx, y: simulationNode.fy }, grabPosition);
    assert.equal(controller.simulation.alphaTarget(), 0.2);
    const modelDragPosition = { x: 312.5, y: -118.25 };
    assert.equal(controller.dragNode('b', modelDragPosition), true);
    assert.deepEqual({ x: simulationNode.fx, y: simulationNode.fy }, modelDragPosition);
    graphView.getElementById('b').position(modelDragPosition);
    assert.equal(controller.freeNode('b', graphView.getElementById('b').position()), true);
    assert.equal(simulationNode.fx, null);
    assert.equal(simulationNode.fy, null);
    assert.equal(controller.simulation.alphaTarget(), 0);
    controller.destroy();
    graphView.destroy();
});

test('preserves simulation identity and viewport across data-only refreshes', () => {
    const graphView = headlessGraph();
    graphView.zoom(1.7);
    graphView.pan({ x: 54, y: 37 });
    const { controller } = createBehaviorController(graphView);
    controller.syncGraph(radialFixture(), 'a');
    controller.simulation.stop();
    const originalNode = controller.simulationNodesById.get('a');
    originalNode.x = 91;
    originalNode.y = 73;
    const originalPan = { ...graphView.pan() };
    const originalZoom = graphView.zoom();
    const dataOnlyGraph = radialFixture();
    dataOnlyGraph.nodes[0] = { ...dataOnlyGraph.nodes[0], canonical_name: 'Alpha renamed' };
    const dataResult = controller.syncGraph(dataOnlyGraph, 'a');
    assert.equal(dataResult.topologyChanged, false);
    assert.equal(controller.simulationNodesById.get('a'), originalNode);
    assert.deepEqual({ x: originalNode.x, y: originalNode.y }, { x: 91, y: 73 });
    assert.deepEqual(graphView.pan(), originalPan);
    assert.equal(graphView.zoom(), originalZoom);
    controller.destroy();
    graphView.destroy();
});

test('resizes force targets without moving nodes or reheating the simulation', () => {
    const graphView = headlessGraph();
    const { controller } = createBehaviorController(graphView);
    controller.syncGraph(radialFixture(), 'a');
    controller.simulation.stop().alpha(0);
    const simulationNode = controller.simulationNodesById.get('a');
    const beforePosition = { x: simulationNode.x, y: simulationNode.y };
    const xForce = controller.simulation.force('x');
    const beforeTarget = xForce.x()(simulationNode);
    graphView.panBy({ x: 120, y: 0 });
    controller.resize();
    assert.notEqual(xForce.x()(simulationNode), beforeTarget);
    assert.deepEqual({ x: simulationNode.x, y: simulationNode.y }, beforePosition);
    assert.equal(controller.simulation.alpha(), 0);
    assert.equal(controller.simulation.alphaTarget(), 0);
    controller.destroy();
    graphView.destroy();
});

test('pans from an edge gesture without moving model nodes', () => {
    const graphView = headlessGraph();
    graphView.add([
        { group: 'nodes', data: { id: 'a' }, position: { x: 30, y: 40 } },
        { group: 'nodes', data: { id: 'b' }, position: { x: 180, y: 130 } },
        { group: 'edges', data: { id: 'ab', source: 'a', target: 'b' } },
    ]);
    const beforePositions = graphView.nodes().map((node) => ({ id: node.id(), ...node.position() }));
    const beforePan = { ...graphView.pan() };
    const edgePan = createEdgePanController(graphView, 5);
    assert.equal(edgePan.start('ab', { x: 20, y: 20 }), true);
    assert.equal(edgePan.move({ x: 45, y: 32 }), true);
    assert.deepEqual(edgePan.finish(), { edgeId: 'ab', panned: true });
    assert.deepEqual(graphView.pan(), { x: beforePan.x + 25, y: beforePan.y + 12 });
    assert.deepEqual(
        graphView.nodes().map((node) => ({ id: node.id(), ...node.position() })),
        beforePositions,
    );
    edgePan.destroy();
    graphView.destroy();
});
