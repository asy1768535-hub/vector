import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const explorer = fs.readFileSync(new URL('./src/components/GraphExplorer.js', import.meta.url), 'utf8');
const canvas = fs.readFileSync(new URL('./src/components/GraphCanvas.js', import.meta.url), 'utf8');
const parent = fs.readFileSync(new URL('./src/views/GraphGovernance.js', import.meta.url), 'utf8');
const helper = fs.readFileSync(new URL('./src/graph_exploration_ui.js', import.meta.url), 'utf8');
const governanceHelper = fs.readFileSync(new URL('./src/graph_governance_ui.js', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('integrates exploration as the fifth non-default Knowledge Graph tab', () => {
    assert.ok(parent.includes("<el-tab-pane label=\"图谱探查\" name=\"explore\""));
    assert.ok(parent.includes('<graph-explorer'));
    assert.ok(parent.includes('@open-entity="loadEntityDetail"'));
    assert.ok(parent.includes('@open-relation="loadRelationDetail"'));
    assert.ok(parent.includes('@open-evidence="openEvidence"'));
    assert.ok(parent.indexOf('name="explore"') > parent.indexOf('name="publications"'));
    assert.ok(governanceHelper.includes("'explore'"));
    assert.ok(governanceHelper.includes("'entities'"));
});

test('explorer fences scope, search and independent per-seed traversal requests', () => {
    for (const token of [
        'searchRequestSeq', 'traversalRequestSeq', 'resetExplorer',
        'api.searchGraphEntities', 'graphEntityPageMatches',
        'api.queryPublishedGraph', 'graphTraversalResponseMatches',
        'Promise.allSettled', 'selectedSeeds', 'selectedSeeds.length >= 4',
        'expected_publication_id', 'include_evidence_locators',
        'publication_state: \'all\'', 'statuses: [\'active\']',
    ]) assert.ok(explorer.includes(token), `missing explorer fence ${token}`);
    assert.ok(explorer.includes('props.organizationId'));
    assert.ok(explorer.includes('props.librarySlugs'));
});

test('renders canvas and equivalent accessible Entity, Relation and Evidence controls', () => {
    for (const token of [
        '<graph-canvas', '<table', '<th scope="col"',
        'openEntity', 'openRelation', 'openEvidence',
        'group.data.nodes', 'group.data.relations', 'relation.evidence', 'node.evidence',
        'Publication', 'Ontology', 'Manifest', '截断',
    ]) assert.ok(explorer.includes(token), `missing result surface ${token}`);
    for (const token of [
        '<canvas', 'ResizeObserver', 'devicePixelRatio',
        'layoutGraphRadially', 'hitTestGraphLayout', 'drawArrowhead',
        "emit('open-entity'", "emit('open-relation'", 'onBeforeUnmount',
    ]) assert.ok(canvas.includes(token), `missing canvas behavior ${token}`);
});

test('keeps private payloads out and provides desktop internal scrolling', () => {
    const sources = `${explorer}\n${canvas}`;
    for (const forbidden of [
        'v-html', 'console.', 'source_text', 'raw_response', 'storage_url',
        'object_key', 'prompt', 'api_key', 'candidate_payload', 'window.open',
    ]) assert.equal(sources.includes(forbidden), false, `forbidden token ${forbidden}`);
    assert.doesNotMatch(explorer, /style="[^"]*"/);
    for (const token of [
        '.graph-explorer', '.graph-explorer-controls', '.graph-explorer-canvas',
        '.graph-explorer-table-shell', '@media screen and (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing exploration CSS ${token}`);
    assert.match(css, /\.graph-explorer-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
    assert.match(css, /\.graph-explorer-canvas\s*\{[^}]*aspect-ratio:/s);
    assert.doesNotMatch(css, /\.graph-explorer-seed-library\s*\{[^}]*display:\s*none/s);
    const mobileEnd = css.indexOf('/* v0.9 Schema lifecycle console */');
    const mobileStart = css.lastIndexOf('@media screen and (max-width: 520px)', mobileEnd);
    assert.ok(mobileStart >= 0 && mobileEnd > mobileStart);
    assert.doesNotMatch(css.slice(mobileStart, mobileEnd), /\.graph-explorer/);
});
