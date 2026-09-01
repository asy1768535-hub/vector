import assert from 'node:assert/strict';
import test from 'node:test';

import {
    chatGraphDegrees,
    chatGraphExpansionRequest,
    chatGraphRelationLabel,
    mergeChatGraph,
    prepareChatGraph,
    projectChatGraphFocus,
} from './src/chat_graph_exploration.js';

const publication = { id: 'publication', ontology_version_id: 'ontology' };
const node = (id, depth = 1) => ({ id, depth, canonical_name: id, evidence: [] });
const relation = (id, source, target) => ({
    id,
    source_entity_id: source,
    target_entity_id: target,
    evidence: [],
});
const initial = {
    publication,
    seed_matches: [{ input_index: 0, entity_id: 'seed' }],
    nodes: [node('seed', 0), node('a')],
    relations: [relation('seed-a', 'seed', 'a')],
    counts: { seeds: 1, nodes: 2, relations: 1, evidence_locators: 0 },
    truncated: { nodes: false, relations: false, evidence: false },
};

test('prepares immutable citation seeds and display depths', () => {
    const graph = prepareChatGraph(initial);
    assert.equal(graph.nodes[0].citation_seed, true);
    assert.equal(graph.nodes[0].display_depth, 0);
    assert.equal(graph.nodes[1].citation_seed, false);
    assert.equal(initial.nodes[0].citation_seed, undefined);
});

test('builds a fixed one-hop expansion fenced to the current publication', () => {
    const expansion = chatGraphExpansionRequest(initial, 'a');
    assert.deepEqual(expansion.request.seeds, [{ entity_id: 'a' }]);
    assert.equal(expansion.request.max_hops, 1);
    assert.equal(expansion.request.expected_publication_id, publication.id);
    assert.equal(expansion.identity.ontologyVersionId, publication.ontology_version_id);
});

test('merges authoritative nodes and relations without duplicates or dangling edges', () => {
    const incoming = {
        nodes: [node('a', 0), node('b'), node('c')],
        relations: [
            relation('a-b', 'a', 'b'),
            relation('a-c', 'a', 'c'),
            relation('dangling', 'a', 'missing'),
        ],
        truncated: { nodes: false, relations: false, evidence: false },
    };
    const merged = mergeChatGraph(initial, incoming, 'a');
    assert.deepEqual(merged.addedNodeIds, ['b', 'c']);
    assert.deepEqual(merged.addedRelationIds, ['a-b', 'a-c']);
    assert.equal(merged.graph.nodes.find((item) => item.id === 'b').display_depth, 2);
    assert.equal(merged.graph.nodes.filter((item) => item.id === 'a').length, 1);
    assert.equal(merged.graph.relations.some((item) => item.id === 'dangling'), false);
    assert.deepEqual(merged.graph.counts, { seeds: 1, nodes: 4, relations: 3, evidence_locators: 0 });
});

test('enforces total graph bounds and reports truncation', () => {
    const incoming = {
        nodes: [node('a', 0), node('b'), node('c')],
        relations: [relation('a-b', 'a', 'b'), relation('a-c', 'a', 'c')],
    };
    const merged = mergeChatGraph(initial, incoming, 'a', { maxNodes: 3, maxRelations: 1 });
    assert.equal(merged.graph.nodes.length, 3);
    assert.equal(merged.graph.relations.length, 1);
    assert.equal(merged.limitReached, true);
    assert.equal(merged.graph.truncated.nodes, true);
    assert.equal(merged.graph.truncated.relations, true);
});

test('computes degree-based importance from the visible authoritative subgraph', () => {
    const degrees = chatGraphDegrees({
        nodes: [node('a'), node('b'), node('c')],
        relations: [relation('a-b', 'a', 'b'), relation('a-c', 'a', 'c')],
    });
    assert.deepEqual([...degrees.entries()], [['a', 2], ['b', 1], ['c', 1]]);
});

test('preserves exploration depth across consecutive expansions', () => {
    const first = mergeChatGraph(initial, {
        nodes: [node('a', 0), node('b')],
        relations: [relation('a-b', 'a', 'b')],
    }, 'a').graph;
    const second = mergeChatGraph(first, {
        nodes: [node('b', 0), node('c')],
        relations: [relation('b-c', 'b', 'c')],
    }, 'b').graph;
    assert.equal(second.nodes.find((item) => item.id === 'a').display_depth, 1);
    assert.equal(second.nodes.find((item) => item.id === 'b').display_depth, 2);
    assert.equal(second.nodes.find((item) => item.id === 'c').display_depth, 3);
    assert.equal(second.nodes.find((item) => item.id === 'seed').citation_seed, true);
});

test('projects only the connected component around one deterministic focus', () => {
    const disconnected = {
        seed_matches: [{ entity_id: 'seed' }, { entity_id: 'other' }],
        nodes: [node('seed', 0), node('a'), node('other', 0), node('b'), node('isolated', 0)],
        relations: [relation('seed-a', 'seed', 'a'), relation('other-b', 'other', 'b')],
    };
    const initialProjection = projectChatGraphFocus(disconnected);
    assert.equal(initialProjection.focusId, 'other');
    assert.deepEqual(initialProjection.graph.nodes.map((item) => item.id), ['other', 'b']);
    assert.equal(initialProjection.hiddenNodeCount, 3);
    assert.equal(initialProjection.hiddenComponentCount, 2);
    assert.equal(initialProjection.pageIndex, 0);
    assert.equal(initialProjection.pageCount, 3);
    assert.deepEqual(initialProjection.pageFocusIds, ['other', 'seed', 'isolated']);

    const switched = projectChatGraphFocus(disconnected, 'seed');
    assert.equal(switched.focusId, 'seed');
    assert.equal(switched.pageIndex, 1);
    assert.deepEqual(switched.pageFocusIds, initialProjection.pageFocusIds);
    assert.deepEqual(switched.graph.relations.map((item) => item.id), ['seed-a']);

    const recentered = projectChatGraphFocus(disconnected, 'a');
    assert.equal(recentered.focusId, 'a');
    assert.equal(recentered.pageIndex, 1);
    assert.deepEqual(recentered.pageFocusIds, initialProjection.pageFocusIds);
});

test('uses Chinese presentation labels without mutating schema values', () => {
    const english = relation('r1', 'a', 'b');
    english.relation_type = { key: 'belongs_to', label: 'Belongs To' };
    assert.equal(chatGraphRelationLabel(english), '属于');
    assert.equal(english.relation_type.key, 'belongs_to');
    assert.equal(chatGraphRelationLabel({ relation_type: { key: 'custom_link' } }), '关联');
    assert.equal(chatGraphRelationLabel({ relation_type: { label: '监督' } }), '监督');
});
