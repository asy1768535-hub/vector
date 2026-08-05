import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('citation dialog exposes a graph action only for usable chunks', () => {
    assert.match(chat, /v-if="recalledChunkDialog\.source\.chunk_id"/);
    assert.match(chat, /@click="openCitationGraph\(recalledChunkDialog\.source\)"/);
    assert.ok(chat.includes('复制片段'));
    assert.ok(chat.includes('知识图谱'));
});

test('completed answers expose a graph action for their first usable citation', () => {
    assert.match(chat, /function messageGraphSource\(message\)/);
    assert.match(chat, /\.find\(\(source\) => source\?\.chunk_id\)/);
    assert.match(chat, /v-if="m\.time && messageGraphSource\(m\)"/);
    assert.match(chat, /class="chat-answer-graph"/);
    assert.match(chat, /plain size="small" type="primary"/);
    assert.ok(chat.includes('查看知识图谱'));
    assert.ok(chat.includes('title="知识点图谱"'));
    assert.match(chat, /@click="openCitationGraph\(messageGraphSource\(m\)\)"/);
    assert.doesNotMatch(chat, /chat-graph-button|openKnowledgeGraph/);
});

test('citation graph uses a dedicated dialog and published context endpoint', () => {
    assert.match(api, /getChatGraphContext/);
    assert.match(api, /\/libraries\/\$\{slug\}\/chat\/graph-context/);
    assert.ok(chat.includes('recalledChunkDialog.value = { open: false, source: null }'));
    assert.match(chat, /class="chat-citation-graph-dialog"/);
    assert.match(chat, /<graph-canvas/);
    assert.match(chat, /variant="citation"/);
    assert.match(chat, /class="chat-citation-graph-inspector"/);
    assert.ok(!chat.includes('<graph-knowledge-browser'));
});

test('citation graph renders loading empty error and selected fact states', () => {
    assert.ok(chat.includes('citationGraphDialog.loading'));
    assert.ok(chat.includes('citationGraphDialog.error'));
    assert.ok(chat.includes('该引用暂未关联已发布图谱'));
    assert.ok(chat.includes('citationGraphSelection()'));
    assert.ok(chat.includes('@open-entity="selectCitationGraphNode"'));
    assert.ok(chat.includes('@focus-entity="focusCitationGraphNode"'));
    assert.ok(chat.includes('@open-relation="selectCitationGraphRelation"'));
});

test('stale citation graph requests cannot overwrite a newer selection', () => {
    assert.ok(chat.includes('const requestSeq = ++citationGraphRequestSeq'));
    assert.ok(chat.includes('if (requestSeq !== citationGraphRequestSeq) return'));
    assert.ok(chat.includes('@closed="closeAndInvalidateCitationGraph"'));
    assert.match(chat, /function closeAndInvalidateCitationGraph\(\)/);
});

test('citation graph dialog has bounded responsive dimensions', () => {
    assert.match(css, /\.chat-citation-graph-dialog \.el-dialog/);
    assert.match(css, /width:\s*1180px/);
    assert.match(css, /max-width:\s*calc\(100vw - 24px\)/);
    assert.match(css, /\.chat-citation-graph-dialog \.graph-explorer-canvas/);
    assert.match(css, /min-height:\s*620px/);
});

test('citation graph delegates rendering and interaction to local Cytoscape', () => {
    const graphCanvas = readFileSync(new URL('./src/components/GraphCanvas.js', import.meta.url), 'utf8');
    const index = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
    assert.match(graphCanvas, /import cytoscape from 'cytoscape'/);
    assert.match(graphCanvas, /name: 'preset'/);
    assert.match(graphCanvas, /layoutCitationGraph\(/);
    assert.match(graphCanvas, /projectChatGraphFocus/);
    assert.match(graphCanvas, /autoungrabify:\s*false/);
    assert.match(graphCanvas, /graphView\.on\('tap', 'node'/);
    assert.match(graphCanvas, /graphView\.on\('dbltap', 'node'/);
    assert.match(graphCanvas, /graphView\.on\('grab', 'node\.citation-focus'/);
    assert.match(graphCanvas, /graphView\.on\('drag', 'node\.citation-focus'/);
    assert.match(graphCanvas, /userPanningEnabled:\s*true/);
    assert.match(graphCanvas, /userZoomingEnabled:\s*\{ type: Boolean, default: true \}/);
    assert.match(graphCanvas, /userZoomingEnabled:\s*props\.userZoomingEnabled/);
    assert.match(index, /"cytoscape": "\.\/vendor\/cytoscape\.esm\.min\.mjs"/);
});

test('citation graph pages real components and keeps one Obsidian-style settings entry', () => {
    const graphCanvas = readFileSync(new URL('./src/components/GraphCanvas.js', import.meta.url), 'utf8');
    assert.match(graphCanvas, /class="graph-component-pager"/);
    assert.match(graphCanvas, /citationProjection\.pageIndex \+ 1/);
    assert.match(graphCanvas, /switchCitationComponent\(-1\)/);
    assert.match(graphCanvas, /switchCitationComponent\(1\)/);
    assert.match(graphCanvas, /class="graph-settings-toggle"/);
    assert.match(graphCanvas, /适配画布/);
    assert.match(graphCanvas, /重新布局/);
    assert.doesNotMatch(graphCanvas, /graph-canvas-tools|zoomCitation/);
});

test('citation nodes expand through the fenced published traversal client', () => {
    assert.match(chat, /chatGraphExpansionRequest/);
    assert.match(chat, /api\.queryPublishedGraph\(currentSlug\.value, expansion\.request\)/);
    assert.match(chat, /graphTraversalResponseMatches\(response, expansion\.identity\)/);
    assert.match(chat, /mergeChatGraph\(citationGraphDialog\.value\.data\.graph, response, node\.id\)/);
    assert.match(chat, /requestSeq !== citationGraphRequestSeq/);
    assert.match(chat, /expandedGraphEntityIds\.value\.has\(node\.id\)/);
    assert.match(chat, /const currentSelection = citationGraphDialog\.value\.selected/);
    assert.match(chat, /currentSelection\.item\?\.id === node\.id/);
    assert.match(chat, /@clear-selection="clearCitationGraphSelection"/);
    assert.match(chat, /:expanding-id="expandingGraphEntityId"/);
    assert.match(chat, /:expanded-ids="expandedGraphEntityIdList"/);
    assert.match(chat, /function focusCitationGraphNode\(node\)/);
});
