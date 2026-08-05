import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const browser = fs.readFileSync(
    new URL('./src/components/GraphKnowledgeBrowser.js', import.meta.url),
    'utf8',
);
const explorer = fs.readFileSync(
    new URL('./src/components/GraphExplorer.js', import.meta.url),
    'utf8',
);
const parent = fs.readFileSync(
    new URL('./src/views/GraphGovernance.js', import.meta.url),
    'utf8',
);
const helper = fs.readFileSync(
    new URL('./src/graph_governance_ui.js', import.meta.url),
    'utf8',
);
const css = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const canvas = fs.readFileSync(
    new URL('./src/components/GraphCanvas.js', import.meta.url),
    'utf8',
);
const radialForce = fs.readFileSync(
    new URL('./src/radial_force_controller.js', import.meta.url),
    'utf8',
);

test('uses one graph workbench with directory and canvas, not nested page modes', () => {
    for (const token of [
        'class="graph-browser"',
        'class="graph-browser-directory"',
        'class="graph-browser-canvas"',
        'selectedEntityId',
        ':selected-seed="selected"',
        'class="graph-browser-entity-row"',
        "emit('select-entity', row)",
        "emit('open-entity', row)",
    ]) assert.ok(browser.includes(token), `missing unified browser token ${token}`);
    for (const removed of [
        'graph-browser-mode-switch',
        'graph-browser-content',
        'graph-browser-map',
        "const mode = ref('directory')",
        'graphEntityDetailMatches',
        'api.getGraphEntity',
        "emit('open-evidence'",
    ]) assert.equal(browser.includes(removed), false, `duplicate browser token ${removed}`);
    assert.ok(parent.includes('<graph-knowledge-browser'));
    assert.ok(parent.includes('name="browse"'));
    assert.ok(parent.includes('name="publications"'));
    assert.equal((parent.match(/<el-tab-pane /g) || []).length, 2);
    assert.equal(parent.includes('name="explore"'), false);
    assert.ok(parent.includes('class="graph-scope-inline"'));
    assert.ok(parent.includes('graph-library-select'));
    assert.equal(parent.includes('class="graph-scope-band"'), false);
    assert.equal(browser.includes('graph-browser-eyebrow'), false);
    assert.equal(browser.includes('{{ directory.items.length }}'), false);
    assert.equal(browser.includes('100 个实体'), false);
    assert.equal(browser.includes('知识库名称'), false);
    assert.equal(browser.includes('mdi:circle-medium'), false);
    assert.equal(css.includes('.graph-browser-entity-row > local-icon'), false);
});

test('shares selected entity state and preserves existing detail drawer events', () => {
    for (const token of [
        ':selected-entity-id="selectedEntityId"',
        ':selected-id="selectedEntityId"',
        'selectedSeed',
        'loadTraversal',
        'graphTraversalResponseMatches',
        'expected_publication_id',
        'include_evidence_locators: true',
        "emit('open-entity'",
        "emit('open-relation'",
    ]) assert.ok(`${browser}\n${explorer}`.includes(token), `missing selection contract ${token}`);
    assert.ok(browser.includes("emit('select-entity', node)"));
    assert.ok(parent.includes('@open-entity="loadEntityDetail($event, false)"'));
    assert.ok(parent.includes('@open-relation="loadRelationDetail"'));
    assert.ok(parent.includes('scope.entityId'));
    assert.equal(browser.includes('directory.items.unshift'), false);
});

