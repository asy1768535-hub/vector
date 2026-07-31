import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    graphActionSummary,
    graphLibraryCapabilities,
    resolveGraphScope,
} from './src/graph_governance_ui.js';

const view = readFileSync(new URL('./src/views/GraphGovernance.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const schema = readFileSync(new URL('../app/schemas/graph_catalog.py', import.meta.url), 'utf8');
const detailService = readFileSync(
    new URL('../app/services/graph_catalog_details.py', import.meta.url),
    'utf8',
);

test('write and management capabilities remain exact to one Library', () => {
    const permissions = [
        { organization_id: 'org-a', library_slug: 'writer', actions: ['read', 'insert'] },
        { organization_id: 'org-a', library_slug: 'manager', actions: ['read', 'admin'] },
        { organization_id: 'org-b', library_slug: 'org-managed', actions: ['read'] },
        { organization_id: 'org-c', library_slug: 'reader', actions: ['read'] },
    ];
    const organizations = [
        { organization_id: 'org-b', role: 'organization_admin' },
        { organization_id: 'org-c', role: 'member' },
    ];
    assert.deepEqual(graphLibraryCapabilities(permissions, organizations, 'writer'), {
        read: true,
        insert: true,
        manage: false,
        organizationId: 'org-a',
    });
    assert.equal(graphLibraryCapabilities(permissions, organizations, 'manager').manage, true);
    assert.equal(graphLibraryCapabilities(permissions, organizations, 'org-managed').manage, true);
    assert.equal(graphLibraryCapabilities(permissions, organizations, 'reader').manage, false);
    assert.deepEqual(resolveGraphScope({
        tab: 'review',
        organization: 'org-a',
        libraries: 'writer,manager',
    }, permissions, organizations).librarySlugs, ['manager']);
});

test('manual fact and correction forms use active context and exact state fences', () => {
    for (const token of [
        'getGraphGovernanceContext',
        'graphGovernanceContextMatches',
        'contextRequestSeq',
        'factDialog.idempotencyKey',
        'createGraphIntentKey(`${kind}-create`)',
        'api.submitGraphEntity',
        'ontology_version_id: factDialog.ontologyId',
        'entity_type_id: factDialog.typeId',
        'api.submitGraphRelation',
        'relation_type_id: factDialog.typeId',
        'source_entity_id: factDialog.sourceEntityId',
        'target_entity_id: factDialog.targetEntityId',
        'api.correctGraphEntity',
        'expected_state_hash: correctionDialog.expectedStateHash',
        'api.correctGraphRelation',
        'api.addGraphEntityAlias',
        'expected_entity_state_hash: aliasDialog.expectedStateHash',
    ]) assert.ok(view.includes(token), `missing submitted-fact contract ${token}`);
    assert.match(view, /v-if="canWrite && \['browse', 'entities'\]\.includes\(scope\.tab\)"/);
    assert.match(view, /v-if="canWrite && \['browse', 'relations'\]\.includes\(scope\.tab\)"/);
});

test('management commands carry state hashes, fixed reasons, confirmations and no retry loop', () => {
    for (const token of [
        'ElMessageBox.confirm',
        'api.disableGraphEntity',
        'api.restoreGraphEntity',
        'api.disableGraphRelation',
        'api.restoreGraphRelation',
        'api.disableGraphAlias',
        'expected_state_hash: fact.governance_state_hash',
        "reason_code: 'operator_request'",
        'api.mergeGraphEntities',
        'expected_survivor_state_hash: mergeDialog.expectedSurvivorStateHash',
        'expected_loser_state_hash: loser.governance_state_hash',
        'resolutions: []',
        '存在冲突关系，请先修正或停用冲突关系并完成发布，再重试合并。',
        'api.rerunGraphExtraction',
        'client_idempotency_key: createGraphIntentKey',
        'rerun_of_job_id',
    ]) assert.ok(view.includes(token), `missing management command ${token}`);
    assert.match(view, /if \(mutation\.error\.kind === 'conflict'\)[\s\S]*refreshGovernanceSurfaces\(\)/);
    assert.doesNotMatch(view, /if \([^)]*conflict[^)]*\)[\s\S]{0,180}api\.(submit|correct|disable|restore|merge|review)/);
});

test('review queues are bounded batch reads with exact decision fences', () => {
    for (const token of [
        'reviewRequestSeq',
        'Promise.all([',
        'api.listGraphGovernanceActions',
        "statuses: ['pending_review', 'approved']",
        'api.searchGraphRelations',
        "review_statuses: ['pending_review']",
        'graphActionPageMatches',
        'graphRelationPageMatches',
        'graphActionSummary(action)',
        'api.decideGraphGovernanceAction',
        "expected_status: 'pending_review'",
        'api.reviewGraphRelation',
        'expected_state_hash: relation.governance_state_hash',
        'api.cancelGraphGovernanceAction',
    ]) assert.ok(view.includes(token), `missing review contract ${token}`);
    assert.doesNotMatch(view, /v-for="action[^>]*>[\s\S]{0,300}getGraphGovernanceAction/);
});

test('action summaries never expose arbitrary payload fields', () => {
    const summary = graphActionSummary({
        action_kind: 'relation_correct',
        payload: {
            source_entity_id: 'source',
            target_entity_id: 'target',
            properties: { secret: true },
            prompt: 'private prompt',
            url: 'https://objects.invalid/private',
        },
    });
    assert.deepEqual(summary.fields, [
        { key: 'source_entity_id', label: '源实体', value: 'source' },
        { key: 'target_entity_id', label: '目标实体', value: 'target' },
    ]);
    assert.ok(!view.includes('JSON.stringify(action.payload)'));
    assert.ok(!view.includes('v-html'));
    assert.ok(!view.includes('console.'));
});

test('Alias detail repeats a server-owned governance hash', () => {
    assert.match(
        schema,
        /class GraphCatalogAliasRead[\s\S]*governance_state_hash:\s*str/,
    );
    assert.match(
        detailService,
        /governance_state_hash=alias_governance_state_hash\(row\)/,
    );
    assert.match(view, /alias\.governance_state_hash/);
});

test('strict API module exports every Stage 3 command path', () => {
    for (const token of [
        'submitGraphEntity',
        'submitGraphRelation',
        'correctGraphEntity',
        'correctGraphRelation',
        'addGraphEntityAlias',
        'decideGraphGovernanceAction',
        'cancelGraphGovernanceAction',
        'reviewGraphRelation',
        'disableGraphEntity',
        'restoreGraphEntity',
        'disableGraphRelation',
        'restoreGraphRelation',
        'disableGraphAlias',
        'mergeGraphEntities',
        'rerunGraphExtraction',
    ]) assert.ok(api.includes(`export const ${token}`), `missing API export ${token}`);
});
