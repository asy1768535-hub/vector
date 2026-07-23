import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    approvedUnboundGraphActions,
    graphActionSelection,
    graphPublicationCommitMatches,
    graphPublicationPreviewMatches,
    graphPublicationReadMatches,
    graphRollbackPreviewMatches,
} from './src/graph_governance_ui.js';

const view = readFileSync(new URL('./src/views/GraphGovernance.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

const LIBRARY = '10000000-0000-4000-8000-000000000001';
const ONTOLOGY_A = '20000000-0000-4000-8000-000000000001';
const ONTOLOGY_B = '20000000-0000-4000-8000-000000000002';
const ACTION_A = '30000000-0000-4000-8000-000000000001';
const ACTION_B = '30000000-0000-4000-8000-000000000002';
const PUBLICATION = '40000000-0000-4000-8000-000000000001';
const OTHER_PUBLICATION = '40000000-0000-4000-8000-000000000002';
const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

test('selects approved unbound actions from exactly one Ontology', () => {
    const actions = [
        { id: ACTION_A, ontology_version_id: ONTOLOGY_A, status: 'approved', planned_publication_id: null },
        { id: ACTION_B, ontology_version_id: ONTOLOGY_B, status: 'approved', planned_publication_id: null },
        { id: '30000000-0000-4000-8000-000000000003', ontology_version_id: ONTOLOGY_A, status: 'approved', planned_publication_id: PUBLICATION },
    ];
    assert.deepEqual(approvedUnboundGraphActions(actions, ONTOLOGY_A), [actions[0]]);
    assert.deepEqual(graphActionSelection(actions, [ACTION_A]), {
        actionIds: [ACTION_A],
        ontologyVersionId: ONTOLOGY_A,
    });
    assert.equal(graphActionSelection(actions, [ACTION_A, ACTION_B]), null);
});

test('loads an exact current Publication instead of inferring the parent from recent history', () => {
    for (const token of [
        'publicationRequestSeq',
        'api.getActiveGraphPublication',
        'graphPublicationReadMatches(current.data, identity)',
        "!['active', 'degraded'].includes(current.data.status)",
        'publications.current = current.data',
        'const parentPublicationId = activePublication.value?.id || null',
        'expected_parent_publication_id: identity.parentPublicationId',
    ]) assert.ok(view.includes(token), `missing exact current-parent contract ${token}`);

    assert.equal(graphPublicationReadMatches({
        id: PUBLICATION,
        library_id: LIBRARY,
        ontology_version_id: ONTOLOGY_A,
        status: 'active',
        source_mode: 'manual_plan',
        publication_enabled: true,
        manifest_hash: HASH_A,
        entity_count: 3,
        relation_count: 2,
    }, {
        libraryId: LIBRARY,
        ontologyVersionId: ONTOLOGY_A,
        publicationId: PUBLICATION,
        status: 'active',
    }), true);
});

test('uses one stable intent and one stable preview identity for dry-run and commit', () => {
    const preview = {
        publication_id: PUBLICATION,
        manifest_hash: HASH_A,
        action_set_hash: HASH_B,
        parent_publication_id: OTHER_PUBLICATION,
        dry_run: true,
        entity_count: 3,
        relation_count: 2,
    };
    assert.equal(graphPublicationPreviewMatches(preview, {
        parentPublicationId: OTHER_PUBLICATION,
    }), true);
    assert.equal(graphPublicationCommitMatches({
        ...preview,
        status: 'planned',
        dry_run: false,
    }, preview), true);
    assert.equal(graphPublicationCommitMatches({
        ...preview,
        publication_id: '40000000-0000-4000-8000-000000000003',
        status: 'planned',
        dry_run: false,
    }, preview), false);

    assert.match(view, /dry_run:\s*true,[\s\S]*idempotency_key:\s*identity\.intentKey/);
    assert.match(view, /dry_run:\s*false,[\s\S]*idempotency_key:\s*preview\.intentKey/);
    assert.match(view, /graphPublicationPreviewMatches,[\s\S]*graphPublicationReadMatches,/);
    assert.match(view, /graphPublicationCommitMatches\(result, preview\)/);
});

test('invalidates stale previews and suppresses duplicate Publication commands', () => {
    for (const token of [
        '@change="changePublicationActions"',
        'sameValues(selection.actionIds, preview.actionIds)',
        "publications.error = { ...MALFORMED_ERROR, message: '发布预览已过期，请重新预览。' }",
        'if (!preview || !publications.enabled || publications.mutationKind',
        "publications.mutationKind = 'commit-confirm'",
        "publications.mutationKind = 'activate-confirm'",
        "publications.mutationKind = 'cancel-confirm'",
        "publications.mutationKind = 'rollback-preview'",
    ]) assert.ok(view.includes(token), `missing preview or duplicate-submit fence ${token}`);
});

test('activates and cancels only a verified governance plan with exact Manifest fencing', () => {
    for (const token of [
        'canOperatePlannedPublication(publication)',
        "publication.source_mode === 'rollback'",
        'governancePlannedPublicationIds.value.has(String(publication.id))',
        'expected_manifest_hash: identity.manifestHash',
        "graphPublicationReadMatches(result, { ...identity, status: 'active' })",
        "graphPublicationReadMatches(result, { ...identity, status: 'cancelled' })",
        "reason_code: 'operator_cancelled'",
    ]) assert.ok(view.includes(token), `missing activation/cancellation fence ${token}`);
    assert.match(view, /confirmHighImpact\('激活发布版本'/);
    assert.match(view, /confirmHighImpact\('取消发布版本'/);
});

test('pairs rollback dry-run and persistence with the same intent and Manifest', () => {
    const rollback = {
        id: PUBLICATION,
        library_id: LIBRARY,
        ontology_version_id: ONTOLOGY_A,
        rollback_target_publication_id: OTHER_PUBLICATION,
        source_mode: 'rollback',
        status: 'planned',
        dry_run: true,
        manifest_hash: HASH_A,
    };
    assert.equal(graphRollbackPreviewMatches(rollback, {
        libraryId: LIBRARY,
        ontologyVersionId: ONTOLOGY_A,
        targetPublicationId: OTHER_PUBLICATION,
        dryRun: true,
    }), true);
    assert.match(view, /idempotency_key:\s*identity\.intentKey, dry_run:\s*true/);
    assert.match(view, /idempotency_key:\s*identity\.intentKey, dry_run:\s*false/);
    assert.match(view, /result\.manifest_hash !== preview\.manifest_hash/);
});

test('keeps Publication controls management-only, fixed-error, private and responsive', () => {
    for (const token of [
        "scope.tab !== 'publications' || !canManage.value",
        '!canManage.value || !publications.enabled',
        'publications.error.message',
        'graph-publication-grid',
        'graph-publication-action-list',
        'graph-publication-preview',
        'graph-publication-item-actions',
    ]) assert.ok(view.includes(token) || css.includes(token), `missing permission/layout contract ${token}`);
    for (const forbidden of [
        'v-html',
        'console.',
        'JSON.stringify(publications',
        'storage_url',
        'object_key',
        'source_content',
    ]) assert.ok(!view.includes(forbidden), `forbidden Publication token ${forbidden}`);
    assert.match(css, /@media screen and \(max-width: 899px\)[\s\S]*\.graph-publication-grid\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
    assert.doesNotMatch(view, /if \([^)]*conflict[^)]*\)[\s\S]{0,180}api\.(planGraphGovernancePublication|activateGraphPublication|cancelGraphPublication|rollbackGraphPublication)/);
});
