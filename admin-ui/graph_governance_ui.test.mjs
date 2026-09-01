import assert from 'node:assert/strict';
import test from 'node:test';

import {
    advanceGraphCursor,
    approvedUnboundGraphActions,
    createGraphIntentKey,
    graphActionPageMatches,
    graphActionSelection,
    graphActionSummary,
    graphEntityDetailMatches,
    graphEntityPageMatches,
    graphErrorProjection,
    graphEvidenceMatches,
    graphEntityTypeLabel,
    graphFactLabel,
    graphGovernanceContextMatches,
    graphLibraryCapabilities,
    graphPublicationLabel,
    graphPublicationCommitMatches,
    graphPublicationListMatches,
    graphPublicationPreviewMatches,
    graphPublicationReadMatches,
    graphPropertyRows,
    graphRelationDetailMatches,
    graphRelationPageMatches,
    graphReviewLabel,
    graphRollbackPreviewMatches,
    graphRouteQuery,
    graphScopeOptions,
    resolveGraphScope,
    retreatGraphCursor,
} from './src/graph_governance_ui.js';

const ORG_A = '10000000-0000-4000-8000-000000000001';
const ORG_B = '10000000-0000-4000-8000-000000000002';
const LIB_A_ID = '20000000-0000-4000-8000-000000000001';
const ONTOLOGY_A = '30000000-0000-4000-8000-000000000001';
const ONTOLOGY_B = '30000000-0000-4000-8000-000000000002';
const ENTITY_A = '40000000-0000-4000-8000-000000000001';
const ENTITY_B = '40000000-0000-4000-8000-000000000002';
const RELATION_A = '50000000-0000-4000-8000-000000000001';
const EVIDENCE_A = '60000000-0000-4000-8000-000000000001';
const DOCUMENT_A = '70000000-0000-4000-8000-000000000001';
const REVISION_A = '80000000-0000-4000-8000-000000000001';
const ACTION_A = '90000000-0000-4000-8000-000000000001';
const ACTION_B = '90000000-0000-4000-8000-000000000002';
const PUBLICATION_A = 'a0000000-0000-4000-8000-000000000001';
const TYPE_A = 'b0000000-0000-4000-8000-000000000001';
const TYPE_B = 'b0000000-0000-4000-8000-000000000002';
const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

const permissions = [
    {
        organization_id: ORG_A,
        library_slug: 'legal-a',
        library_name: 'Legal A',
        actions: ['read', 'insert'],
    },
    {
        organization_id: ORG_A,
        library_slug: 'legal-b',
        library_name: 'Legal B',
        actions: ['read', 'admin'],
    },
    {
        organization_id: ORG_B,
        library_slug: 'finance',
        library_name: 'Finance',
        actions: ['read'],
    },
    {
        organization_id: ORG_B,
        library_slug: 'write-only',
        actions: ['insert'],
    },
];
const organizations = [
    { organization_id: ORG_A, name: 'Legal Org', role: 'member' },
    { organization_id: ORG_B, name: 'Finance Org', role: 'organization_admin' },
];

test('projects exact Organization and Library capabilities from effective rows', () => {
    assert.deepEqual(graphLibraryCapabilities(permissions, organizations, 'legal-a'), {
        read: true,
        insert: true,
        manage: false,
        organizationId: ORG_A,
    });
    assert.equal(graphLibraryCapabilities(permissions, organizations, 'legal-b').manage, true);
    assert.equal(graphLibraryCapabilities(permissions, organizations, 'finance').manage, true);
    assert.deepEqual(graphScopeOptions(permissions, organizations), [
        {
            id: ORG_A,
            name: 'Legal Org',
            libraries: [
                { slug: 'legal-a', name: 'Legal A', insert: true, manage: false },
                { slug: 'legal-b', name: 'Legal B', insert: false, manage: true },
            ],
        },
        {
            id: ORG_B,
            name: 'Finance Org',
            libraries: [
                { slug: 'finance', name: 'Finance', insert: false, manage: true },
            ],
        },
    ]);
});

test('defaults graph navigation to the browse workspace', () => {
    const scope = resolveGraphScope({}, permissions, organizations);
    assert.equal(scope.tab, 'browse');
    assert.equal(graphRouteQuery(scope).tab, 'browse');
});