test('loads the directory independently and bounds panorama work by Publication metadata', () => {
    for (const token of [
        'directorySeq',
        'scopeKey',
        'api.searchGraphEntities',
        'graphEntityPageMatches',
        'explorationSeedSearchRowValid',
        'PANORAMA_ENTITY_LIMIT',
        'PANORAMA_RELATION_LIMIT',
        'PANORAMA_TOTAL_ENTITY_LIMIT',
        'PANORAMA_TOTAL_RELATION_LIMIT',
        'totalEntities',
        'totalRelations',
        'publication.entity_count',
        'publication.relation_count',
        "panoramaFailure('too_large')",
        'Promise.allSettled',
        'requestSeq',
    ]) assert.ok(`${browser}\n${explorer}`.includes(token), `missing bounded load token ${token}`);
    assert.ok(explorer.includes('loadPublicationPanorama'));
    assert.ok(explorer.includes('loadTraversal'));
    assert.ok(explorer.includes('graph.partialError'));
    assert.equal(browser.includes('api.listDocuments'), false);
    assert.equal(browser.includes('api.listGraphExtractions'), false);
});

test('retains permission-gated create operations in the workbench', () => {
    for (const token of [
        "emit('create-entity')",
        "emit('create-relation'",
        'graph-browser-directory-actions',
        '新增实体',
        '新增关系',
    ]) assert.ok(browser.includes(token), `missing workbench action ${token}`);
    assert.ok(parent.includes(':can-write="canWrite"'));
    assert.ok(parent.includes('@create-entity="openFactDialog(\'entity\')"'));
    assert.ok(parent.includes('@create-relation="openFactDialog(\'relation\', $event)"'));
    assert.equal(browser.includes("emit('open-import')"), false);
    assert.equal(parent.includes('@open-import='), false);
    assert.equal(parent.includes('openGraphImport'), false);
});

