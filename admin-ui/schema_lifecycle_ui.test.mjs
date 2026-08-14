import assert from 'node:assert/strict';
import test from 'node:test';

import {
    normalizeSchemaRoute,
    schemaCanEdit,
    schemaErrorKind,
    schemaErrorMessage,
    schemaImpactMatches,
    schemaIssueLabel,
    schemaRouteQuery,
    schemaStatusLabel,
    schemaValidationMatches,
    schemaVersionDeletionMatches,
    schemaVersionDetailMatches,
    schemaVersionListMatches,
} from './src/schema_lifecycle_ui.js';

const LIBRARY = '10000000-0000-4000-8000-000000000001';
const VERSION = '20000000-0000-4000-8000-000000000001';
const HASH = 'a'.repeat(64);

test('Schema route normalization retains only known tab and selected identities', () => {
    assert.deepEqual(normalizeSchemaRoute({
        library: ' enterprise-kb ', version: VERSION, tab: 'attributes', raw: 'drop',
    }), { librarySlug: 'enterprise-kb', versionId: VERSION, tab: 'attributes' });
    assert.deepEqual(normalizeSchemaRoute({ tab: 'unknown' }), {
        librarySlug: '', versionId: '', tab: 'overview',
    });
    assert.deepEqual(schemaRouteQuery({
        librarySlug: 'enterprise-kb', versionId: VERSION, tab: 'impact',
    }), { library: 'enterprise-kb', version: VERSION, tab: 'impact' });
});

test('Schema response identity rejects cross-Library and stale-version payloads', () => {
    const version = {
        id: VERSION,
        library_id: LIBRARY,
        library_slug: 'enterprise-kb',
        state_hash: HASH,
        version_key: 'enterprise',
        version_no: 2,
        status: 'draft',
        entity_type_count: 0,
        relation_type_count: 0,
        attribute_count: 0,
        constraint_count: 0,
        entity_types: [], relation_types: [], attributes: [], constraints: [],
    };
    assert.equal(schemaVersionListMatches({
        library_id: LIBRARY,
        library_slug: 'enterprise-kb',
        versions: [version],
    }, { libraryId: LIBRARY, librarySlug: 'enterprise-kb' }), true);
    assert.equal(schemaVersionDetailMatches(version, {
        libraryId: LIBRARY, librarySlug: 'enterprise-kb', versionId: VERSION,
    }), true);
    assert.equal(schemaVersionDetailMatches({ ...version, library_id: 'other' }, {
        libraryId: LIBRARY, librarySlug: 'enterprise-kb', versionId: VERSION,
    }), false);
    const deletion = {
        action_id: '30000000-0000-4000-8000-000000000001',
        reused: false,
        library_id: LIBRARY,
        ontology_version_id: VERSION,
        status: 'deleted',
    };
    assert.equal(schemaVersionDeletionMatches(deletion, {
        libraryId: LIBRARY, versionId: VERSION,
    }), true);
    assert.equal(schemaVersionDeletionMatches({ ...deletion, ontology_version_id: LIBRARY }, {
        libraryId: LIBRARY, versionId: VERSION,
    }), false);
    const emptyDiff = { added: [], removed: [], changed: [] };
    const emptyReferences = {
        source_version_count: 0,
        draft_version_count: 0,
        source_current_ids: [],
        draft_current_ids: [],
    };
    const impact = {
        library_id: LIBRARY,
        ontology_version_id: VERSION,
        version_state_hash: HASH,
        entity_types: emptyDiff,
        relation_types: emptyDiff,
        attributes: emptyDiff,
        constraints: emptyDiff,
        extraction_jobs: emptyReferences,
        publications: emptyReferences,
        compatibility: [],
        historical_rows_migrated: false,
        retrieval_scope_changed: false,
    };
    assert.equal(schemaImpactMatches(impact, {
        libraryId: LIBRARY, versionId: VERSION, stateHash: HASH,
    }), true);
    assert.equal(schemaImpactMatches({ ...impact, version_state_hash: 'b'.repeat(64) }, {
        libraryId: LIBRARY, versionId: VERSION, stateHash: HASH,
    }), false);
    assert.equal(schemaValidationMatches({
        library_id: LIBRARY,
        ontology_version_id: VERSION,
        version_state_hash: HASH,
        valid: true,
        issues: [],
    }, { libraryId: LIBRARY, versionId: VERSION, stateHash: HASH }), true);
    assert.equal(schemaValidationMatches({
        library_id: 'other',
        ontology_version_id: VERSION,
        version_state_hash: HASH,
        valid: true,
        issues: [],
    }, { libraryId: LIBRARY, versionId: VERSION, stateHash: HASH }), false);
});

test('Schema detail rejects cross-scope children and inconsistent counts', () => {
    const child = {
        id: '30000000-0000-4000-8000-000000000001',
        library_id: LIBRARY,
        ontology_version_id: VERSION,
        status: 'draft',
        state_hash: HASH,
    };
    const detail = {
        id: VERSION,
        library_id: LIBRARY,
        library_slug: 'enterprise-kb',
        state_hash: HASH,
        version_key: 'enterprise',
        version_no: 2,
        status: 'draft',
        entity_type_count: 1,
        relation_type_count: 0,
        attribute_count: 0,
        constraint_count: 0,
        entity_types: [child], relation_types: [], attributes: [], constraints: [],
    };
    const identity = { libraryId: LIBRARY, librarySlug: 'enterprise-kb', versionId: VERSION };
    assert.equal(schemaVersionDetailMatches(detail, identity), true);
    assert.equal(schemaVersionDetailMatches({
        ...detail,
        entity_types: [{ ...child, ontology_version_id: '40000000-0000-4000-8000-000000000001' }],
    }, identity), false);
    assert.equal(schemaVersionDetailMatches({ ...detail, entity_type_count: 0 }, identity), false);
});

test('Schema state and errors use fixed labels without raw server content', () => {
    assert.equal(schemaCanEdit({ status: 'draft' }), true);
    assert.equal(schemaCanEdit({ status: 'active' }), false);
    assert.equal(schemaStatusLabel('active'), '已激活');
    assert.equal(schemaStatusLabel('unknown'), '未知状态');
    assert.equal(schemaIssueLabel({ code: 'attribute_owner_missing' }), '属性所属类型不可用');
    assert.equal(schemaErrorKind({
        status: 409,
        body: { detail: 'schema_lifecycle_dependency_conflict' },
    }), 'dependency');
    assert.equal(schemaErrorMessage('dependency'), '该 Schema 仍被图谱数据、任务或其他版本引用，不能删除或停用');
    for (const [status, kind] of [[404, 'unavailable'], [403, 'forbidden'], [409, 'conflict'], [422, 'invalid'], [503, 'unavailable']]) {
        const error = { status, message: 'raw secret prompt storage_url' };
        assert.equal(schemaErrorKind(error), kind);
        assert.equal(schemaErrorMessage(kind).includes('secret'), false);
        assert.equal(schemaErrorMessage(kind).includes('storage_url'), false);
    }
});