test('normalizes route scope without crossing Organizations or retaining unsafe details', () => {
    const readScope = resolveGraphScope({
        tab: 'entities',
        organization: ORG_A,
        libraries: 'legal-b,finance,legal-a,legal-b',
        entity: ENTITY_A,
        relation: RELATION_A,
    }, permissions, organizations);
    assert.deepEqual(readScope, {
        tab: 'browse',
        organizationId: ORG_A,
        librarySlugs: ['legal-b', 'legal-a'],
        entityId: ENTITY_A,
        relationId: '',
    });
    assert.deepEqual(graphRouteQuery(readScope), {
        tab: 'browse',
        organization: ORG_A,
        libraries: 'legal-b,legal-a',
        entity: ENTITY_A,
    });

    const legacyExplore = resolveGraphScope({
        tab: 'explore',
        organization: ORG_A,
        libraries: 'legal-a',
        entity: ENTITY_A,
    }, permissions, organizations);
    assert.equal(legacyExplore.tab, 'browse');
    assert.equal(graphRouteQuery(legacyExplore).tab, 'browse');

    const managementScope = resolveGraphScope({
        tab: 'review',
        organization: ORG_A,
        libraries: 'legal-a,legal-b',
        entity: 'not-a-uuid',
    }, permissions, organizations);
    assert.deepEqual(managementScope, {
        tab: 'review',
        organizationId: ORG_A,
        librarySlugs: ['legal-b'],
        entityId: '',
        relationId: '',
    });
});

test('preserves opaque cursor history and uses bounded labels and errors', () => {
    const page2 = advanceGraphCursor({ history: [], current: null }, 'opaque-2');
    const page3 = advanceGraphCursor(page2, 'opaque-3');
    assert.deepEqual(retreatGraphCursor(page3), { history: [null], current: 'opaque-2' });
    assert.equal(graphEntityTypeLabel({ key: 'term', label: 'Term' }), '术语');
    assert.equal(graphEntityTypeLabel({ key: 'custom', label: 'Custom' }), 'Custom');
    assert.equal(graphFactLabel('active'), '生效');
    assert.equal(graphFactLabel('future'), '未知状态');
    assert.equal(graphReviewLabel('approved'), '已通过');
    assert.equal(graphPublicationLabel('superseded'), '已被替代');
    assert.deepEqual(graphErrorProjection({ status: 409, body: {
        detail: {
            code: 'graph_catalog_scope_incompatible',
            incompatibilities: [
                { library_slug: 'legal-a', reason_codes: ['graph_profile_mismatch'] },
                { library_slug: { secret: true }, reason_codes: ['provider secret'] },
            ],
            storage_url: 'must-not-render',
        },
    } }), {
        kind: 'conflict',
        message: '数据已发生变化，请重新加载后再操作。',
        conflicts: [{ librarySlug: 'legal-a', reasons: ['图谱配置不一致'] }],
    });
});

test('accepts only exact graph page and detail identities', () => {
    const entityPage = {
        contract_version: 'graph-catalog-entities-v1',
        items: [{ id: ENTITY_A, library: { slug: 'legal-a' } }],
        next_cursor: 'opaque',
    };
    assert.equal(graphEntityPageMatches(entityPage, { librarySlugs: ['legal-a'] }), true);
    assert.equal(graphEntityPageMatches(entityPage, { librarySlugs: ['legal-b'] }), false);
    assert.equal(graphRelationPageMatches({
        contract_version: 'graph-catalog-relations-v1',
        items: [{ id: RELATION_A, library: { slug: 'legal-b' } }],
        next_cursor: null,
    }, { librarySlugs: ['legal-a', 'legal-b'] }), true);

    assert.equal(graphEntityDetailMatches({
        contract_version: 'graph-catalog-entity-detail-v1',
        entity: { id: ENTITY_A, ontology_version_id: ONTOLOGY_A, library: { slug: 'legal-a' } },
    }, { entityId: ENTITY_A, ontologyVersionId: ONTOLOGY_A, librarySlug: 'legal-a' }), true);
    assert.equal(graphEntityDetailMatches({
        contract_version: 'graph-catalog-entity-detail-v1',
        entity: { id: ENTITY_B, ontology_version_id: ONTOLOGY_A, library: { slug: 'legal-a' } },
    }, { entityId: ENTITY_A, librarySlug: 'legal-a' }), false);
    assert.equal(graphRelationDetailMatches({
        contract_version: 'graph-catalog-relation-detail-v1',
        relation: { id: RELATION_A, ontology_version_id: ONTOLOGY_A, library: { slug: 'legal-a' } },
    }, { relationId: RELATION_A, ontologyVersionId: ONTOLOGY_A, librarySlug: 'legal-a' }), true);
});

