import assert from 'node:assert/strict';
import test from 'node:test';

import {
    activateGraphPublication,
    addGraphEntityAlias,
    cancelGraphGovernanceAction,
    cancelGraphPublication,
    correctGraphEntity,
    correctGraphRelation,
    decideGraphGovernanceAction,
    disableGraphAlias,
    disableGraphEntity,
    disableGraphRelation,
    getActiveGraphPublication,
    getGraphEntity,
    getGraphGovernanceContext,
    getGraphRelation,
    listGraphGovernanceActions,
    listGraphPublicationItems,
    listGraphPublications,
    mergeGraphEntities,
    planGraphGovernancePublication,
    rerunGraphExtraction,
    restoreGraphEntity,
    restoreGraphRelation,
    reviewGraphRelation,
    rollbackGraphPublication,
    searchGraphEntities,
    searchGraphRelations,
    submitGraphEntity,
    submitGraphRelation,
} from './src/api.js';

const ORG = '10000000-0000-4000-8000-000000000001';
const ONTOLOGY = '20000000-0000-4000-8000-000000000001';
const ENTITY = '30000000-0000-4000-8000-000000000001';
const ENTITY_2 = '30000000-0000-4000-8000-000000000002';
const RELATION = '40000000-0000-4000-8000-000000000001';
const ALIAS = '50000000-0000-4000-8000-000000000001';
const ACTION = '60000000-0000-4000-8000-000000000001';
const PUBLICATION = '70000000-0000-4000-8000-000000000001';
const JOB = '80000000-0000-4000-8000-000000000001';
const TYPE = '90000000-0000-4000-8000-000000000001';
const HASH = 'a'.repeat(64);

async function captureRequests(run) {
    const calls = [];
    const priorFetch = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
        calls.push({ url: String(url), options });
        return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await run();
    } finally {
        globalThis.fetch = priorFetch;
    }
    return calls;
}

function body(call) {
    return JSON.parse(call.options.body);
}

test('Graph Catalog clients use exact paths and search allowlists', async () => {
    const calls = await captureRequests(async () => {
        await searchGraphEntities(ORG, {
            library_slugs: ['legal-a', 'legal-b'],
            query: 'Acme',
            ontology_version_ids: [ONTOLOGY],
            type_keys: ['company'],
            statuses: ['active'],
            source_types: ['manual'],
            publication_state: 'all',
            cursor: 'opaque',
            limit: 25,
            object_key: 'must-not-leak',
        });
        await searchGraphRelations(ORG, {
            scope_id: 'a0000000-0000-4000-8000-000000000001',
            review_statuses: ['pending_review'],
            statuses: [],
            source_content: 'must-not-leak',
        });
        await getGraphEntity(ORG, 'legal-a', ENTITY);
        await getGraphRelation(ORG, 'legal-a', RELATION);
    });

    assert.equal(calls[0].url, `/organizations/${ORG}/graph-catalog/entities:search`);
    assert.equal(calls[0].options.credentials, 'include');
    assert.deepEqual(body(calls[0]), {
        library_slugs: ['legal-a', 'legal-b'],
        query: 'Acme',
        ontology_version_ids: [ONTOLOGY],
        type_keys: ['company'],
        statuses: ['active'],
        source_types: ['manual'],
        publication_state: 'all',
        cursor: 'opaque',
        limit: 25,
    });
    assert.deepEqual(body(calls[1]), {
        scope_id: 'a0000000-0000-4000-8000-000000000001',
        statuses: [],
        review_statuses: ['pending_review'],
    });
    assert.equal(
        calls[2].url,
        `/organizations/${ORG}/graph-catalog/libraries/legal-a/entities/${ENTITY}`,
    );
    assert.equal(
        calls[3].url,
        `/organizations/${ORG}/graph-catalog/libraries/legal-a/relations/${RELATION}`,
    );
});

