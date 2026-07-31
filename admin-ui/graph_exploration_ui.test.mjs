import assert from 'node:assert/strict';
import test from 'node:test';

import {
    explorationSeedEligible,
    explorationSeedKey,
    explorationSeedSearchRowValid,
    graphExplorationErrorProjection,
    graphTraversalResponseMatches,
    graphTraversalSummary,
    hitTestGraphLayout,
    layoutGraphRadially,
    selectExplorationSeed,
} from './src/graph_exploration_ui.js';

const LIB_A = '10000000-0000-4000-8000-000000000001';
const LIB_B = '10000000-0000-4000-8000-000000000002';
const ONTOLOGY = '20000000-0000-4000-8000-000000000001';
const PUBLICATION = '30000000-0000-4000-8000-000000000001';
const SEED = '40000000-0000-4000-8000-000000000001';
const NEIGHBOR = '40000000-0000-4000-8000-000000000002';
const OUTER = '40000000-0000-4000-8000-000000000003';
const RELATION = '50000000-0000-4000-8000-000000000001';
const RELATION_2 = '50000000-0000-4000-8000-000000000002';
const ENTITY_TYPE = '60000000-0000-4000-8000-000000000001';
const RELATION_TYPE = '70000000-0000-4000-8000-000000000001';
const EVIDENCE = '80000000-0000-4000-8000-000000000001';
const DOCUMENT = '90000000-0000-4000-8000-000000000001';
const REVISION = 'a0000000-0000-4000-8000-000000000001';
const HASH = 'b'.repeat(64);

function seed(libraryId = LIB_A, slug = 'legal-a') {
    return {
        id: SEED,
        library: { id: libraryId, slug, name: slug === 'legal-a' ? 'Legal A' : 'Legal B' },
        ontology_version_id: ONTOLOGY,
        entity_type: { id: ENTITY_TYPE, key: 'company', label: '公司' },
        canonical_name: '同名公司',
        normalized_name: '同名公司',
        publication_state: 'published',
        publication: { id: PUBLICATION, status: 'active', item_hash: HASH },
    };
}

function evidence(overrides = {}) {
    return {
        evidence_id: EVIDENCE,
        document_id: DOCUMENT,
        document_revision_id: REVISION,
        document_block_id: null,
        evidence_kind: 'direct_statement',
        page_start: 2,
        page_end: 3,
        source_start: 10,
        source_end: 30,
        ...overrides,
    };
}

function node(id, name, depth, evidenceRows = []) {
    return {
        id,
        item_hash: HASH,
        entity_type: { id: ENTITY_TYPE, key: 'company', label: '公司' },
        canonical_name: name,
        normalized_name: name.toLowerCase(),
        source_type: 'extracted',
        confidence: 0.91,
        depth,
        evidence: evidenceRows,
    };
}

function response() {
    return {
        contract_version: 'v1',
        publication: {
            id: PUBLICATION,
            ontology_version_id: ONTOLOGY,
            manifest_version: 'v1',
            manifest_hash: HASH,
            activated_at: '2026-07-23T08:00:00Z',
        },
        seed_matches: [{ input_index: 0, entity_id: SEED }],
        nodes: [
            node(SEED, '同名公司', 0, [evidence()]),
            node(NEIGHBOR, '乙公司', 1),
            node(OUTER, '丙公司', 2),
        ],
        relations: [
            {
                id: RELATION,
                item_hash: HASH,
                relation_type: {
                    id: RELATION_TYPE,
                    key: 'invests',
                    label: '投资',
                    direction: 'directed',
                },
                source_entity_id: SEED,
                target_entity_id: NEIGHBOR,
                source_type: 'extracted',
                confidence: 0.88,
                depth: 1,
                evidence: [evidence()],
            },
            {
                id: RELATION_2,
                item_hash: HASH,
                relation_type: {
                    id: RELATION_TYPE,
                    key: 'controls',
                    label: '控制',
                    direction: 'undirected',
                },
                source_entity_id: NEIGHBOR,
                target_entity_id: OUTER,
                source_type: 'manual',
                confidence: null,
                depth: 2,
                evidence: [],
            },
        ],
        counts: { seeds: 1, nodes: 3, relations: 2, evidence_locators: 2 },
        truncated: { nodes: false, relations: true, evidence: false },
    };
}

const identity = {
    ontologyVersionId: ONTOLOGY,
    publicationId: PUBLICATION,
    seedEntityId: SEED,
    maxHops: 2,
    maxNodes: 50,
    maxRelations: 100,
};

