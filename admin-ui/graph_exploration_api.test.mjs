import assert from 'node:assert/strict';
import test from 'node:test';

import { queryPublishedGraph } from './src/api.js';

const ONTOLOGY = '10000000-0000-4000-8000-000000000001';
const PUBLICATION = '20000000-0000-4000-8000-000000000001';
const ENTITY = '30000000-0000-4000-8000-000000000001';

test('published graph client uses the v0.6 path and a bounded allowlisted body', async () => {
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
        await queryPublishedGraph('legal-a', {
            ontology_version_id: ONTOLOGY,
            expected_publication_id: PUBLICATION,
            seeds: [{ entity_id: ENTITY, canonical_name: 'must-not-leak' }],
            direction: 'outbound',
            relation_type_keys: [
                'invests', 'owns', 'invests', 'controls', 'supplies',
                'manages', 'employs', 'located_in', 'ninth-is-dropped', '',
            ],
            max_hops: 9,
            max_nodes: 900,
            max_relations: 900,
            include_evidence_locators: false,
            query: 'must-not-leak',
            properties: { secret: true },
        });
    } finally {
        globalThis.fetch = priorFetch;
    }

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/libraries/legal-a/v06/graph/query');
    assert.equal(calls[0].options.method, 'POST');
    assert.equal(calls[0].options.credentials, 'include');
    assert.deepEqual(JSON.parse(calls[0].options.body), {
        ontology_version_id: ONTOLOGY,
        expected_publication_id: PUBLICATION,
        seeds: [{ entity_id: ENTITY }],
        direction: 'outbound',
        relation_type_keys: [
            'invests', 'owns', 'controls', 'supplies',
            'manages', 'employs', 'located_in', 'ninth-is-dropped',
        ],
        max_hops: 2,
        max_nodes: 100,
        max_relations: 200,
        include_evidence_locators: true,
    });
});

test('published graph client applies safe control defaults', async () => {
    let requestBody = null;
    const priorFetch = globalThis.fetch;
    globalThis.fetch = async (_url, options = {}) => {
        requestBody = JSON.parse(options.body);
        return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await queryPublishedGraph('legal-a', {
            ontology_version_id: ONTOLOGY,
            expected_publication_id: PUBLICATION,
            seeds: [{ entity_id: ENTITY }],
            direction: 'sideways',
            max_hops: 0,
            max_nodes: 0,
            max_relations: 0,
        });
    } finally {
        globalThis.fetch = priorFetch;
    }

    assert.deepEqual(requestBody, {
        ontology_version_id: ONTOLOGY,
        expected_publication_id: PUBLICATION,
        seeds: [{ entity_id: ENTITY }],
        direction: 'both',
        relation_type_keys: [],
        max_hops: 1,
        max_nodes: 1,
        max_relations: 1,
        include_evidence_locators: true,
    });
});