test('validates Evidence and governance context before display', () => {
    const evidence = {
        contract_version: 'catalog-evidence-v1',
        evidence_id: EVIDENCE_A,
        library_id: LIB_A_ID,
        document_id: DOCUMENT_A,
        document_revision_id: REVISION_A,
        fact_refs: [{ fact_id: ENTITY_A, item_kind: 'entity', chunk_id: null }],
    };
    assert.equal(graphEvidenceMatches(evidence, {
        evidenceId: EVIDENCE_A,
        libraryId: LIB_A_ID,
        documentId: DOCUMENT_A,
        revisionId: REVISION_A,
        factId: ENTITY_A,
        factKind: 'entity',
    }), true);
    assert.equal(graphEvidenceMatches(evidence, {
        evidenceId: EVIDENCE_A,
        libraryId: LIB_A_ID,
        documentId: DOCUMENT_A,
        revisionId: 'wrong',
    }), false);

    const context = {
        contract_version: 'graph-governance-context-v1',
        library_id: LIB_A_ID,
        library_slug: 'legal-a',
        ontology_versions: [{
            id: ONTOLOGY_A,
            version_key: 'legal-v1',
            version_no: 1,
            entity_types: [{ id: TYPE_A, key: 'company', label: 'Company' }],
            relation_types: [{
                id: TYPE_B,
                key: 'invests',
                label: 'Invests',
                direction: 'directed',
                default_review_policy: 'pending_review',
                requires_evidence: true,
            }],
        }],
    };
    assert.equal(graphGovernanceContextMatches(context, {
        libraryId: LIB_A_ID,
        librarySlug: 'legal-a',
    }), true);
    const deterministicLibraryId = '00000000-0000-0000-0000-000000000001';
    assert.equal(graphGovernanceContextMatches({ ...context, library_id: deterministicLibraryId }, {
        libraryId: deterministicLibraryId,
        librarySlug: 'legal-a',
    }), true);
    assert.equal(graphGovernanceContextMatches({ ...context, library_slug: 'other' }, {
        libraryId: LIB_A_ID,
        librarySlug: 'legal-a',
    }), false);
});

test('renders bounded property rows without dumping nested metadata', () => {
    assert.deepEqual(graphPropertyRows({
        country: 'CN',
        active: true,
        tags: ['important', 'reviewed'],
        private_trace: { provider: 'secret' },
    }), [
        { key: 'country', value: 'CN' },
        { key: 'active', value: 'true' },
        { key: 'tags', value: 'important、reviewed' },
        { key: 'private_trace', value: '结构化值' },
    ]);
});

test('projects action summaries through kind-specific allowlists only', () => {
    assert.deepEqual(graphActionSummary({
        action_kind: 'entity_create',
        payload: {
            canonical_name: 'Acme',
            entity_type_id: TYPE_A,
            properties: { tax_id: 'secret' },
            storage_url: 'https://objects.invalid/private',
        },
    }), {
        label: '新增实体',
        fields: [
            { key: 'canonical_name', label: '实体名称', value: 'Acme' },
            { key: 'entity_type_id', label: '实体类型', value: TYPE_A },
        ],
    });
    assert.deepEqual(graphActionSummary({ action_kind: 'future', payload: { secret: 'x' } }), {
        label: '未知操作',
        fields: [],
    });
});

test('accepts only exact bounded governance action pages', () => {
    const page = {
        total: 1,
        limit: 20,
        offset: 0,
        items: [{
            id: ACTION_A,
            library_id: LIB_A_ID,
            ontology_version_id: ONTOLOGY_A,
            action_kind: 'entity_create',
        }],
    };
    assert.equal(graphActionPageMatches(page, { libraryId: LIB_A_ID, limit: 20, offset: 0 }), true);
    assert.equal(graphActionPageMatches({
        ...page,
        items: [{ ...page.items[0], library_id: 'wrong' }],
    }, { libraryId: LIB_A_ID, limit: 20, offset: 0 }), false);
    assert.equal(graphActionPageMatches({
        ...page,
        items: [{ ...page.items[0], action_kind: 'future_internal_action' }],
    }, { libraryId: LIB_A_ID, limit: 20, offset: 0 }), false);
});