test('keeps same-named seeds separate by Library and rejects unavailable Publications', () => {
    const first = seed();
    const second = seed(LIB_B, 'legal-b');
    assert.equal(explorationSeedEligible(first), true);
    assert.notEqual(explorationSeedKey(first), explorationSeedKey(second));
    assert.deepEqual(selectExplorationSeed([], first), [first]);
    assert.deepEqual(selectExplorationSeed([first], second), [first, second]);
    assert.deepEqual(selectExplorationSeed([first], first), [first]);
    assert.equal(explorationSeedEligible({
        ...first,
        publication: { ...first.publication, status: 'degraded' },
    }), false);
    assert.equal(explorationSeedEligible({ ...first, publication_state: 'staged' }), false);
    assert.equal(explorationSeedSearchRowValid({
        ...first,
        publication_state: 'staged',
        publication: null,
    }), true);
    assert.equal(explorationSeedSearchRowValid({ ...first, entity_type: null }), false);
    assert.equal(selectExplorationSeed([first, second, seed(LIB_A, 'a2'), seed(LIB_B, 'b2')], {
        ...first,
        id: NEIGHBOR,
    }).length, 4);
});

test('accepts only exact bounded traversal identities, counts, endpoints, hops and Evidence', () => {
    const valid = response();
    assert.equal(graphTraversalResponseMatches(valid, identity), true);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        publication: { ...valid.publication, id: 'wrong' },
    }, identity), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        seed_matches: [{ input_index: 0, entity_id: NEIGHBOR }],
    }, identity), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        counts: { ...valid.counts, relations: 1 },
    }, identity), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        relations: [{ ...valid.relations[0], target_entity_id: 'wrong' }, valid.relations[1]],
    }, identity), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        nodes: valid.nodes.map((item) => item.id === OUTER ? { ...item, depth: 2 } : item),
    }, { ...identity, maxHops: 1 }), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        nodes: valid.nodes.map((item) => item.id === SEED
            ? { ...item, evidence: [evidence({ page_start: 3, page_end: 2 })] }
            : item),
    }, identity), false);
    assert.equal(graphTraversalResponseMatches({
        ...valid,
        relations: [{ ...valid.relations[0], id: RELATION_2 }, valid.relations[1]],
    }, identity), false);
});

test('projects fixed errors and explicit truncation without raw server detail', () => {
    assert.deepEqual(graphExplorationErrorProjection({
        status: 409,
        body: { detail: 'publication_changed', storage_url: 'must-not-render' },
    }), {
        kind: 'publication_changed',
        message: '图谱发布版本已变化，请重新选择实体后再探查。',
    });
    assert.equal(
        graphExplorationErrorProjection({ status: 503, body: { detail: 'graph_retrieval_disabled' } }).kind,
        'disabled',
    );
    assert.equal(graphExplorationErrorProjection({ status: 500, body: 'secret' }).kind, 'error');
    assert.deepEqual(graphTraversalSummary(response()), {
        nodes: 3,
        relations: 2,
        evidence: 2,
        truncationLabels: ['关系'],
    });
});

test('builds deterministic concentric rings and hit-tests nodes before relations', () => {
    const first = layoutGraphRadially(response(), 640, 420);
    const second = layoutGraphRadially(response(), 640, 420);
    assert.deepEqual(first, second);
    assert.deepEqual(first.nodes.map((item) => item.depth), [0, 1, 2]);
    assert.equal(first.nodes[0].x, 320);
    assert.equal(first.nodes[0].y, 210);
    assert.ok(first.nodes[1].y < first.nodes[0].y);
    assert.ok(first.nodes[2].y < first.nodes[1].y);
    assert.deepEqual(hitTestGraphLayout(first, first.nodes[0].x, first.nodes[0].y), {
        kind: 'node',
        id: SEED,
    });
    const edge = first.relations[0];
    assert.deepEqual(hitTestGraphLayout(first, (edge.x1 + edge.x2) / 2, (edge.y1 + edge.y2) / 2), {
        kind: 'relation',
        id: RELATION,
    });
});

test('lays out multiple seed nodes on a distinct inner ring', () => {
    const graph = response();
    graph.nodes = [
        graph.nodes[0],
        { ...graph.nodes[0], id: '60000000-0000-0000-0000-000000000011', canonical_name: 'Seed B' },
        { ...graph.nodes[0], id: '60000000-0000-0000-0000-000000000012', canonical_name: 'Seed C' },
        { ...graph.nodes[0], id: '60000000-0000-0000-0000-000000000013', canonical_name: 'Seed D' },
        ...graph.nodes.slice(1),
    ];

    const layout = layoutGraphRadially(graph, 390, 360);
    const seeds = layout.nodes.filter((item) => item.depth === 0);
    const positions = new Set(seeds.map((item) => `${item.x.toFixed(3)}:${item.y.toFixed(3)}`));

    assert.equal(seeds.length, 4);
    assert.equal(positions.size, 4);
    assert.ok(seeds.every((item) => Math.hypot(item.x - 195, item.y - 180) > 40));
    assert.ok(layout.nodes.filter((item) => item.depth === 1).every(
        (item) => Math.hypot(item.x - 195, item.y - 180) > 100,
    ));
});