test('governance clients preserve exact command fences and discard caller extras', async () => {
    const command = {
        ontology_version_id: ONTOLOGY,
        entity_type_id: TYPE,
        relation_type_id: TYPE,
        canonical_name: 'Acme',
        source_entity_id: ENTITY,
        target_entity_id: ENTITY_2,
        properties: { country: 'CN' },
        expected_state_hash: HASH,
        expected_entity_state_hash: HASH,
        alias: 'Acme Group',
        decision: 'approve',
        expected_status: 'pending_review',
        reason_code: 'verified',
        idempotency_key: 'intent-1',
        prompt: 'must-not-leak',
        storage_url: 'must-not-leak',
    };
    const calls = await captureRequests(async () => {
        await getGraphGovernanceContext('legal-a');
        await listGraphGovernanceActions('legal-a', {
            statuses: ['pending_review', 'approved'],
            limit: 20,
            offset: 40,
            ignored: 'no',
        });
        await submitGraphEntity('legal-a', command);
        await submitGraphRelation('legal-a', command);
        await correctGraphEntity('legal-a', ENTITY, command);
        await correctGraphRelation('legal-a', RELATION, command);
        await addGraphEntityAlias('legal-a', ENTITY, command);
        await decideGraphGovernanceAction('legal-a', ACTION, command);
        await cancelGraphGovernanceAction('legal-a', ACTION, command);
        await reviewGraphRelation('legal-a', RELATION, command);
        await disableGraphEntity('legal-a', ENTITY, command);
        await restoreGraphEntity('legal-a', ENTITY, command);
        await disableGraphRelation('legal-a', RELATION, command);
        await restoreGraphRelation('legal-a', RELATION, command);
        await disableGraphAlias('legal-a', ALIAS, command);
        await mergeGraphEntities('legal-a', {
            ...command,
            survivor_entity_id: ENTITY,
            loser_entity_id: ENTITY_2,
            expected_survivor_state_hash: HASH,
            expected_loser_state_hash: HASH,
            resolutions: [],
        });
        await planGraphGovernancePublication('legal-a', {
            ...command,
            action_ids: [ACTION],
            expected_parent_publication_id: null,
            dry_run: true,
        });
        await rerunGraphExtraction('legal-a', JOB, {
            client_idempotency_key: 'rerun-001',
            source_text: 'must-not-leak',
        });
    });

    assert.equal(calls[0].url, '/libraries/legal-a/graph-governance/context');
    assert.equal(
        calls[1].url,
        '/libraries/legal-a/graph-governance/actions?status=pending_review&status=approved&limit=20&offset=40',
    );
    assert.deepEqual(body(calls[2]), {
        ontology_version_id: ONTOLOGY,
        entity_type_id: TYPE,
        canonical_name: 'Acme',
        properties: { country: 'CN' },
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[3]), {
        ontology_version_id: ONTOLOGY,
        relation_type_id: TYPE,
        source_entity_id: ENTITY,
        target_entity_id: ENTITY_2,
        properties: { country: 'CN' },
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[4]), {
        expected_state_hash: HASH,
        canonical_name: 'Acme',
        properties: { country: 'CN' },
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[5]), {
        expected_state_hash: HASH,
        source_entity_id: ENTITY,
        target_entity_id: ENTITY_2,
        properties: { country: 'CN' },
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[6]), {
        expected_entity_state_hash: HASH,
        alias: 'Acme Group',
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[7]), {
        expected_status: 'pending_review',
        decision: 'approve',
        reason_code: 'verified',
    });
    assert.deepEqual(body(calls[8]), {
        expected_status: 'pending_review',
        reason_code: 'verified',
    });
    assert.deepEqual(body(calls[9]), {
        expected_state_hash: HASH,
        decision: 'approve',
        reason_code: 'verified',
        idempotency_key: 'intent-1',
    });
    for (const call of calls.slice(10, 15)) {
        assert.deepEqual(body(call), {
            expected_state_hash: HASH,
            reason_code: 'verified',
            idempotency_key: 'intent-1',
        });
    }
    assert.deepEqual(body(calls[15]), {
        ontology_version_id: ONTOLOGY,
        survivor_entity_id: ENTITY,
        loser_entity_id: ENTITY_2,
        expected_survivor_state_hash: HASH,
        expected_loser_state_hash: HASH,
        reason_code: 'verified',
        resolutions: [],
        idempotency_key: 'intent-1',
    });
    assert.deepEqual(body(calls[16]), {
        ontology_version_id: ONTOLOGY,
        action_ids: [ACTION],
        expected_parent_publication_id: null,
        dry_run: true,
        idempotency_key: 'intent-1',
    });
    assert.equal(calls[17].url, `/libraries/legal-a/v04/graph-extractions/${JOB}/rerun`);
    assert.deepEqual(body(calls[17]), { client_idempotency_key: 'rerun-001' });
});

test('Publication clients use current v0.5 routes and strict bodies', async () => {
    const calls = await captureRequests(async () => {
        await listGraphPublications('legal-a', {
            status: 'superseded',
            source_mode: 'manual_plan',
            ontology_version_id: ONTOLOGY,
            page: 2,
            page_size: 20,
            storage_key: 'must-not-leak',
        });
        await getActiveGraphPublication('legal-a', ONTOLOGY);
        await activateGraphPublication('legal-a', PUBLICATION, {
            idempotency_key: 'activate-1',
            expected_manifest_hash: HASH,
            force: true,
        });
        await cancelGraphPublication('legal-a', PUBLICATION, {
            idempotency_key: 'cancel-1',
            reason_code: 'operator_cancelled',
            raw_reason: 'must-not-leak',
        });
        await rollbackGraphPublication('legal-a', PUBLICATION, {
            idempotency_key: 'rollback-1',
            dry_run: true,
            target_status: 'must-not-leak',
        });
    });

    assert.equal(
        calls[0].url,
        `/libraries/legal-a/v05/graph-publications/?status=superseded&source_mode=manual_plan&ontology_version_id=${ONTOLOGY}&page=2&page_size=20`,
    );
    assert.equal(
        calls[1].url,
        `/libraries/legal-a/v05/graph-publications/active?ontology_version_id=${ONTOLOGY}`,
    );
    assert.deepEqual(body(calls[2]), {
        idempotency_key: 'activate-1',
        expected_manifest_hash: HASH,
    });
    assert.deepEqual(body(calls[3]), {
        idempotency_key: 'cancel-1',
        reason_code: 'operator_cancelled',
    });
    assert.deepEqual(body(calls[4]), { idempotency_key: 'rollback-1', dry_run: true });
});

test('Publication item client keeps the frozen snapshot query scoped', async () => {
    const calls = await captureRequests(async () => {
        await listGraphPublicationItems('legal-a', PUBLICATION, {
            item_kind: 'entity',
            status: 'active',
            page: 2,
            page_size: 500,
            fact_snapshot: 'must-not-leak',
        });
    });

    assert.equal(
        calls[0].url,
        `/libraries/legal-a/v05/graph-publications/${PUBLICATION}/items?item_kind=entity&status=active&page=2&page_size=500`,
    );
    assert.equal(calls[0].options.credentials, 'include');
    assert.equal(calls[0].options.body, undefined);
});
