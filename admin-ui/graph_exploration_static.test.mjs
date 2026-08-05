import assert from 'node:assert/strict';
import test from 'node:test';

import {
    graphPublicationItemPageMatches,
    graphPublicationReadMatches,
    staticPublicationPanorama,
} from './src/graph_exploration_ui.js';

const LIBRARY = '10000000-0000-4000-8000-000000000001';
const ONTOLOGY = '20000000-0000-4000-8000-000000000001';
const PUBLICATION = '30000000-0000-4000-8000-000000000001';
const ENTITY = '40000000-0000-4000-8000-000000000001';
const ENTITY_2 = '40000000-0000-4000-8000-000000000002';
const RELATION = '50000000-0000-4000-8000-000000000001';
const ENTITY_ITEM = '50000000-0000-4000-8000-000000000002';
const ENTITY_ITEM_2 = '50000000-0000-4000-8000-000000000003';
const RELATION_ITEM = '50000000-0000-4000-8000-000000000004';
const ENTITY_TYPE = '60000000-0000-4000-8000-000000000001';
const RELATION_TYPE = '70000000-0000-4000-8000-000000000001';
const EVIDENCE = '80000000-0000-4000-8000-000000000001';
const HASH = 'a'.repeat(64);
const HASH_2 = 'b'.repeat(64);

function publication(overrides = {}) {
    return {
        id: PUBLICATION,
        library_id: LIBRARY,
        ontology_version_id: ONTOLOGY,
        status: 'active',
        healthy: true,
        publication_enabled: true,
        manifest_version: 'v1',
        manifest_hash: HASH,
        entity_count: 2,
        relation_count: 1,
        activated_at: '2026-08-03T08:00:00Z',
        ...overrides,
    };
}

function entityItem(id, name, itemHash = HASH) {
    return {
        id: id === ENTITY ? ENTITY_ITEM : ENTITY_ITEM_2,
        publication_id: PUBLICATION,
        item_kind: 'entity',
        entity_id: id,
        relation_id: null,
        item_hash: itemHash,
        status: 'active',
        support_evidence_ids: [EVIDENCE],
        support_counts: { active_mentions: 1 },
        fact_snapshot: {
            manifest_version: 'v1',
            item_kind: 'entity',
            library_id: LIBRARY,
            ontology_version_id: ONTOLOGY,
            entity_id: id,
            entity_type_id: ENTITY_TYPE,
            canonical_name: name,
            normalized_name: name.toLowerCase(),
            properties_hash: HASH_2,
            source_type: 'extracted',
            confidence: '0.910000',
            support_evidence_ids: [EVIDENCE],
        },
    };
}

function relationItem(itemHash = HASH_2) {
    return {
        id: RELATION_ITEM,
        publication_id: PUBLICATION,
        item_kind: 'relation',
        entity_id: null,
        relation_id: RELATION,
        item_hash: itemHash,
        status: 'active',
        support_evidence_ids: [EVIDENCE],
        support_counts: { supports: 1 },
        fact_snapshot: {
            manifest_version: 'v1',
            item_kind: 'relation',
            library_id: LIBRARY,
            ontology_version_id: ONTOLOGY,
            relation_id: RELATION,
            relation_type_id: RELATION_TYPE,
            source_entity_id: ENTITY,
            target_entity_id: ENTITY_2,
            properties_hash: HASH,
            source_type: 'manual',
            confidence: null,
            support_evidence_ids: [EVIDENCE],
        },
    };
}

const identity = {
    publicationId: PUBLICATION,
    libraryId: LIBRARY,
    ontologyVersionId: ONTOLOGY,
};

test('accepts one exact active Publication and paged frozen items', () => {
    const current = publication();
    assert.equal(graphPublicationReadMatches(current, identity), true);
    const entityPage = {
        items: [entityItem(ENTITY, 'Alpha'), entityItem(ENTITY_2, 'Beta', HASH_2)],
        total: 2,
        page: 1,
        page_size: 500,
    };
    assert.equal(graphPublicationItemPageMatches(entityPage, {
        ...identity,
        entityCount: 2,
        relationCount: 1,
        page: 1,
        pageSize: 500,
    }, 'entity'), true);
});

test('builds the panorama from frozen snapshots and preserves item/evidence identity', () => {
    const graph = staticPublicationPanorama(
        publication(),
        [entityItem(ENTITY, 'Alpha'), entityItem(ENTITY_2, 'Beta', HASH_2)],
        [relationItem()],
        { id: LIBRARY, slug: 'legal-a', name: 'Legal A' },
    );

    assert.equal(graph.nodes.length, 2);
    assert.equal(graph.relations.length, 1);
    assert.equal(graph.nodes[0].item_hash, HASH);
    assert.deepEqual(graph.nodes[0].evidence, [{ evidence_id: EVIDENCE }]);
    assert.equal(graph.relations[0].source_entity_id, ENTITY);
    assert.equal(graph.relations[0].target_entity_id, ENTITY_2);
    assert.equal(graph.relations[0].item_hash, HASH_2);
    assert.equal(graph.publication.id, PUBLICATION);
});

test('rejects stale identities, malformed pages, hash drift, and dangling relation endpoints', () => {
    const current = publication();
    assert.equal(graphPublicationReadMatches({ ...current, id: ENTITY }, identity), false);
    assert.equal(graphPublicationReadMatches({ ...current, status: 'degraded', healthy: false }, identity), false);
    assert.equal(graphPublicationItemPageMatches({
        items: [entityItem(ENTITY, 'Alpha')],
        total: 2,
        page: 1,
        page_size: 500,
    }, {
        ...identity,
        entityCount: 2,
        relationCount: 1,
        page: 1,
        pageSize: 500,
    }, 'entity'), true);
    assert.equal(staticPublicationPanorama(
        current,
        [entityItem(ENTITY, 'Alpha'), entityItem(ENTITY_2, 'Beta', HASH_2)],
        [{ ...relationItem(), item_hash: 'not-a-hash' }],
        { id: LIBRARY, slug: 'legal-a', name: 'Legal A' },
    ), null);
    assert.equal(staticPublicationPanorama(
        current,
        [entityItem(ENTITY, 'Alpha'), entityItem(ENTITY_2, 'Beta', HASH_2)],
        [{
            ...relationItem(),
            fact_snapshot: {
                ...relationItem().fact_snapshot,
                target_entity_id: '90000000-0000-4000-8000-000000000001',
            },
        }],
        { id: LIBRARY, slug: 'legal-a', name: 'Legal A' },
    ), null);
});