test('rejects unsafe payload rendering and keeps scope changes from leaking old responses', () => {
    const sources = `${browser}\n${explorer}`;
    for (const forbidden of [
        'v-html', 'console.', 'source_text', 'raw_response', 'storage_url',
        'object_key', 'prompt', 'api_key', 'window.open',
    ]) assert.equal(sources.includes(forbidden), false, `forbidden token ${forbidden}`);
    assert.doesNotMatch(browser, /style="[^"]*"/);
    assert.match(browser, /if \(seq !== directorySeq/);
    assert.match(browser, /identity\.scopeKey !== scopeKey/);
    assert.match(explorer, /if \(seq !== requestSeq/);
    assert.match(explorer, /stopScopeWatch\(\)/);
});

test('provides a stable canvas-first layout with collapsible directory and inspector', () => {
    for (const token of [
        '.graph-browser',
        '.graph-browser-directory',
        '.graph-browser-canvas',
        '.graph-browser-entity-row.is-selected',
        '.graph-explorer-range',
        '.graph-explorer-summary',
        '.graph-detail-inspector',
        '@media screen and (max-width: 899px)',
        '@media screen and (max-width: 520px)',
    ]) assert.ok(css.includes(token), `missing unified graph CSS ${token}`);
    assert.match(css, /\.graph-browser\s*\{[\s\S]*grid-template-columns:\s*280px minmax\(0, 1fr\)/);
    assert.match(css, /\.graph-workspace\.has-inspector\s*\{[\s\S]*clamp\(360px, 28vw, 420px\)/);
    assert.match(css, /\.graph-browser\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test('keeps main selection request-free and loads local relations only inside the entity dialog', () => {
    assert.equal(explorer.includes('stopSelectionWatch'), false);
    assert.equal(explorer.includes('relatedRequestKey'), false);
    assert.equal(browser.includes('related-request-key'), false);
    assert.equal(parent.includes('showSelectedRelations'), false);
    assert.ok(parent.includes('class="graph-entity-dialog"'));
    assert.ok(parent.includes('entity-preview'));
    assert.ok(parent.includes('该实体暂无关系，无法显示局部关系图'));
});

test('closing details does not reload the entity directory or panorama', () => {
    const directoryWatch = browser.slice(
        browser.indexOf('const stopScopeWatch'),
        browser.indexOf('onBeforeUnmount(() =>'),
    );
    assert.match(directoryWatch, /props\.organizationId/);
    assert.match(directoryWatch, /JSON\.stringify\(props\.librarySlugs\)/);
    assert.match(directoryWatch, /props\.refreshKey/);
    assert.doesNotMatch(directoryWatch, /selectedEntityId|selectedEntity|inspectorOpen/);

    const mainGraphWatch = explorer.slice(
        explorer.indexOf('const stopScopeWatch'),
        explorer.indexOf('onBeforeUnmount(() =>'),
    );
    assert.doesNotMatch(mainGraphWatch, /selectedEntityId/);
    assert.match(mainGraphWatch, /props\.entityPreview\s*\?/);
});

test('uses an entity dialog without compressing the main canvas and keeps honest directory totals', () => {
    assert.ok(parent.includes("'has-inspector': scope.tab === 'browse' && relationDetail.open"));
    assert.ok(parent.includes('<el-dialog v-model="entityDetail.open"'));
    assert.ok(parent.includes('class="graph-entity-dialog"'));
    assert.equal(parent.includes('<Teleport'), false);
    assert.ok(browser.includes('directory.nextCursor'));
    assert.ok(browser.includes('cursor: requestedCursor || undefined'));
    assert.ok(browser.includes('当前仅显示前'));
    assert.ok(browser.includes('继续加载'));
    assert.ok(browser.includes("item.normalized_name !== item.canonical_name"));
    assert.ok(browser.includes("item.publication_state !== 'published'"));
});

test('keeps relationship-graph drag in Cytoscape model coordinates', () => {
    assert.ok(canvas.includes("graphView.on('grab', 'node'"));
    assert.ok(canvas.includes("graphView.on('drag', 'node'"));
    assert.ok(canvas.includes("graphView.on('dragfree', 'node'"));
    assert.ok(canvas.includes("graphView.on('free', 'node'"));
    assert.ok(canvas.includes('createRadialForceController'));
    assert.ok(canvas.includes('hasRadialLayoutViewport'));
    assert.ok(radialForce.includes('simulation.alphaTarget(0.2).restart()'));
    assert.ok(canvas.includes('selectedNodeId'));
    assert.ok(canvas.includes('hoveredNodeId'));
    assert.ok(canvas.includes('draggingNodeId'));
    assert.ok(canvas.includes('userPanningEnabled: true'));
    assert.ok(canvas.includes('userZoomingEnabled: props.userZoomingEnabled'));
    assert.equal(canvas.includes('event.clientX'), false);
    assert.equal(canvas.includes('event.clientY'), false);
    assert.equal(canvas.includes('releaseDraggedNode'), false);
    const grabBody = canvas.slice(
        canvas.indexOf("graphView.on('grab', 'node'"),
        canvas.indexOf("graphView.on('drag', 'node'"),
    );
    assert.match(grabBody, /const position = event\.target\.position\(\)/);
    assert.match(grabBody, /radialForceController\?\.grabNode\(draggingNodeId, position\)/);
    assert.doesNotMatch(grabBody, /fitRadialGraph|\.fit\(|\.center\(|renderRadialGraph/);
    const dragBody = canvas.slice(
        canvas.indexOf("graphView.on('drag', 'node'"),
        canvas.indexOf("graphView.on('dragfree', 'node'"),
    );
    assert.match(dragBody, /radialForceController\?\.dragNode\(event\.target\.id\(\), event\.position\)/);
    assert.doesNotMatch(dragBody, /fitRadialGraph|\.fit\(|\.center\(|renderRadialGraph|\.position\(\s*\{/);
    assert.equal(canvas.includes('followRatio'), false);
    assert.equal(canvas.includes('expandDraggedComponent'), false);
});

test('persists dragged model positions and only relayouts on explicit request', () => {
    assert.match(canvas, /radialForceController\?\.freeNode\(id, event\.target\.position\(\)\)/);
    assert.match(radialForce, /node\.fx = null/);
    assert.match(radialForce, /node\.fy = null/);
    assert.match(radialForce, /simulation\.alphaTarget\(0\)/);
    assert.match(radialForce, /const simulationNodesById = new Map\(\)/);
    assert.match(radialForce, /const simulationLinksById = new Map\(\)/);
    assert.match(canvas, /else radialForceController\?\.relayout\(\)/);
    assert.doesNotMatch(canvas, /runForceLayout|radialPositions/);
});

test('keeps node detail, blank-canvas clearing and drag tap suppression', () => {
    assert.match(canvas, /graphView\.on\('tap', 'node'/);
    assert.match(canvas, /emit\('open-entity', node\)/);
    assert.match(canvas, /if \(event\.target !== graphView\) return;[\s\S]*?emit\('clear-selection'\)/);
    assert.match(canvas, /renderedDistance > 5\) didDrag = true/);
    assert.match(canvas, /Date\.now\(\) \+ 220/);
});

test('keeps detail previews in a free-moving viewport while the main canvas remains stationary', () => {
    const animateBody = canvas.slice(canvas.indexOf('function animateToNode'), canvas.indexOf('function focusRadialNode'));
    assert.match(animateBody, /graphView\.animate\(\{ center: \{ eles: node \} \}/);
    assert.doesNotMatch(animateBody, /distance|0\.16/);

    const tapBody = canvas.slice(
        canvas.indexOf("graphView.on('tap', 'node'"),
        canvas.indexOf("graphView.on('tap', 'edge'"),
    );
    assert.ok(tapBody.indexOf("emit('open-entity', node)") < tapBody.indexOf('focusRadialNode(event.target.id())'));
    assert.match(tapBody, /if \(props\.centerSelected\) focusRadialNode/);
    assert.match(tapBody, /if \(props\.centerSelected\) \{[\s\S]*setPrimaryNode/);
    assert.match(canvas, /function focusRadialNode[\s\S]*scheduleRadialViewportAction/);
    const selectedWatch = canvas.slice(
        canvas.indexOf('watch(() => props.selectedId'),
        canvas.indexOf('watch(() => [props.expandingId', canvas.indexOf('watch(() => props.selectedId')),
    );
    assert.doesNotMatch(selectedWatch, /syncRadialGraph/);
    assert.match(selectedWatch, /if \(props\.centerSelected\) focusSelectedRadialNode\(\)/);
    assert.ok(explorer.includes(':center-selected="false"'));
    assert.ok(explorer.includes(':user-zooming-enabled="!entityPreview"'));
    assert.ok(explorer.includes(':constrain-to-viewport="entityPreview"'));
    assert.match(canvas, /userZoomingEnabled:\s*\{ type: Boolean, default: true \}/);
    assert.match(canvas, /userZoomingEnabled:\s*props\.userZoomingEnabled/);
    const positionBody = canvas.slice(
        canvas.indexOf("graphView.on('position', 'node'"),
        canvas.indexOf("graphView.on('grab', 'node'"),
    );
    assert.match(positionBody, /!props\.centerSelected/);
    assert.match(positionBody, /event\.target\.id\(\) !== selectedNodeId/);
    assert.match(positionBody, /graphView\.center\(event\.target\)/);
});

test('bounds the entity detail graph as a fixed viewport', () => {
    assert.match(css, /\.graph-entity-network\s*\{[^}]*height:\s*var\(--graph-entity-preview-height\)/s);
    assert.match(css, /\.graph-entity-network \.graph-explorer\s*\{[^}]*height:\s*100%/s);
    assert.match(css, /\.graph-entity-network \.graph-explorer-canvas,[\s\S]*?flex:\s*1 1 auto/s);
    assert.match(css, /\.graph-entity-network \.graph-explorer-canvas\s*\{[^}]*border:\s*1px solid #aebbc5/s);
});

test('coalesces ResizeObserver work without layout or fit', () => {
    const resizeBody = canvas.slice(canvas.indexOf('function resizeCanvas'), canvas.indexOf('function radialStyles'));
    assert.match(resizeBody, /requestAnimationFrame/);
    assert.match(resizeBody, /graphView\?\.resize\(\)/);
    assert.doesNotMatch(resizeBody, /runForceLayout|fitRadialGraph|\.fit\(|\.center\(/);
});
