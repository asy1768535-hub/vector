import assert from 'node:assert/strict';
import test from 'node:test';

import {
    activateSchemaVersion,
    cloneSchemaVersion,
    createSchemaAttribute,
    createSchemaConstraint,
    createSchemaEntityType,
    createSchemaRelationType,
    disableSchemaItem,
    getSchemaImpact,
    getSchemaVersion,
    listSchemaVersions,
    updateSchemaItem,
    validateSchemaVersion,
} from './src/api.js';

const VERSION = '10000000-0000-4000-8000-000000000001';
const ITEM = '20000000-0000-4000-8000-000000000001';
const HASH = 'a'.repeat(64);

async function capture(run) {
    const calls = [];
    const prior = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
        calls.push({ url: String(url), options });
        return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try { await run(); } finally { globalThis.fetch = prior; }
    return calls;
}

const body = (call) => JSON.parse(call.options.body);

test('Schema lifecycle clients use exact read and command routes', async () => {
    const calls = await capture(async () => {
        await listSchemaVersions('enterprise-kb');
        await getSchemaVersion('enterprise-kb', VERSION);
        await validateSchemaVersion('enterprise-kb', VERSION);
        await getSchemaImpact('enterprise-kb', VERSION);
        await cloneSchemaVersion('enterprise-kb', VERSION, {
            expected_version_state_hash: HASH,
            idempotency_key: 'clone-1',
            description: 'Draft',
            source_text: 'forbidden',
        });
        await activateSchemaVersion('enterprise-kb', VERSION, {
            expected_version_state_hash: HASH,
            expected_active_version_id: null,
            confirmation: 'activate_schema_version',
            idempotency_key: 'activate-1',
            force: true,
        });
    });
    assert.equal(calls[0].url, '/libraries/enterprise-kb/schema-lifecycle/versions');
    assert.equal(calls[1].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}`);
    assert.equal(calls[2].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/validate`);
    assert.equal(calls[3].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/impact`);
    assert.deepEqual(body(calls[4]), {
        expected_version_state_hash: HASH,
        idempotency_key: 'clone-1',
        description: 'Draft',
    });
    assert.deepEqual(body(calls[5]), {
        expected_version_state_hash: HASH,
        expected_active_version_id: null,
        confirmation: 'activate_schema_version',
        idempotency_key: 'activate-1',
    });
});

test('Schema item clients preserve only type-specific allowlists', async () => {
    const common = {
        expected_version_state_hash: HASH,
        idempotency_key: 'item-1',
        key: 'company',
        label: 'Company',
        description: null,
        properties_schema: { type: 'object' },
        direction: 'directed',
        requires_evidence: true,
        default_review_policy: 'pending_review',
        owner_kind: 'entity_type',
        owner_type_id: ITEM,
        value_type: 'string',
        required: false,
        enum_values: null,
        validation_schema: null,
        indexed: true,
        relation_type_id: ITEM,
        source_entity_type_id: ITEM,
        target_entity_type_id: ITEM,
        cardinality: 'many_to_many',
        requires_review: true,
        prompt: 'forbidden',
    };
    const calls = await capture(async () => {
        await createSchemaEntityType('enterprise-kb', VERSION, common);
        await createSchemaRelationType('enterprise-kb', VERSION, common);
        await createSchemaAttribute('enterprise-kb', VERSION, common);
        await createSchemaConstraint('enterprise-kb', VERSION, common);
        await updateSchemaItem('enterprise-kb', VERSION, 'entity-types', ITEM, common);
        await disableSchemaItem('enterprise-kb', VERSION, 'entity-types', ITEM, common);
    });
    assert.deepEqual(body(calls[0]), {
        expected_version_state_hash: HASH,
        idempotency_key: 'item-1',
        key: 'company',
        label: 'Company',
        description: null,
        properties_schema: { type: 'object' },
    });
    assert.equal(calls[1].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/relation-types`);
    assert.equal(calls[2].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/attributes`);
    assert.equal(calls[3].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/constraints`);
    assert.equal(calls[4].options.method, 'PATCH');
    assert.equal(calls[4].url, `/libraries/enterprise-kb/schema-lifecycle/versions/${VERSION}/entity-types/${ITEM}`);
    assert.deepEqual(body(calls[5]), {
        expected_version_state_hash: HASH,
        idempotency_key: 'item-1',
    });
});