test('selects only approved unbound actions from one Ontology', () => {
    const actions = [
        { id: ACTION_A, ontology_version_id: ONTOLOGY_A, status: 'approved', planned_publication_id: null },
        { id: ACTION_B, ontology_version_id: ONTOLOGY_B, status: 'approved', planned_publication_id: null },
        { id: '90000000-0000-4000-8000-000000000003', ontology_version_id: ONTOLOGY_A, status: 'pending_review', planned_publication_id: null },
    ];
    assert.deepEqual(approvedUnboundGraphActions(actions, ONTOLOGY_A), [actions[0]]);
    assert.deepEqual(graphActionSelection(actions, [ACTION_A]), {
        actionIds: [ACTION_A],
        ontologyVersionId: ONTOLOGY_A,
    });
    assert.equal(graphActionSelection(actions, [ACTION_A, ACTION_B]), null);
});

test('fences Publication previews and creates bounded per-intent keys', () => {
    const preview = {
        publication_id: PUBLICATION_A,
        manifest_hash: HASH_A,
        action_set_hash: HASH_B,
        parent_publication_id: null,
        dry_run: true,
        entity_count: 2,
        relation_count: 1,
    };
    assert.equal(graphPublicationPreviewMatches(preview, { parentPublicationId: null }), true);
    assert.equal(graphPublicationPreviewMatches({ ...preview, dry_run: false }, {
        parentPublicationId: null,
    }), false);
    assert.equal(graphPublicationPreviewMatches(preview, { manifestHash: HASH_B }), false);
    assert.equal(graphPublicationCommitMatches({ ...preview, dry_run: false, status: 'planned' }, preview), true);
    assert.equal(graphPublicationCommitMatches({
        ...preview,
        publication_id: 'a0000000-0000-4000-8000-000000000002',
        dry_run: false,
        status: 'planned',
    }, preview), false);
    assert.equal(graphPublicationCommitMatches({
        ...preview,
        dry_run: false,
        status: 'planned',
        action_set_hash: HASH_A,
    }, preview), false);
    assert.equal(graphPublicationListMatches({
        items: [{
            id: PUBLICATION_A,
            library_id: LIB_A_ID,
            ontology_version_id: ONTOLOGY_A,
            status: 'active',
            source_mode: 'manual_plan',
            publication_enabled: true,
            manifest_hash: HASH_A,
            entity_count: 2,
            relation_count: 1,
        }],
        total: 1,
        page: 1,
        page_size: 50,
        publication_enabled: true,
    }, {
        libraryId: LIB_A_ID,
        ontologyVersionId: ONTOLOGY_A,
        page: 1,
        pageSize: 50,
    }), true);
    assert.equal(graphPublicationListMatches({
        items: [{
            id: PUBLICATION_A,
            library_id: LIB_A_ID,
            ontology_version_id: ONTOLOGY_A,
            status: 'active',
            source_mode: 'manual_plan',
            publication_enabled: false,
            manifest_hash: HASH_A,
            entity_count: 2,
            relation_count: 1,
        }],
        total: 1,
        page: 1,
        page_size: 50,
        publication_enabled: true,
    }, {
        libraryId: LIB_A_ID,
        ontologyVersionId: ONTOLOGY_A,
        page: 1,
        pageSize: 50,
    }), false);
    assert.equal(graphPublicationReadMatches({
        id: PUBLICATION_A,
        library_id: LIB_A_ID,
        ontology_version_id: ONTOLOGY_A,
        status: 'active',
        source_mode: 'manual_plan',
        publication_enabled: true,
        manifest_hash: HASH_A,
        entity_count: 2,
        relation_count: 1,
    }, {
        publicationId: PUBLICATION_A,
        libraryId: LIB_A_ID,
        ontologyVersionId: ONTOLOGY_A,
        status: 'active',
        manifestHash: HASH_A,
    }), true);
    assert.equal(graphRollbackPreviewMatches({
        id: PUBLICATION_A,
        library_id: LIB_A_ID,
        ontology_version_id: ONTOLOGY_A,
        rollback_target_publication_id: 'a0000000-0000-4000-8000-000000000002',
        source_mode: 'rollback',
        status: 'planned',
        dry_run: true,
        manifest_hash: HASH_A,
    }, {
        libraryId: LIB_A_ID,
        ontologyVersionId: ONTOLOGY_A,
        targetPublicationId: 'a0000000-0000-4000-8000-000000000002',
        dryRun: true,
    }), true);
    assert.equal(
        createGraphIntentKey('entity create', () => 'c0000000-0000-4000-8000-000000000001'),
        'entity-create:c0000000-0000-4000-8000-000000000001',
    );
});
