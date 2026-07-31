import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';

import * as api from '../api.js';
import {
    catalogEvidenceParts,
    catalogPageLabel,
    catalogTitlePath,
    formatCatalogConfidence,
    formatCatalogTime,
    shortCatalogId,
} from '../catalog_ui.js';
import {
    advanceGraphCursor,
    createGraphIntentKey,
    graphActionPageMatches,
    graphActionSummary,
    graphEntityDetailMatches,
    graphEntityPageMatches,
    graphErrorProjection,
    graphEvidenceMatches,
    graphFactLabel,
    graphGovernanceContextMatches,
    graphLibraryCapabilities,
    graphPropertyRows,
    graphPublicationCommitMatches,
    graphPublicationLabel,
    graphPublicationListMatches,
    graphPublicationPreviewMatches,
    graphPublicationReadMatches,
    graphRelationDetailMatches,
    graphRelationPageMatches,
    graphReviewLabel,
    graphRollbackPreviewMatches,
    graphRouteQuery,
    graphScopeOptions,
    graphActionSelection,
    approvedUnboundGraphActions,
    resolveGraphScope,
    retreatGraphCursor,
} from '../graph_governance_ui.js';
import { store } from '../store.js';
import GraphExplorer from '../components/GraphExplorer.js';
import GraphKnowledgeBrowser from '../components/GraphKnowledgeBrowser.js';

const MALFORMED_ERROR = {
    kind: 'malformed',
    message: '服务返回的数据不完整，请重新加载。',
    conflicts: [],
};

function sameValues(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
}

function normalizedQuery(query) {
    const result = {};
    for (const key of ['tab', 'organization', 'libraries', 'entity', 'relation']) {
        if (query?.[key]) result[key] = String(query[key]);
    }
    return result;
}

function sourceTypeLabel(value) {
    return {
        manual: '人工',
        imported: '导入',
        extracted: '模型抽取',
    }[value] || '未知来源';
}

function publicationSourceLabel(value) {
    return {
        initial_seed: '初始发布',
        manual_plan: '治理发布',
        rollback: '回滚发布',
        coordinated_purge: '清理发布',
    }[value] || '未知来源';
}

function statusTag(value) {
    if (['active', 'approved', 'not_required', 'published'].includes(value)) return 'success';
    if (['draft', 'pending_review', 'staged', 'planned'].includes(value)) return 'warning';
    if (['rejected', 'failed'].includes(value)) return 'danger';
    return 'info';
}

export default {
    components: { GraphExplorer, GraphKnowledgeBrowser },
    setup() {
        const route = useRoute();
        const router = useRouter();
        const scope = reactive({
            tab: 'browse',
            organizationId: '',
            librarySlugs: [],
            entityId: '',
            relationId: '',
        });
        const filters = reactive({
            query: '',
            typeKey: '',
            status: '',
            sourceType: '',
            publicationState: 'all',
            reviewStatus: '',
        });
        const entityCursor = reactive({ history: [], current: null });
        const relationCursor = reactive({ history: [], current: null });
        const entityPage = reactive({ loading: false, data: null, error: null });
        const relationPage = reactive({ loading: false, data: null, error: null });
        const entityDetail = reactive({ open: false, loading: false, data: null, error: null });
        const relationDetail = reactive({ open: false, loading: false, data: null, error: null });
        const evidence = reactive({
            open: false,
            loading: false,
            data: null,
            error: null,
            identity: null,
            librarySlug: '',
        });
        const writeContext = reactive({ loading: false, data: null, error: null, librarySlug: '' });
        const entityOptions = reactive({ loading: false, items: [], error: null });
        const factDialog = reactive({
            open: false,
            kind: 'entity',
            ontologyId: '',
            typeId: '',
            canonicalName: '',
            sourceEntityId: '',
            targetEntityId: '',
            idempotencyKey: '',
        });
        const correctionDialog = reactive({
            open: false,
            kind: 'entity',
            targetId: '',
            expectedStateHash: '',
            canonicalName: '',
            sourceEntityId: '',
            targetEntityId: '',
            idempotencyKey: '',
        });
        const aliasDialog = reactive({
            open: false,
            entityId: '',
            expectedStateHash: '',
            alias: '',
            idempotencyKey: '',
        });
        const mergeDialog = reactive({
            open: false,
            survivorId: '',
            survivorName: '',
            ontologyId: '',
            typeKey: '',
            expectedSurvivorStateHash: '',
            loserId: '',
            reasonCode: 'duplicate_fact',
            idempotencyKey: '',
        });
        const reviewQueues = reactive({
            loading: false,
            error: null,
            actions: [],
            approvedActions: [],
            actionTotal: 0,
            relations: [],
        });
        const mutation = reactive({ loading: false, kind: '', error: null });
        const publications = reactive({
            loading: false,
            error: null,
            enabled: false,
            ontologyId: '',
            actions: [],
            selectedActionIds: [],
            history: [],
            current: null,
            preview: null,
            intentKey: '',
            mutationKind: '',
        });
        const routeSeq = ref(0);
        const explorerRefreshKey = ref(0);
        let entityRequestSeq = 0;
        let relationRequestSeq = 0;
        let entityDetailRequestSeq = 0;
        let relationDetailRequestSeq = 0;
        let evidenceRequestSeq = 0;
        let contextRequestSeq = 0;
        let entityOptionRequestSeq = 0;
        let reviewRequestSeq = 0;
        let mutationRequestSeq = 0;
        let publicationRequestSeq = 0;
        let publicationMutationSeq = 0;

        const organizations = computed(() => graphScopeOptions(
            store.permissions,
            store.organizations,
        ));
        const selectedOrganization = computed(() => organizations.value.find(
            (item) => item.id === scope.organizationId,
        ) || null);
        const showOrganizationSelector = computed(() => organizations.value.length > 1);
        const availableLibraries = computed(() => selectedOrganization.value?.libraries || []);
        const hasManagement = computed(() => organizations.value.some(
            (item) => item.libraries.some((library) => library.manage),
        ));
        const pageState = computed(() => scope.tab === 'relations' ? relationPage : entityPage);
        const currentCursor = computed(() => scope.tab === 'relations' ? relationCursor : entityCursor);
        const evidenceParts = computed(() => catalogEvidenceParts(evidence.data));
        const selectedLibrary = computed(() => (
            scope.librarySlugs.length === 1
                ? availableLibraries.value.find((item) => item.slug === scope.librarySlugs[0]) || null
                : null
        ));
        const selectedCapabilities = computed(() => graphLibraryCapabilities(
            store.permissions,
            store.organizations,
            selectedLibrary.value?.slug || '',
        ));
        const canWrite = computed(() => Boolean(selectedLibrary.value && selectedCapabilities.value.insert));
        const canManage = computed(() => Boolean(selectedLibrary.value && selectedCapabilities.value.manage));
        const factOntology = computed(() => (writeContext.data?.ontology_versions || []).find(
            (item) => String(item.id) === factDialog.ontologyId,
        ) || null);
        const factTypeOptions = computed(() => (
            factDialog.kind === 'entity'
                ? factOntology.value?.entity_types || []
                : factOntology.value?.relation_types || []
        ));
        const publicationActions = computed(() => approvedUnboundGraphActions(
            publications.actions,
            publications.ontologyId,
        ));
        const activePublication = computed(() => publications.current);
        const governancePlannedPublicationIds = computed(() => new Set(
            publications.actions
                .map((item) => String(item?.planned_publication_id || ''))
                .filter(Boolean),
        ));
        const workspaceLoading = computed(() => {
            if (scope.tab === 'review') return reviewQueues.loading;
            if (scope.tab === 'publications') return publications.loading;
            if (scope.tab === 'browse' || scope.tab === 'explore') return false;
            return pageState.value.loading;
        });

        function scopeIdentity() {
            return JSON.stringify({
                organizationId: scope.organizationId,
                librarySlugs: [...scope.librarySlugs],
                tab: scope.tab,
            });
        }

        function resetCursors() {
            entityCursor.history = [];
            entityCursor.current = null;
            relationCursor.history = [];
            relationCursor.current = null;
        }

        function invalidateDetails() {
            entityDetailRequestSeq += 1;
            relationDetailRequestSeq += 1;
            evidenceRequestSeq += 1;
            entityDetail.open = false;
            entityDetail.loading = false;
            entityDetail.data = null;
            entityDetail.error = null;
            relationDetail.open = false;
            relationDetail.loading = false;
            relationDetail.data = null;
            relationDetail.error = null;
            evidence.open = false;
            evidence.loading = false;
            evidence.data = null;
            evidence.error = null;
            evidence.identity = null;
            contextRequestSeq += 1;
            entityOptionRequestSeq += 1;
            reviewRequestSeq += 1;
            mutationRequestSeq += 1;
            publicationRequestSeq += 1;
            publicationMutationSeq += 1;
            writeContext.loading = false;
            writeContext.data = null;
            writeContext.error = null;
            writeContext.librarySlug = '';
            entityOptions.loading = false;
            entityOptions.items = [];
            entityOptions.error = null;
            factDialog.open = false;
            correctionDialog.open = false;
            aliasDialog.open = false;
            mergeDialog.open = false;
            reviewQueues.loading = false;
            reviewQueues.error = null;
            reviewQueues.actions = [];
            reviewQueues.approvedActions = [];
            reviewQueues.actionTotal = 0;
            reviewQueues.relations = [];
            mutation.loading = false;
            mutation.kind = '';
            mutation.error = null;
            publications.loading = false;
            publications.error = null;
            publications.enabled = false;
            publications.ontologyId = '';
            publications.actions = [];
            publications.selectedActionIds = [];
            publications.history = [];
            publications.current = null;
            publications.preview = null;
            publications.intentKey = '';
            publications.mutationKind = '';
        }

        function searchBody(cursor, relation = false) {
            const body = {
                library_slugs: [...scope.librarySlugs],
                ontology_version_ids: [],
                type_keys: filters.typeKey.trim() ? [filters.typeKey.trim()] : [],
                statuses: filters.status ? [filters.status] : [],
                source_types: filters.sourceType ? [filters.sourceType] : [],
                publication_state: filters.publicationState,
                cursor: cursor.current || undefined,
                limit: 50,
            };
            const query = filters.query.trim();
            if (query) body.query = query;
            if (relation) body.review_statuses = filters.reviewStatus ? [filters.reviewStatus] : [];
            return body;
        }

        async function loadWriteContext(force = false) {
            const librarySlug = selectedLibrary.value?.slug || '';
            if (!librarySlug || (!canWrite.value && !canManage.value)) return null;
            if (!force && writeContext.data && writeContext.librarySlug === librarySlug) {
                return writeContext.data;
            }
            const seq = ++contextRequestSeq;
            const identity = { librarySlug, scopeKey: scopeIdentity() };
            writeContext.loading = true;
            writeContext.error = null;
            writeContext.data = null;
            writeContext.librarySlug = librarySlug;
            try {
                const data = await api.getGraphGovernanceContext(librarySlug);
                if (seq !== contextRequestSeq || identity.scopeKey !== scopeIdentity()) return null;
                if (!graphGovernanceContextMatches(data, identity)) {
                    writeContext.error = MALFORMED_ERROR;
                    return null;
                }
                writeContext.data = data;
                return data;
            } catch (error) {
                if (seq !== contextRequestSeq || identity.scopeKey !== scopeIdentity()) return null;
                writeContext.error = graphErrorProjection(error);
                return null;
            } finally {
                if (seq === contextRequestSeq) writeContext.loading = false;
            }
        }

        async function loadEntityOptions({ ontologyId = '', typeKey = '', query = '' } = {}) {
            const librarySlug = selectedLibrary.value?.slug || '';
            if (!librarySlug || !scope.organizationId) return [];
            const seq = ++entityOptionRequestSeq;
            const identity = {
                librarySlugs: [librarySlug],
                scopeKey: scopeIdentity(),
            };
            entityOptions.loading = true;
            entityOptions.error = null;
            try {
                const data = await api.searchGraphEntities(scope.organizationId, {
                    library_slugs: [librarySlug],
                    query: query.trim() || undefined,
                    ontology_version_ids: ontologyId ? [ontologyId] : [],
                    type_keys: typeKey ? [typeKey] : [],
                    statuses: ['active'],
                    source_types: [],
                    publication_state: 'all',
                    limit: 100,
                });
                if (seq !== entityOptionRequestSeq || identity.scopeKey !== scopeIdentity()) return [];
                if (!graphEntityPageMatches(data, identity)) {
                    entityOptions.items = [];
                    entityOptions.error = MALFORMED_ERROR;
                    return [];
                }
                entityOptions.items = data.items;
                return data.items;
            } catch (error) {
                if (seq !== entityOptionRequestSeq || identity.scopeKey !== scopeIdentity()) return [];
                entityOptions.items = [];
                entityOptions.error = graphErrorProjection(error);
                return [];
            } finally {
                if (seq === entityOptionRequestSeq) entityOptions.loading = false;
            }
        }

        async function loadReviewQueues() {
            if (scope.tab !== 'review' || !canManage.value || !selectedLibrary.value) return;
            const context = await loadWriteContext();
            if (!context) return;
            const seq = ++reviewRequestSeq;
            const identity = {
                librarySlug: selectedLibrary.value.slug,
                libraryId: String(context.library_id),
                librarySlugs: [selectedLibrary.value.slug],
                limit: 50,
                offset: 0,
                scopeKey: scopeIdentity(),
            };
            reviewQueues.loading = true;
            reviewQueues.error = null;
            try {
                const [actions, relations] = await Promise.all([
                    api.listGraphGovernanceActions(identity.librarySlug, {
                        statuses: ['pending_review', 'approved'],
                        limit: identity.limit,
                        offset: identity.offset,
                    }),
                    api.searchGraphRelations(scope.organizationId, {
                        library_slugs: [identity.librarySlug],
                        ontology_version_ids: [],
                        type_keys: [],
                        statuses: ['pending_review'],
                        review_statuses: ['pending_review'],
                        source_types: [],
                        publication_state: 'all',
                        limit: 100,
                    }),
                ]);
                if (seq !== reviewRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphActionPageMatches(actions, identity)
                    || !graphRelationPageMatches(relations, identity)) {
                    reviewQueues.error = MALFORMED_ERROR;
                    reviewQueues.actions = [];
                    reviewQueues.relations = [];
                    return;
                }
                reviewQueues.actions = actions.items.filter((item) => item.status === 'pending_review');
                reviewQueues.approvedActions = actions.items.filter((item) => item.status === 'approved');
                reviewQueues.actionTotal = reviewQueues.actions.length;
                const reviewedRelationIds = new Set(reviewQueues.approvedActions
                    .filter((item) => item.action_kind === 'relation_review')
                    .map((item) => String(item.target_relation_id || '')));
                reviewQueues.relations = relations.items.filter(
                    (item) => !reviewedRelationIds.has(String(item.id)),
                );
            } catch (error) {
                if (seq !== reviewRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                reviewQueues.error = graphErrorProjection(error);
                reviewQueues.actions = [];
                reviewQueues.approvedActions = [];
                reviewQueues.relations = [];
            } finally {
                if (seq === reviewRequestSeq) reviewQueues.loading = false;
            }
        }

        async function loadPublicationWorkspace({ preserveSelection = false } = {}) {
            if (scope.tab !== 'publications' || !canManage.value || !selectedLibrary.value) return;
            const context = await loadWriteContext();
            if (!context) return;
            const ontologyIds = new Set(context.ontology_versions.map((item) => String(item.id)));
            if (!ontologyIds.has(publications.ontologyId)) {
                publications.ontologyId = String(context.ontology_versions[0]?.id || '');
            }
            if (!publications.ontologyId) {
                publications.enabled = false;
                publications.actions = [];
                publications.history = [];
                publications.current = null;
                return;
            }
            const seq = ++publicationRequestSeq;
            const identity = {
                librarySlug: selectedLibrary.value.slug,
                libraryId: String(context.library_id),
                ontologyVersionId: publications.ontologyId,
                limit: 100,
                offset: 0,
                page: 1,
                pageSize: 50,
                scopeKey: scopeIdentity(),
            };
            publications.loading = true;
            publications.error = null;
            try {
                const [actions, history, current] = await Promise.all([
                    api.listGraphGovernanceActions(identity.librarySlug, {
                        statuses: ['approved'],
                        limit: identity.limit,
                        offset: identity.offset,
                    }),
                    api.listGraphPublications(identity.librarySlug, {
                        ontology_version_id: identity.ontologyVersionId,
                        page: identity.page,
                        page_size: identity.pageSize,
                    }),
                    api.getActiveGraphPublication(
                        identity.librarySlug,
                        identity.ontologyVersionId,
                    ).then(
                        (data) => ({ data, error: null }),
                        (error) => ({ data: null, error }),
                    ),
                ]);
                if (seq !== publicationRequestSeq || identity.scopeKey !== scopeIdentity()
                    || publications.ontologyId !== identity.ontologyVersionId) return;
                if (!graphActionPageMatches(actions, identity)
                    || !graphPublicationListMatches(history, identity)
                    || (current.error && current.error.status !== 404)
                    || (current.data && (
                        !graphPublicationReadMatches(current.data, identity)
                        || !['active', 'degraded'].includes(current.data.status)
                        || current.data.publication_enabled !== history.publication_enabled
                    ))) {
                    publications.error = MALFORMED_ERROR;
                    publications.enabled = false;
                    publications.actions = [];
                    publications.history = [];
                    publications.current = null;
                    publications.preview = null;
                    return;
                }
                publications.enabled = history.publication_enabled;
                publications.actions = actions.items;
                publications.history = history.items;
                publications.current = current.data;
                if (preserveSelection) {
                    const selection = graphActionSelection(
                        publications.actions,
                        publications.selectedActionIds,
                    );
                    publications.selectedActionIds = selection?.ontologyVersionId === publications.ontologyId
                        ? selection.actionIds
                        : [];
                } else publications.selectedActionIds = [];
                publications.preview = null;
                publications.intentKey = '';
            } catch (error) {
                if (seq !== publicationRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                publications.error = graphErrorProjection(error);
                publications.enabled = false;
                publications.actions = [];
                publications.history = [];
                publications.current = null;
                publications.preview = null;
            } finally {
                if (seq === publicationRequestSeq) publications.loading = false;
            }
        }

        async function changePublicationOntology(ontologyId) {
            publicationRequestSeq += 1;
            publications.ontologyId = String(ontologyId || '');
            publications.selectedActionIds = [];
            publications.current = null;
            publications.preview = null;
            publications.intentKey = '';
            await loadPublicationWorkspace();
        }

        async function maybeOpenEntityFromRoute() {
            if (!scope.entityId || entityDetail.data?.entity?.id === scope.entityId) return;
            const row = (entityPage.data?.items || []).find((item) => item.id === scope.entityId);
            if (row) await loadEntityDetail(row, false);
            else if (scope.librarySlugs.length === 1) {
                await loadEntityDetail({
                    id: scope.entityId,
                    library: { slug: scope.librarySlugs[0] },
                }, false);
            }
        }

        async function maybeOpenRelationFromRoute() {
            if (!scope.relationId || relationDetail.data?.relation?.id === scope.relationId) return;
            const row = (relationPage.data?.items || []).find((item) => item.id === scope.relationId);
            if (row) await loadRelationDetail(row, false);
            else if (scope.librarySlugs.length === 1) {
                await loadRelationDetail({
                    id: scope.relationId,
                    library: { slug: scope.librarySlugs[0] },
                }, false);
            }
        }

        async function loadEntities() {
            if (!scope.organizationId || !scope.librarySlugs.length) return;
            const seq = ++entityRequestSeq;
            const identity = {
                organizationId: scope.organizationId,
                librarySlugs: [...scope.librarySlugs],
                scopeKey: scopeIdentity(),
            };
            entityPage.loading = true;
            entityPage.error = null;
            try {
                const data = await api.searchGraphEntities(
                    identity.organizationId,
                    searchBody(entityCursor, false),
                );
                if (seq !== entityRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphEntityPageMatches(data, identity)) {
                    entityPage.data = null;
                    entityPage.error = MALFORMED_ERROR;
                    return;
                }
                entityPage.data = data;
                await maybeOpenEntityFromRoute();
            } catch (error) {
                if (seq !== entityRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                entityPage.data = null;
                entityPage.error = graphErrorProjection(error);
            } finally {
                if (seq === entityRequestSeq) entityPage.loading = false;
            }
        }

        async function loadRelations() {
            if (!scope.organizationId || !scope.librarySlugs.length) return;
            const seq = ++relationRequestSeq;
            const identity = {
                organizationId: scope.organizationId,
                librarySlugs: [...scope.librarySlugs],
                scopeKey: scopeIdentity(),
            };
            relationPage.loading = true;
            relationPage.error = null;
            try {
                const data = await api.searchGraphRelations(
                    identity.organizationId,
                    searchBody(relationCursor, true),
                );
                if (seq !== relationRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphRelationPageMatches(data, identity)) {
                    relationPage.data = null;
                    relationPage.error = MALFORMED_ERROR;
                    return;
                }
                relationPage.data = data;
                await maybeOpenRelationFromRoute();
            } catch (error) {
                if (seq !== relationRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                relationPage.data = null;
                relationPage.error = graphErrorProjection(error);
            } finally {
                if (seq === relationRequestSeq) relationPage.loading = false;
            }
        }

        async function loadCurrentPage() {
            if (scope.tab === 'entities') await loadEntities();
            else if (scope.tab === 'relations') await loadRelations();
            else if (scope.tab === 'review') await loadReviewQueues();
            else if (scope.tab === 'publications') await loadPublicationWorkspace();
        }

        async function refreshCurrentPage() {
            if (scope.tab === 'browse' || scope.tab === 'explore') {
                explorerRefreshKey.value += 1;
                return;
            }
            await loadCurrentPage();
        }

        async function applyRoute() {
            const seq = ++routeSeq.value;
            const next = resolveGraphScope(route.query, store.permissions, store.organizations);
            const previousIdentity = scopeIdentity();
            Object.assign(scope, next);
            const canonical = graphRouteQuery(scope);
            if (previousIdentity !== scopeIdentity()) {
                entityRequestSeq += 1;
                relationRequestSeq += 1;
                resetCursors();
                invalidateDetails();
            }
            if (!sameValues(normalizedQuery(route.query), normalizedQuery(canonical))) {
                await router.replace({
                    path: '/knowledge-governance/graph',
                    query: canonical,
                });
                return;
            }
            if (seq !== routeSeq.value) return;
            await loadCurrentPage();
        }

        const stopRouteWatch = watch(
            () => [route.fullPath, store.permissions, store.organizations],
            applyRoute,
            { immediate: true },
        );
        onBeforeUnmount(() => {
            stopRouteWatch();
            entityRequestSeq += 1;
            relationRequestSeq += 1;
            invalidateDetails();
        });

        function navigate(next) {
            router.push({
                path: '/knowledge-governance/graph',
                query: graphRouteQuery(next),
            });
        }

        function changeTab(tab) {
            const next = resolveGraphScope({
                tab,
                organization: scope.organizationId,
                libraries: scope.librarySlugs.join(','),
            }, store.permissions, store.organizations);
            navigate(next);
        }

        function changeOrganization(organizationId) {
            const next = resolveGraphScope({
                tab: scope.tab,
                organization: organizationId,
            }, store.permissions, store.organizations);
            navigate(next);
        }

        function changeLibraries(librarySlugs) {
            const next = resolveGraphScope({
                tab: scope.tab,
                organization: scope.organizationId,
                libraries: (librarySlugs || []).join(','),
            }, store.permissions, store.organizations);
            navigate(next);
        }

        function selectBrowserEntity(row) {
            const entityId = String(row?.id || '');
            if (!entityId || entityId === scope.entityId) return;
            navigate({ ...scope, entityId, relationId: '' });
        }

        async function editBrowserEntity(data) {
            if (!data?.entity) return;
            Object.assign(entityDetail, { open: false, loading: false, data, error: null });
            await openEntityCorrection();
        }

        async function applyFilters() {
            if (scope.tab === 'relations') {
                relationCursor.history = [];
                relationCursor.current = null;
                await loadRelations();
            } else {
                entityCursor.history = [];
                entityCursor.current = null;
                await loadEntities();
            }
        }

        async function clearFilters() {
            Object.assign(filters, {
                query: '',
                typeKey: '',
                status: '',
                sourceType: '',
                publicationState: 'all',
                reviewStatus: '',
            });
            await applyFilters();
        }

        async function nextPage() {
            const data = pageState.value.data;
            if (!data?.next_cursor) return;
            const cursor = scope.tab === 'relations' ? relationCursor : entityCursor;
            Object.assign(cursor, advanceGraphCursor(cursor, data.next_cursor));
            await loadCurrentPage();
        }

        async function previousPage() {
            const cursor = scope.tab === 'relations' ? relationCursor : entityCursor;
            if (!cursor.history.length) return;
            Object.assign(cursor, retreatGraphCursor(cursor));
            await loadCurrentPage();
        }

        async function loadEntityDetail(row, updateRoute = true) {
            const librarySlug = String(row?.library?.slug || '');
            if (!row?.id || !scope.librarySlugs.includes(librarySlug)) return;
            const seq = ++entityDetailRequestSeq;
            const identity = {
                organizationId: scope.organizationId,
                librarySlug,
                entityId: String(row.id),
                ontologyVersionId: row.ontology_version_id || '',
                scopeKey: scopeIdentity(),
            };
            entityDetail.open = true;
            entityDetail.loading = true;
            entityDetail.data = null;
            entityDetail.error = null;
            try {
                const data = await api.getGraphEntity(
                    identity.organizationId,
                    identity.librarySlug,
                    identity.entityId,
                );
                if (seq !== entityDetailRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphEntityDetailMatches(data, identity)) {
                    entityDetail.error = MALFORMED_ERROR;
                    return;
                }
                entityDetail.data = data;
                if (updateRoute && scope.tab === 'entities' && route.query.entity !== identity.entityId) {
                    navigate({ ...scope, entityId: identity.entityId, relationId: '' });
                }
            } catch (error) {
                if (seq !== entityDetailRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                entityDetail.error = graphErrorProjection(error);
            } finally {
                if (seq === entityDetailRequestSeq) entityDetail.loading = false;
            }
        }

        async function loadRelationDetail(row, updateRoute = true) {
            const librarySlug = String(row?.library?.slug || '');
            if (!row?.id || !scope.librarySlugs.includes(librarySlug)) return;
            const seq = ++relationDetailRequestSeq;
            const identity = {
                organizationId: scope.organizationId,
                librarySlug,
                relationId: String(row.id),
                ontologyVersionId: row.ontology_version_id || '',
                scopeKey: scopeIdentity(),
            };
            relationDetail.open = true;
            relationDetail.loading = true;
            relationDetail.data = null;
            relationDetail.error = null;
            try {
                const data = await api.getGraphRelation(
                    identity.organizationId,
                    identity.librarySlug,
                    identity.relationId,
                );
                if (seq !== relationDetailRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphRelationDetailMatches(data, identity)) {
                    relationDetail.error = MALFORMED_ERROR;
                    return;
                }
                relationDetail.data = data;
                if (updateRoute && scope.tab === 'relations' && route.query.relation !== identity.relationId) {
                    navigate({ ...scope, relationId: identity.relationId, entityId: '' });
                }
            } catch (error) {
                if (seq !== relationDetailRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                relationDetail.error = graphErrorProjection(error);
            } finally {
                if (seq === relationDetailRequestSeq) relationDetail.loading = false;
            }
        }

        function closeEntityDetail() {
            entityDetailRequestSeq += 1;
            entityDetail.data = null;
            entityDetail.error = null;
            if (scope.entityId) navigate({ ...scope, entityId: '' });
        }

        function closeRelationDetail() {
            relationDetailRequestSeq += 1;
            relationDetail.data = null;
            relationDetail.error = null;
            if (scope.relationId) navigate({ ...scope, relationId: '' });
        }

        async function openEvidence(locator, factKind, fact) {
            const library = fact?.library;
            if (!locator?.evidence_id || !library?.slug || !library?.id) return;
            const seq = ++evidenceRequestSeq;
            const identity = {
                evidenceId: String(locator.evidence_id),
                libraryId: String(library.id),
                documentId: String(locator.document_id || ''),
                revisionId: String(locator.document_revision_id || ''),
                chunkId: locator.chunk_id ? String(locator.chunk_id) : '',
                factId: String(fact.id || ''),
                factKind,
                scopeKey: scopeIdentity(),
            };
            evidence.open = true;
            evidence.loading = true;
            evidence.data = null;
            evidence.error = null;
            evidence.identity = identity;
            evidence.librarySlug = library.slug;
            try {
                const data = await api.getCatalogEvidence(library.slug, identity.evidenceId, true);
                if (seq !== evidenceRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphEvidenceMatches(data, identity)) {
                    evidence.error = MALFORMED_ERROR;
                    return;
                }
                evidence.data = data;
            } catch (error) {
                if (seq !== evidenceRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                evidence.error = graphErrorProjection(error);
            } finally {
                if (seq === evidenceRequestSeq) evidence.loading = false;
            }
        }

        function closeEvidence() {
            evidenceRequestSeq += 1;
            evidence.data = null;
            evidence.error = null;
            evidence.identity = null;
        }

        function openCatalogDocument(document, librarySlug) {
            if (!document?.document_id || !librarySlug) return;
            router.push({
                path: '/knowledge-assets/catalog',
                query: { library: librarySlug, document: String(document.document_id) },
            });
        }

        function openGraphImport() {
            if (!selectedLibrary.value || !canWrite.value) return;
            router.push({
                path: '/knowledge-assets/import',
                query: { library: selectedLibrary.value.slug, mode: 'add' },
            });
        }

        function openEvidenceDocument() {
            if (!evidence.data) return;
            openCatalogDocument({ document_id: evidence.data.document_id }, evidence.librarySlug);
        }

        function openRelatedRelation(row) {
            if (!row?.id || !row?.library?.slug) return;
            const next = resolveGraphScope({
                tab: 'relations',
                organization: scope.organizationId,
                libraries: row.library.slug,
                relation: row.id,
            }, store.permissions, store.organizations);
            navigate(next);
        }

        async function refreshGovernanceSurfaces() {
            await loadCurrentPage();
            if (scope.tab === 'browse') explorerRefreshKey.value += 1;
            if (scope.tab === 'review') return;
            if (entityDetail.open && entityDetail.data?.entity) {
                await loadEntityDetail(entityDetail.data.entity, false);
            }
            if (relationDetail.open && relationDetail.data?.relation) {
                await loadRelationDetail(relationDetail.data.relation, false);
            }
        }

        async function executeGovernanceCommand(kind, operation, successMessage) {
            if (mutation.loading || !writeContext.data) return null;
            const token = ++mutationRequestSeq;
            const identity = {
                libraryId: String(writeContext.data.library_id),
                scopeKey: scopeIdentity(),
            };
            mutation.loading = true;
            mutation.kind = kind;
            mutation.error = null;
            try {
                const result = await operation();
                if (token !== mutationRequestSeq || identity.scopeKey !== scopeIdentity()) return null;
                if (!result?.id || String(result.library_id || '') !== identity.libraryId) {
                    mutation.error = MALFORMED_ERROR;
                    return null;
                }
                factDialog.open = false;
                correctionDialog.open = false;
                aliasDialog.open = false;
                mergeDialog.open = false;
                ElMessage.success(successMessage);
                await refreshGovernanceSurfaces();
                return result;
            } catch (error) {
                if (token !== mutationRequestSeq || identity.scopeKey !== scopeIdentity()) return null;
                mutation.error = graphErrorProjection(error);
                if (mutation.error.kind === 'conflict') {
                    factDialog.open = false;
                    correctionDialog.open = false;
                    aliasDialog.open = false;
                    mergeDialog.open = false;
                    ElMessage.warning(mutation.error.message);
                    await refreshGovernanceSurfaces();
                } else ElMessage.error(mutation.error.message);
                return null;
            } finally {
                if (token === mutationRequestSeq) {
                    mutation.loading = false;
                    mutation.kind = '';
                }
            }
        }

        async function openFactDialog(kind) {
            if (!canWrite.value) return;
            const context = await loadWriteContext();
            if (!context?.ontology_versions?.length) return;
            const ontology = context.ontology_versions[0];
            Object.assign(factDialog, {
                open: true,
                kind,
                ontologyId: String(ontology.id),
                typeId: String((kind === 'entity'
                    ? ontology.entity_types[0]?.id
                    : ontology.relation_types[0]?.id) || ''),
                canonicalName: '',
                sourceEntityId: '',
                targetEntityId: '',
                idempotencyKey: createGraphIntentKey(`${kind}-create`),
            });
            mutation.error = null;
            if (kind === 'relation') await loadEntityOptions({ ontologyId: factDialog.ontologyId });
        }

        async function changeFactOntology(ontologyId) {
            factDialog.ontologyId = String(ontologyId || '');
            const ontology = (writeContext.data?.ontology_versions || []).find(
                (item) => String(item.id) === factDialog.ontologyId,
            );
            const types = factDialog.kind === 'entity'
                ? ontology?.entity_types || []
                : ontology?.relation_types || [];
            factDialog.typeId = String(types[0]?.id || '');
            factDialog.sourceEntityId = '';
            factDialog.targetEntityId = '';
            if (factDialog.kind === 'relation') {
                await loadEntityOptions({ ontologyId: factDialog.ontologyId });
            }
        }

        async function submitFact() {
            const slug = selectedLibrary.value?.slug || '';
            if (!slug || !factDialog.ontologyId || !factDialog.typeId) return;
            if (factDialog.kind === 'entity') {
                const canonicalName = factDialog.canonicalName.trim();
                if (!canonicalName) {
                    ElMessage.warning('请输入实体名称');
                    return;
                }
                await executeGovernanceCommand(
                    'entity-create',
                    () => api.submitGraphEntity(slug, {
                        ontology_version_id: factDialog.ontologyId,
                        entity_type_id: factDialog.typeId,
                        canonical_name: canonicalName,
                        properties: {},
                        idempotency_key: factDialog.idempotencyKey,
                    }),
                    '实体已提交审核',
                );
                return;
            }
            if (!factDialog.sourceEntityId || !factDialog.targetEntityId) {
                ElMessage.warning('请选择关系两端的实体');
                return;
            }
            await executeGovernanceCommand(
                'relation-create',
                () => api.submitGraphRelation(slug, {
                    ontology_version_id: factDialog.ontologyId,
                    relation_type_id: factDialog.typeId,
                    source_entity_id: factDialog.sourceEntityId,
                    target_entity_id: factDialog.targetEntityId,
                    properties: {},
                    idempotency_key: factDialog.idempotencyKey,
                }),
                '关系已提交审核',
            );
        }

        async function openEntityCorrection() {
            const entity = entityDetail.data?.entity;
            if (!canWrite.value || !entity?.governance_state_hash) return;
            if (!await loadWriteContext()) return;
            Object.assign(correctionDialog, {
                open: true,
                kind: 'entity',
                targetId: String(entity.id),
                expectedStateHash: entity.governance_state_hash,
                canonicalName: entity.canonical_name,
                sourceEntityId: '',
                targetEntityId: '',
                idempotencyKey: createGraphIntentKey('entity-correct'),
            });
        }

        async function openRelationCorrection() {
            const relation = relationDetail.data?.relation;
            if (!canWrite.value || !relation?.governance_state_hash) return;
            if (!await loadWriteContext()) return;
            Object.assign(correctionDialog, {
                open: true,
                kind: 'relation',
                targetId: String(relation.id),
                expectedStateHash: relation.governance_state_hash,
                canonicalName: '',
                sourceEntityId: String(relation.source.id),
                targetEntityId: String(relation.target.id),
                idempotencyKey: createGraphIntentKey('relation-correct'),
            });
            await loadEntityOptions({ ontologyId: String(relation.ontology_version_id) });
        }

        async function submitCorrection() {
            const slug = selectedLibrary.value?.slug || '';
            if (!slug || !correctionDialog.expectedStateHash) return;
            if (correctionDialog.kind === 'entity') {
                const canonicalName = correctionDialog.canonicalName.trim();
                if (!canonicalName) {
                    ElMessage.warning('请输入实体名称');
                    return;
                }
                await executeGovernanceCommand(
                    'entity-correct',
                    () => api.correctGraphEntity(slug, correctionDialog.targetId, {
                        expected_state_hash: correctionDialog.expectedStateHash,
                        canonical_name: canonicalName,
                        idempotency_key: correctionDialog.idempotencyKey,
                    }),
                    '实体修正已提交审核',
                );
                return;
            }
            if (!correctionDialog.sourceEntityId || !correctionDialog.targetEntityId) {
                ElMessage.warning('请选择关系两端的实体');
                return;
            }
            await executeGovernanceCommand(
                'relation-correct',
                () => api.correctGraphRelation(slug, correctionDialog.targetId, {
                    expected_state_hash: correctionDialog.expectedStateHash,
                    source_entity_id: correctionDialog.sourceEntityId,
                    target_entity_id: correctionDialog.targetEntityId,
                    idempotency_key: correctionDialog.idempotencyKey,
                }),
                '关系修正已提交审核',
            );
        }

        async function openAliasDialog() {
            const entity = entityDetail.data?.entity;
            if (!canWrite.value || !entity?.governance_state_hash) return;
            if (!await loadWriteContext()) return;
            Object.assign(aliasDialog, {
                open: true,
                entityId: String(entity.id),
                expectedStateHash: entity.governance_state_hash,
                alias: '',
                idempotencyKey: createGraphIntentKey('alias-add'),
            });
        }

        async function submitAlias() {
            const slug = selectedLibrary.value?.slug || '';
            const value = aliasDialog.alias.trim();
            if (!slug || !value) {
                ElMessage.warning('请输入别名');
                return;
            }
            await executeGovernanceCommand(
                'alias-add',
                () => api.addGraphEntityAlias(slug, aliasDialog.entityId, {
                    expected_entity_state_hash: aliasDialog.expectedStateHash,
                    alias: value,
                    idempotency_key: aliasDialog.idempotencyKey,
                }),
                '别名已提交审核',
            );
        }

        async function confirmHighImpact(title, message) {
            try {
                await ElMessageBox.confirm(message, title, {
                    confirmButtonText: '确认',
                    cancelButtonText: '取消',
                    type: 'warning',
                });
                return true;
            } catch (_) {
                return false;
            }
        }

        async function stageFactState(kind, operation) {
            if (!canManage.value || !await loadWriteContext()) return;
            const fact = kind === 'entity'
                ? entityDetail.data?.entity
                : relationDetail.data?.relation;
            if (!fact?.governance_state_hash) return;
            const verb = operation === 'disable' ? '停用' : '恢复';
            if (!await confirmHighImpact(`${verb}${kind === 'entity' ? '实体' : '关系'}`, `确认${verb}当前${kind === 'entity' ? '实体' : '关系'}？变更需要发布后才会生效。`)) return;
            const key = createGraphIntentKey(`${kind}-${operation}`);
            const body = {
                expected_state_hash: fact.governance_state_hash,
                reason_code: 'operator_request',
                idempotency_key: key,
            };
            const command = kind === 'entity'
                ? (operation === 'disable' ? api.disableGraphEntity : api.restoreGraphEntity)
                : (operation === 'disable' ? api.disableGraphRelation : api.restoreGraphRelation);
            await executeGovernanceCommand(
                `${kind}-${operation}`,
                () => command(selectedLibrary.value.slug, fact.id, body),
                `${verb}操作已进入待发布队列`,
            );
        }

        async function disableAlias(alias) {
            if (!canManage.value || alias?.status !== 'active' || !alias?.governance_state_hash
                || !await loadWriteContext()) return;
            if (!await confirmHighImpact('停用别名', '确认停用当前别名？变更需要发布后才会生效。')) return;
            await executeGovernanceCommand(
                'alias-disable',
                () => api.disableGraphAlias(selectedLibrary.value.slug, alias.id, {
                    expected_state_hash: alias.governance_state_hash,
                    reason_code: 'operator_request',
                    idempotency_key: createGraphIntentKey('alias-disable'),
                }),
                '别名停用已进入待发布队列',
            );
        }

        async function openMergeDialog() {
            const entity = entityDetail.data?.entity;
            if (!canManage.value || entity?.status !== 'active' || !entity?.governance_state_hash
                || !await loadWriteContext()) return;
            Object.assign(mergeDialog, {
                open: true,
                survivorId: String(entity.id),
                survivorName: entity.canonical_name,
                ontologyId: String(entity.ontology_version_id),
                typeKey: entity.entity_type.key,
                expectedSurvivorStateHash: entity.governance_state_hash,
                loserId: '',
                reasonCode: 'duplicate_fact',
                idempotencyKey: createGraphIntentKey('entity-merge'),
            });
            await loadEntityOptions({
                ontologyId: mergeDialog.ontologyId,
                typeKey: mergeDialog.typeKey,
            });
        }

        async function submitMerge() {
            const loser = entityOptions.items.find((item) => String(item.id) === mergeDialog.loserId);
            if (!loser?.governance_state_hash || loser.status !== 'active') {
                ElMessage.warning('请选择一个可合并的生效实体');
                return;
            }
            if (!await confirmHighImpact('合并实体', `确认将“${loser.canonical_name}”合并到“${mergeDialog.survivorName}”？`)) return;
            await executeGovernanceCommand(
                'entity-merge',
                () => api.mergeGraphEntities(selectedLibrary.value.slug, {
                    ontology_version_id: mergeDialog.ontologyId,
                    survivor_entity_id: mergeDialog.survivorId,
                    loser_entity_id: String(loser.id),
                    expected_survivor_state_hash: mergeDialog.expectedSurvivorStateHash,
                    expected_loser_state_hash: loser.governance_state_hash,
                    reason_code: mergeDialog.reasonCode,
                    resolutions: [],
                    idempotency_key: mergeDialog.idempotencyKey,
                }),
                '实体合并已进入待发布队列',
            );
            if (mutation.error?.kind === 'conflict') {
                mutation.error = {
                    ...mutation.error,
                    message: '存在冲突关系，请先修正或停用冲突关系并完成发布，再重试合并。',
                };
            }
        }

        async function rerunExtraction(extraction) {
            if (!canManage.value || !extraction?.job_id || !await loadWriteContext()) return;
            if (!await confirmHighImpact('重新抽取图谱', '确认基于当前文档版本重新执行图谱抽取？')) return;
            const token = ++mutationRequestSeq;
            const identity = { scopeKey: scopeIdentity(), jobId: String(extraction.job_id) };
            mutation.loading = true;
            mutation.kind = 'extraction-rerun';
            mutation.error = null;
            try {
                const result = await api.rerunGraphExtraction(selectedLibrary.value.slug, identity.jobId, {
                    client_idempotency_key: createGraphIntentKey('graph-rerun'),
                });
                if (token !== mutationRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!result?.id || String(result.rerun_of_job_id || '') !== identity.jobId) {
                    mutation.error = MALFORMED_ERROR;
                    return;
                }
                ElMessage.success('图谱重新抽取任务已创建');
            } catch (error) {
                if (token !== mutationRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                mutation.error = graphErrorProjection(error);
                ElMessage.error(mutation.error.message);
            } finally {
                if (token === mutationRequestSeq) {
                    mutation.loading = false;
                    mutation.kind = '';
                }
            }
        }

        async function decideReviewAction(action, decision) {
            if (!canManage.value || action?.status !== 'pending_review' || !await loadWriteContext()) return;
            await executeGovernanceCommand(
                `action-${decision}`,
                () => api.decideGraphGovernanceAction(selectedLibrary.value.slug, action.id, {
                    expected_status: 'pending_review',
                    decision,
                    reason_code: decision === 'approve' ? 'review_approved' : 'review_rejected',
                }),
                decision === 'approve' ? '治理操作已通过' : '治理操作已驳回',
            );
        }

        async function cancelReviewAction(action) {
            if (!canManage.value || !['pending_review', 'approved'].includes(action?.status)
                || !await loadWriteContext()) return;
            if (!await confirmHighImpact('取消治理操作', '确认取消当前治理操作？')) return;
            await executeGovernanceCommand(
                'action-cancel',
                () => api.cancelGraphGovernanceAction(selectedLibrary.value.slug, action.id, {
                    expected_status: action.status,
                    reason_code: 'operator_cancelled',
                }),
                '治理操作已取消',
            );
        }

        async function decideReviewRelation(relation, decision) {
            if (!canManage.value || relation?.review_status !== 'pending_review'
                || !relation?.governance_state_hash || !await loadWriteContext()) return;
            await executeGovernanceCommand(
                `relation-${decision}`,
                () => api.reviewGraphRelation(selectedLibrary.value.slug, relation.id, {
                    expected_state_hash: relation.governance_state_hash,
                    decision,
                    reason_code: decision === 'approve' ? 'review_approved' : 'review_rejected',
                    idempotency_key: createGraphIntentKey(`relation-${decision}`),
                }),
                decision === 'approve' ? '关系审核已通过' : '关系审核已驳回',
            );
        }

        function clearPublicationPreview() {
            publications.preview = null;
            publications.intentKey = '';
        }

        function changePublicationActions() {
            clearPublicationPreview();
            publications.error = null;
        }

        function canOperatePlannedPublication(publication) {
            return Boolean(
                publications.enabled
                && publication?.status === 'planned'
                && (publication.source_mode === 'rollback'
                    || governancePlannedPublicationIds.value.has(String(publication.id))),
            );
        }

        async function previewPublication() {
            if (!canManage.value || !publications.enabled || publications.mutationKind
                || !await loadWriteContext()) return;
            const selection = graphActionSelection(
                publications.actions,
                publications.selectedActionIds,
            );
            if (!selection || selection.ontologyVersionId !== publications.ontologyId) {
                ElMessage.warning('请选择同一 Ontology 下的已批准操作');
                return;
            }
            const token = ++publicationMutationSeq;
            const parentPublicationId = activePublication.value?.id || null;
            const intentKey = createGraphIntentKey('governance-publication');
            const identity = {
                scopeKey: scopeIdentity(),
                ontologyVersionId: publications.ontologyId,
                actionIds: [...selection.actionIds],
                parentPublicationId,
                intentKey,
            };
            publications.mutationKind = 'preview';
            publications.error = null;
            clearPublicationPreview();
            try {
                const result = await api.planGraphGovernancePublication(selectedLibrary.value.slug, {
                    ontology_version_id: identity.ontologyVersionId,
                    action_ids: identity.actionIds,
                    expected_parent_publication_id: identity.parentPublicationId,
                    dry_run: true,
                    idempotency_key: identity.intentKey,
                });
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()
                    || publications.ontologyId !== identity.ontologyVersionId) return;
                if (!graphPublicationPreviewMatches(result, identity)) {
                    publications.error = MALFORMED_ERROR;
                    return;
                }
                publications.intentKey = identity.intentKey;
                publications.preview = {
                    ...result,
                    ontologyVersionId: identity.ontologyVersionId,
                    actionIds: identity.actionIds,
                    intentKey: identity.intentKey,
                };
            } catch (error) {
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                const projectedError = graphErrorProjection(error);
                clearPublicationPreview();
                if (projectedError.kind === 'conflict') {
                    await loadPublicationWorkspace({ preserveSelection: true });
                }
                if (token === publicationMutationSeq && identity.scopeKey === scopeIdentity()) {
                    publications.error = projectedError;
                }
            } finally {
                if (token === publicationMutationSeq) publications.mutationKind = '';
            }
        }

        async function commitPublicationPlan() {
            const preview = publications.preview;
            if (!preview || !publications.enabled || publications.mutationKind || !canManage.value) return;
            const selection = graphActionSelection(
                publications.actions,
                publications.selectedActionIds,
            );
            const currentParentId = activePublication.value?.id || null;
            if (!selection
                || selection.ontologyVersionId !== preview.ontologyVersionId
                || !sameValues(selection.actionIds, preview.actionIds)
                || String(currentParentId || '') !== String(preview.parent_publication_id || '')) {
                clearPublicationPreview();
                publications.error = { ...MALFORMED_ERROR, message: '发布预览已过期，请重新预览。' };
                return;
            }
            const token = ++publicationMutationSeq;
            const identity = { scopeKey: scopeIdentity(), ontologyVersionId: preview.ontologyVersionId };
            publications.mutationKind = 'commit-confirm';
            publications.error = null;
            try {
                if (!await confirmHighImpact('生成发布版本', '确认按当前预览生成待发布版本？')) return;
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                publications.mutationKind = 'commit';
                const result = await api.planGraphGovernancePublication(selectedLibrary.value.slug, {
                    ontology_version_id: preview.ontologyVersionId,
                    action_ids: [...preview.actionIds],
                    expected_parent_publication_id: preview.parent_publication_id,
                    dry_run: false,
                    idempotency_key: preview.intentKey,
                });
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphPublicationCommitMatches(result, preview)) {
                    publications.error = MALFORMED_ERROR;
                    clearPublicationPreview();
                    return;
                }
                ElMessage.success('待发布版本已生成');
                clearPublicationPreview();
                await loadPublicationWorkspace();
            } catch (error) {
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                const projectedError = graphErrorProjection(error);
                clearPublicationPreview();
                if (projectedError.kind === 'conflict') await loadPublicationWorkspace();
                if (token === publicationMutationSeq && identity.scopeKey === scopeIdentity()) {
                    publications.error = projectedError;
                }
            } finally {
                if (token === publicationMutationSeq) publications.mutationKind = '';
            }
        }

        async function activatePublication(publication) {
            if (!canManage.value || publications.mutationKind
                || !canOperatePlannedPublication(publication) || !publication?.manifest_hash) return;
            const token = ++publicationMutationSeq;
            const identity = {
                scopeKey: scopeIdentity(),
                libraryId: String(writeContext.data?.library_id || ''),
                ontologyVersionId: publications.ontologyId,
                publicationId: String(publication.id),
                manifestHash: publication.manifest_hash,
            };
            publications.mutationKind = 'activate-confirm';
            publications.error = null;
            try {
                if (!await confirmHighImpact('激活发布版本', '确认激活当前待发布版本？图谱正式结果将随之更新。')) return;
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                publications.mutationKind = 'activate';
                const result = await api.activateGraphPublication(
                    selectedLibrary.value.slug,
                    identity.publicationId,
                    {
                        idempotency_key: createGraphIntentKey('publication-activate'),
                        expected_manifest_hash: identity.manifestHash,
                    },
                );
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphPublicationReadMatches(result, { ...identity, status: 'active' })) {
                    publications.error = MALFORMED_ERROR;
                    return;
                }
                ElMessage.success('图谱发布版本已激活');
                await loadPublicationWorkspace();
            } catch (error) {
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                const projectedError = graphErrorProjection(error);
                await loadPublicationWorkspace();
                if (token === publicationMutationSeq && identity.scopeKey === scopeIdentity()) {
                    publications.error = projectedError;
                }
            } finally {
                if (token === publicationMutationSeq) publications.mutationKind = '';
            }
        }

        async function cancelPublication(publication) {
            if (!canManage.value || publications.mutationKind
                || !canOperatePlannedPublication(publication)) return;
            const token = ++publicationMutationSeq;
            const identity = {
                scopeKey: scopeIdentity(),
                libraryId: String(writeContext.data?.library_id || ''),
                ontologyVersionId: publications.ontologyId,
                publicationId: String(publication.id),
            };
            publications.mutationKind = 'cancel-confirm';
            publications.error = null;
            try {
                if (!await confirmHighImpact('取消发布版本', '确认取消当前待发布版本？已绑定的治理操作将重新可选。')) return;
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                publications.mutationKind = 'cancel';
                const result = await api.cancelGraphPublication(
                    selectedLibrary.value.slug,
                    identity.publicationId,
                    {
                        idempotency_key: createGraphIntentKey('publication-cancel'),
                        reason_code: 'operator_cancelled',
                    },
                );
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphPublicationReadMatches(result, { ...identity, status: 'cancelled' })) {
                    publications.error = MALFORMED_ERROR;
                    return;
                }
                ElMessage.success('待发布版本已取消');
                await loadPublicationWorkspace();
            } catch (error) {
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                const projectedError = graphErrorProjection(error);
                await loadPublicationWorkspace();
                if (token === publicationMutationSeq && identity.scopeKey === scopeIdentity()) {
                    publications.error = projectedError;
                }
            } finally {
                if (token === publicationMutationSeq) publications.mutationKind = '';
            }
        }

        async function planPublicationRollback(publication) {
            if (!canManage.value || !publications.enabled || publications.mutationKind
                || publication?.status !== 'superseded') return;
            const token = ++publicationMutationSeq;
            const identity = {
                scopeKey: scopeIdentity(),
                libraryId: String(writeContext.data?.library_id || ''),
                ontologyVersionId: publications.ontologyId,
                targetPublicationId: String(publication.id),
                intentKey: createGraphIntentKey('publication-rollback'),
            };
            publications.mutationKind = 'rollback-preview';
            publications.error = null;
            try {
                const preview = await api.rollbackGraphPublication(
                    selectedLibrary.value.slug,
                    identity.targetPublicationId,
                    { idempotency_key: identity.intentKey, dry_run: true },
                );
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphRollbackPreviewMatches(preview, { ...identity, dryRun: true })) {
                    publications.error = MALFORMED_ERROR;
                    return;
                }
                if (!await confirmHighImpact('生成回滚版本', `确认基于版本 ${shortCatalogId(identity.targetPublicationId)} 生成待发布回滚版本？`)) return;
                publications.mutationKind = 'rollback-commit';
                const result = await api.rollbackGraphPublication(
                    selectedLibrary.value.slug,
                    identity.targetPublicationId,
                    { idempotency_key: identity.intentKey, dry_run: false },
                );
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphRollbackPreviewMatches(result, { ...identity, dryRun: false })
                    || result.manifest_hash !== preview.manifest_hash) {
                    publications.error = MALFORMED_ERROR;
                    return;
                }
                ElMessage.success('待发布回滚版本已生成');
                await loadPublicationWorkspace();
            } catch (error) {
                if (token !== publicationMutationSeq || identity.scopeKey !== scopeIdentity()) return;
                const projectedError = graphErrorProjection(error);
                await loadPublicationWorkspace();
                if (token === publicationMutationSeq && identity.scopeKey === scopeIdentity()) {
                    publications.error = projectedError;
                }
            } finally {
                if (token === publicationMutationSeq) publications.mutationKind = '';
            }
        }

        return {
            scope,
            filters,
            organizations,
            showOrganizationSelector,
            availableLibraries,
            hasManagement,
            selectedLibrary,
            canWrite,
            canManage,
            entityCursor,
            relationCursor,
            currentCursor,
            entityPage,
            relationPage,
            pageState,
            workspaceLoading,
            entityDetail,
            relationDetail,
            evidence,
            evidenceParts,
            writeContext,
            entityOptions,
            factDialog,
            factOntology,
            factTypeOptions,
            correctionDialog,
            aliasDialog,
            mergeDialog,
            reviewQueues,
            mutation,
            publications,
            explorerRefreshKey,
            publicationActions,
            activePublication,
            changeTab,
            changeOrganization,
            changeLibraries,
            selectBrowserEntity,
            editBrowserEntity,
            applyFilters,
            clearFilters,
            nextPage,
            previousPage,
            loadCurrentPage,
            refreshCurrentPage,
            loadEntityDetail,
            loadRelationDetail,
            closeEntityDetail,
            closeRelationDetail,
            openEvidence,
            closeEvidence,
            openCatalogDocument,
            openGraphImport,
            openEvidenceDocument,
            openRelatedRelation,
            loadWriteContext,
            loadReviewQueues,
            loadPublicationWorkspace,
            changePublicationOntology,
            openFactDialog,
            changeFactOntology,
            submitFact,
            openEntityCorrection,
            openRelationCorrection,
            submitCorrection,
            openAliasDialog,
            submitAlias,
            stageFactState,
            disableAlias,
            openMergeDialog,
            submitMerge,
            rerunExtraction,
            decideReviewAction,
            cancelReviewAction,
            decideReviewRelation,
            previewPublication,
            commitPublicationPlan,
            activatePublication,
            cancelPublication,
            planPublicationRollback,
            clearPublicationPreview,
            changePublicationActions,
            canOperatePlannedPublication,
            graphActionSummary,
            sourceTypeLabel,
            publicationSourceLabel,
            statusTag,
            graphFactLabel,
            graphReviewLabel,
            graphPublicationLabel,
            graphPropertyRows,
            formatCatalogConfidence,
            formatCatalogTime,
            shortCatalogId,
            catalogPageLabel,
            catalogTitlePath,
        };
    },
    template: `
    <div class="graph-workspace">
      <header class="graph-header">
        <div class="graph-create-actions">
          <el-button v-if="canWrite && ['browse', 'entities'].includes(scope.tab)" type="primary"
                     :disabled="mutation.loading" @click="openFactDialog('entity')">
            <local-icon icon="mdi:plus"></local-icon>新增实体
          </el-button>
          <el-button v-if="canWrite && ['browse', 'relations'].includes(scope.tab)" type="primary"
                     :disabled="mutation.loading" @click="openFactDialog('relation')">
            <local-icon icon="mdi:plus"></local-icon>新增关系
          </el-button>
        </div>
        <div class="graph-heading">
          <div class="graph-heading-icon"><local-icon icon="carbon:chart-relationship"></local-icon></div>
          <div><h2>知识图谱</h2><p>实体、关系与原文证据</p></div>
        </div>
        <el-button :loading="workspaceLoading" title="刷新当前页面" @click="refreshCurrentPage">
          <local-icon icon="status:retry"></local-icon><span>刷新</span>
        </el-button>
      </header>

      <section class="graph-scope-band" :class="{ 'is-single-organization': !showOrganizationSelector }">
        <label v-if="showOrganizationSelector" class="graph-field">
          <span>组织</span>
          <el-select :model-value="scope.organizationId" class="graph-organization-select"
                     @change="changeOrganization">
            <el-option v-for="item in organizations" :key="item.id"
                       :label="item.name" :value="item.id" />
          </el-select>
        </label>
        <label class="graph-field graph-library-field">
          <span>知识库范围</span>
          <el-select :model-value="scope.librarySlugs" multiple :multiple-limit="20"
                     collapse-tags collapse-tags-tooltip @change="changeLibraries">
            <el-option v-for="item in availableLibraries" :key="item.slug"
                       :label="item.name" :value="item.slug">
              <span>{{ item.name }}</span><small>{{ item.slug }}</small>
            </el-option>
          </el-select>
        </label>
        <div class="graph-scope-count">
          <strong>{{ scope.librarySlugs.length }}</strong><span>个知识库</span>
        </div>
      </section>

      <el-tabs :model-value="scope.tab" class="graph-tabs" @tab-change="changeTab">
        <el-tab-pane label="图谱" name="browse" />
        <el-tab-pane label="发布记录" name="publications" :disabled="!hasManagement" />
        <el-tab-pane label="高级探查" name="explore" />
      </el-tabs>

      <el-alert v-if="mutation.error && !mergeDialog.open" class="graph-command-error"
                :title="mutation.error.message" type="warning" :closable="false" show-icon />

      <section v-if="scope.tab === 'browse'" class="graph-browser-workspace">
        <graph-knowledge-browser :organization-id="scope.organizationId"
                                 :library-slugs="scope.librarySlugs"
                                 :selected-entity-id="scope.entityId"
                                 :refresh-key="explorerRefreshKey"
                                 :can-write="canWrite"
                                 @select-entity="selectBrowserEntity"
                                 @edit-entity="editBrowserEntity"
                                 @open-relation="loadRelationDetail"
                                 @open-evidence="openEvidence"
                                 @open-document="openCatalogDocument"
                                 @open-import="openGraphImport" />
      </section>

      <template v-else-if="scope.tab === 'entities' || scope.tab === 'relations'">
        <section class="graph-filter-band">
          <el-input v-model="filters.query" clearable maxlength="160"
                    placeholder="搜索名称或关系" @keyup.enter="applyFilters">
            <template #prefix><local-icon icon="mdi:text-search"></local-icon></template>
          </el-input>
          <el-input v-model="filters.typeKey" clearable maxlength="128" placeholder="类型 Key" />
          <el-select v-model="filters.status" clearable placeholder="事实状态">
            <el-option label="草稿" value="draft" />
            <el-option label="待审核" value="pending_review" />
            <el-option label="生效" value="active" />
            <el-option label="已驳回" value="rejected" />
            <el-option label="已停用" value="disabled" />
          </el-select>
          <el-select v-if="scope.tab === 'relations'" v-model="filters.reviewStatus"
                     clearable placeholder="审核状态">
            <el-option label="待审核" value="pending_review" />
            <el-option label="已通过" value="approved" />
            <el-option label="已驳回" value="rejected" />
            <el-option label="无需审核" value="not_required" />
          </el-select>
          <el-select v-model="filters.publicationState" placeholder="发布状态">
            <el-option label="全部" value="all" />
            <el-option label="已发布" value="published" />
            <el-option label="待发布" value="staged" />
          </el-select>
          <el-select v-model="filters.sourceType" clearable placeholder="来源">
            <el-option label="人工" value="manual" />
            <el-option label="导入" value="imported" />
            <el-option label="模型抽取" value="extracted" />
          </el-select>
          <div class="graph-filter-actions">
            <el-button type="primary" @click="applyFilters">检索</el-button>
            <el-button @click="clearFilters">重置</el-button>
          </div>
        </section>

        <el-alert v-if="pageState.error?.conflicts?.length" class="graph-scope-conflicts"
                  title="所选知识库的图谱配置不兼容" type="warning" :closable="false" show-icon>
          <template #default>
            <div v-for="item in pageState.error.conflicts" :key="item.librarySlug">
              <b>{{ item.librarySlug }}</b><span>{{ item.reasons.join('、') }}</span>
            </div>
          </template>
        </el-alert>

        <section class="graph-directory">
          <div class="graph-directory-heading">
            <div><strong>{{ scope.tab === 'entities' ? '实体目录' : '关系目录' }}</strong>
              <span>第 {{ currentCursor.history.length + 1 }} 页</span></div>
            <span v-if="pageState.loading">正在加载...</span>
          </div>

          <div v-if="pageState.error && !pageState.error.conflicts.length" class="graph-state">
            <img src="./assets/illustrations/service-error.svg" alt="" />
            <strong>{{ pageState.error.message }}</strong>
            <el-button type="primary" plain @click="loadCurrentPage">重新加载</el-button>
          </div>
          <div v-else-if="!pageState.loading && !pageState.data?.items?.length" class="graph-state">
            <img src="./assets/illustrations/search-empty.svg" alt="" />
            <strong>没有找到匹配结果</strong>
          </div>

          <div v-else-if="scope.tab === 'entities'" class="graph-table-shell">
            <el-table :data="entityPage.data?.items || []" v-loading="entityPage.loading"
                      row-key="id" height="520">
              <el-table-column label="实体" min-width="230" fixed="left">
                <template #default="cell">
                  <button class="graph-name-button" @click="loadEntityDetail(cell.row)">
                    <strong>{{ cell.row.canonical_name }}</strong>
                    <small>{{ cell.row.normalized_name }}</small>
                  </button>
                </template>
              </el-table-column>
              <el-table-column label="类型" min-width="150">
                <template #default="cell"><span>{{ cell.row.entity_type.label }}</span><small class="graph-cell-sub">{{ cell.row.entity_type.key }}</small></template>
              </el-table-column>
              <el-table-column label="知识库" min-width="150">
                <template #default="cell"><span>{{ cell.row.library.name }}</span><small class="graph-cell-sub">{{ cell.row.library.slug }}</small></template>
              </el-table-column>
              <el-table-column label="状态" width="150">
                <template #default="cell"><div class="graph-tag-stack">
                  <el-tag :type="statusTag(cell.row.status)" size="small">{{ graphFactLabel(cell.row.status) }}</el-tag>
                  <el-tag :type="statusTag(cell.row.publication_state)" size="small" effect="plain">{{ graphPublicationLabel(cell.row.publication_state) }}</el-tag>
                </div></template>
              </el-table-column>
              <el-table-column label="证据 / 文档" width="120">
                <template #default="cell">{{ cell.row.counts.evidence }} / {{ cell.row.counts.documents }}</template>
              </el-table-column>
              <el-table-column label="可信度" width="100">
                <template #default="cell">{{ formatCatalogConfidence(cell.row.confidence) }}</template>
              </el-table-column>
              <el-table-column label="来源" width="100">
                <template #default="cell">{{ sourceTypeLabel(cell.row.source_type) }}</template>
              </el-table-column>
              <el-table-column label="更新时间" width="170">
                <template #default="cell">{{ formatCatalogTime(cell.row.updated_at) }}</template>
              </el-table-column>
            </el-table>
          </div>

          <div v-else class="graph-table-shell">
            <el-table :data="relationPage.data?.items || []" v-loading="relationPage.loading"
                      row-key="id" height="520">
              <el-table-column label="关系" min-width="360" fixed="left">
                <template #default="cell">
                  <button class="graph-relation-button" @click="loadRelationDetail(cell.row)">
                    <strong>{{ cell.row.source.canonical_name }}</strong>
                    <span><local-icon icon="carbon:chart-relationship"></local-icon>{{ cell.row.relation_type.label }}</span>
                    <strong>{{ cell.row.target.canonical_name }}</strong>
                  </button>
                </template>
              </el-table-column>
              <el-table-column label="知识库" min-width="150">
                <template #default="cell"><span>{{ cell.row.library.name }}</span><small class="graph-cell-sub">{{ cell.row.library.slug }}</small></template>
              </el-table-column>
              <el-table-column label="事实状态" width="110">
                <template #default="cell"><el-tag :type="statusTag(cell.row.status)" size="small">{{ graphFactLabel(cell.row.status) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="审核" width="110">
                <template #default="cell"><el-tag :type="statusTag(cell.row.review_status)" size="small" effect="plain">{{ graphReviewLabel(cell.row.review_status) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="发布" width="100">
                <template #default="cell">{{ graphPublicationLabel(cell.row.publication_state) }}</template>
              </el-table-column>
              <el-table-column label="证据 / 文档" width="120">
                <template #default="cell">{{ cell.row.counts.evidence }} / {{ cell.row.counts.documents }}</template>
              </el-table-column>
              <el-table-column label="可信度" width="100">
                <template #default="cell">{{ formatCatalogConfidence(cell.row.confidence) }}</template>
              </el-table-column>
              <el-table-column label="来源" width="100">
                <template #default="cell">{{ sourceTypeLabel(cell.row.source_type) }}</template>
              </el-table-column>
            </el-table>
          </div>

          <div v-if="pageState.data" class="graph-pagination">
            <span>每页最多 50 项</span>
            <div>
              <el-button :disabled="!currentCursor.history.length || pageState.loading"
                         @click="previousPage">上一页</el-button>
              <el-button :disabled="!pageState.data.next_cursor || pageState.loading"
                         @click="nextPage">下一页</el-button>
            </div>
          </div>
        </section>
      </template>

      <section v-else-if="scope.tab === 'explore'" class="graph-exploration-workspace">
        <graph-explorer :organization-id="scope.organizationId"
                        :library-slugs="scope.librarySlugs"
                        :refresh-key="explorerRefreshKey"
                        @open-entity="loadEntityDetail"
                        @open-relation="loadRelationDetail"
                        @open-evidence="openEvidence" />
      </section>

      <section v-else-if="scope.tab === 'review'" class="graph-review-workspace">
        <el-alert v-if="reviewQueues.error" :title="reviewQueues.error.message"
                  type="warning" :closable="false" show-icon>
          <template #default><el-button link type="primary" @click="loadReviewQueues">重新加载</el-button></template>
        </el-alert>
        <div v-if="reviewQueues.loading" class="graph-state"><strong>正在加载审核队列...</strong></div>
        <div v-else class="graph-review-grid">
          <section class="graph-review-column">
            <div class="graph-section-heading"><h4>治理操作</h4><span>{{ reviewQueues.actionTotal }} 项</span></div>
            <div v-if="reviewQueues.actions.length" class="graph-review-list">
              <article v-for="action in reviewQueues.actions" :key="action.id" class="graph-review-item">
                <div class="graph-review-item-head">
                  <strong>{{ graphActionSummary(action).label }}</strong>
                  <el-tag type="warning" size="small">待审核</el-tag>
                </div>
                <dl v-if="graphActionSummary(action).fields.length" class="graph-review-summary">
                  <div v-for="field in graphActionSummary(action).fields" :key="field.key">
                    <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
                  </div>
                </dl>
                <div class="graph-review-actions">
                  <el-button type="success" plain :disabled="mutation.loading"
                             @click="decideReviewAction(action, 'approve')">通过</el-button>
                  <el-button type="danger" plain :disabled="mutation.loading"
                             @click="decideReviewAction(action, 'reject')">驳回</el-button>
                  <el-button :disabled="mutation.loading" @click="cancelReviewAction(action)">取消</el-button>
                </div>
              </article>
            </div>
            <div v-else class="graph-inline-empty">暂无待审核治理操作</div>
          </section>
          <section class="graph-review-column">
            <div class="graph-section-heading"><h4>抽取关系</h4><span>{{ reviewQueues.relations.length }} 项</span></div>
            <div v-if="reviewQueues.relations.length" class="graph-review-list">
              <article v-for="relation in reviewQueues.relations" :key="relation.id" class="graph-review-item">
                <div class="graph-review-relation">
                  <strong>{{ relation.source.canonical_name }}</strong>
                  <span>{{ relation.relation_type.label }}</span>
                  <strong>{{ relation.target.canonical_name }}</strong>
                </div>
                <div class="graph-review-meta">
                  <span>{{ relation.library.name }}</span>
                  <span>可信度 {{ formatCatalogConfidence(relation.confidence) }}</span>
                  <span>{{ relation.counts.evidence }} 条证据</span>
                </div>
                <div class="graph-review-actions">
                  <el-button type="success" plain :disabled="mutation.loading"
                             @click="decideReviewRelation(relation, 'approve')">通过</el-button>
                  <el-button type="danger" plain :disabled="mutation.loading"
                             @click="decideReviewRelation(relation, 'reject')">驳回</el-button>
                </div>
              </article>
            </div>
            <div v-else class="graph-inline-empty">暂无待审核关系</div>
          </section>
        </div>
      </section>

      <section v-else-if="scope.tab === 'publications'" class="graph-publication-workspace">
        <div class="graph-publication-toolbar">
          <label class="graph-field">
            <span>Ontology 版本</span>
            <el-select :model-value="publications.ontologyId" :disabled="publications.loading || !!publications.mutationKind"
                       @change="changePublicationOntology">
              <el-option v-for="item in writeContext.data?.ontology_versions || []" :key="item.id"
                         :label="item.version_key + ' v' + item.version_no" :value="String(item.id)" />
            </el-select>
          </label>
          <div class="graph-publication-current">
            <span>当前版本</span>
            <strong v-if="activePublication" :title="activePublication.id">{{ shortCatalogId(activePublication.id) }}</strong>
            <strong v-else>尚未发布</strong>
            <el-tag v-if="activePublication" :type="statusTag(activePublication.status)" size="small" effect="plain">
              {{ graphPublicationLabel(activePublication.status) }}
            </el-tag>
          </div>
          <el-button :loading="publications.loading" :disabled="!!publications.mutationKind"
                     @click="loadPublicationWorkspace">刷新发布状态</el-button>
        </div>

        <el-alert v-if="!publications.loading && !publications.enabled" class="graph-publication-alert"
                  title="图谱发布功能当前未启用，只能查看已有记录。"
                  type="info" :closable="false" show-icon />
        <el-alert v-if="publications.error" class="graph-publication-alert"
                  :title="publications.error.message" type="warning" :closable="false" show-icon />

        <div v-if="publications.loading" class="graph-state"><strong>正在加载发布工作区...</strong></div>
        <div v-else class="graph-publication-grid">
          <section class="graph-publication-column graph-publication-planner">
            <div class="graph-section-heading">
              <h4>已批准治理操作</h4><span>{{ publicationActions.length }} 项可发布</span>
            </div>
            <el-checkbox-group v-if="publicationActions.length" v-model="publications.selectedActionIds"
                               class="graph-publication-action-list" :disabled="!publications.enabled || !!publications.mutationKind"
                               @change="changePublicationActions">
              <el-checkbox v-for="action in publicationActions" :key="action.id" :label="String(action.id)"
                           class="graph-publication-action">
                <span class="graph-publication-action-content">
                  <strong>{{ graphActionSummary(action).label }}</strong>
                  <small v-if="graphActionSummary(action).fields.length">
                    {{ graphActionSummary(action).fields.map((field) => field.label + '：' + field.value).join(' · ') }}
                  </small>
                  <small v-else :title="action.id">操作 {{ shortCatalogId(action.id) }}</small>
                </span>
              </el-checkbox>
            </el-checkbox-group>
            <div v-else class="graph-inline-empty">当前 Ontology 暂无可发布操作</div>

            <div class="graph-publication-plan-actions">
              <el-button type="primary" plain
                         :disabled="!publications.enabled || !publications.selectedActionIds.length || !!publications.mutationKind"
                         :loading="publications.mutationKind === 'preview'" @click="previewPublication">
                生成预览
              </el-button>
              <el-button v-if="publications.preview" :disabled="!!publications.mutationKind"
                         @click="clearPublicationPreview">清除预览</el-button>
            </div>

            <section v-if="publications.preview" class="graph-publication-preview">
              <div class="graph-section-heading"><h4>发布预览</h4><el-tag type="success" size="small" effect="plain">校验完成</el-tag></div>
              <dl class="graph-publication-metrics">
                <div><dt>实体</dt><dd>{{ publications.preview.entity_count }}</dd></div>
                <div><dt>关系</dt><dd>{{ publications.preview.relation_count }}</dd></div>
                <div><dt>治理操作</dt><dd>{{ publications.preview.actionIds.length }}</dd></div>
              </dl>
              <dl class="graph-publication-identity">
                <div><dt>父版本</dt><dd :title="publications.preview.parent_publication_id || ''">{{ publications.preview.parent_publication_id ? shortCatalogId(publications.preview.parent_publication_id) : '首次发布' }}</dd></div>
                <div><dt>Manifest</dt><dd :title="publications.preview.manifest_hash">{{ shortCatalogId(publications.preview.manifest_hash) }}</dd></div>
                <div><dt>操作集合</dt><dd :title="publications.preview.action_set_hash">{{ shortCatalogId(publications.preview.action_set_hash) }}</dd></div>
              </dl>
              <el-button type="primary" :loading="publications.mutationKind === 'commit' || publications.mutationKind === 'commit-confirm'"
                         :disabled="!!publications.mutationKind" @click="commitPublicationPlan">
                生成待发布版本
              </el-button>
            </section>
          </section>

          <section class="graph-publication-column graph-publication-history">
            <div class="graph-section-heading"><h4>发布记录</h4><span>最近 {{ publications.history.length }} 项</span></div>
            <div v-if="publications.history.length" class="graph-publication-list">
              <article v-for="publication in publications.history" :key="publication.id" class="graph-publication-item">
                <div class="graph-publication-item-head">
                  <div>
                    <strong :title="publication.id">{{ shortCatalogId(publication.id) }}</strong>
                    <span>{{ publicationSourceLabel(publication.source_mode) }}</span>
                  </div>
                  <div class="graph-tag-stack">
                    <el-tag v-if="activePublication?.id === publication.id" type="success" size="small">当前</el-tag>
                    <el-tag :type="statusTag(publication.status)" size="small" effect="plain">{{ graphPublicationLabel(publication.status) }}</el-tag>
                  </div>
                </div>
                <dl class="graph-publication-metrics graph-publication-item-metrics">
                  <div><dt>实体</dt><dd>{{ publication.entity_count }}</dd></div>
                  <div><dt>关系</dt><dd>{{ publication.relation_count }}</dd></div>
                  <div><dt>计划时间</dt><dd>{{ formatCatalogTime(publication.planned_at) }}</dd></div>
                </dl>
                <div class="graph-publication-manifest">
                  <span>Manifest</span><code :title="publication.manifest_hash">{{ shortCatalogId(publication.manifest_hash) }}</code>
                </div>
                <div v-if="canOperatePlannedPublication(publication) || publication.status === 'superseded'"
                     class="graph-publication-item-actions">
                  <el-button v-if="canOperatePlannedPublication(publication)" type="success" plain
                             :disabled="!!publications.mutationKind" @click="activatePublication(publication)">激活</el-button>
                  <el-button v-if="canOperatePlannedPublication(publication)" type="danger" plain
                             :disabled="!!publications.mutationKind" @click="cancelPublication(publication)">取消</el-button>
                  <el-button v-if="publications.enabled && publication.status === 'superseded'" plain
                             :disabled="!!publications.mutationKind" @click="planPublicationRollback(publication)">回滚到此版本</el-button>
                </div>
              </article>
            </div>
            <div v-else class="graph-inline-empty">暂无发布记录</div>
          </section>
        </div>
      </section>

      <section v-else class="graph-state graph-tab-state">
        <img src="./assets/illustrations/data-empty.svg" alt="" />
        <strong>暂无记录</strong>
      </section>

      <el-drawer v-model="entityDetail.open" class="graph-detail-drawer" size="720px"
                 title="实体详情" @closed="closeEntityDetail">
        <div v-if="entityDetail.loading" class="graph-detail-loading">正在加载实体...</div>
        <div v-else-if="entityDetail.error" class="graph-state"><strong>{{ entityDetail.error.message }}</strong></div>
        <template v-else-if="entityDetail.data">
          <section class="graph-detail-identity">
            <div class="graph-detail-actions">
              <el-button v-if="canWrite" @click="openEntityCorrection">修正</el-button>
              <el-button v-if="canWrite" @click="openAliasDialog">新增别名</el-button>
              <el-button v-if="canManage && entityDetail.data.entity.status === 'active'"
                         type="warning" plain @click="stageFactState('entity', 'disable')">停用</el-button>
              <el-button v-if="canManage && entityDetail.data.entity.status === 'disabled'"
                         type="success" plain @click="stageFactState('entity', 'restore')">恢复</el-button>
              <el-button v-if="canManage && entityDetail.data.entity.status === 'active'"
                         @click="openMergeDialog">合并</el-button>
            </div>
            <div class="graph-detail-title"><div><h3>{{ entityDetail.data.entity.canonical_name }}</h3><span>{{ entityDetail.data.entity.entity_type.label }}</span></div>
              <div class="graph-tag-stack"><el-tag :type="statusTag(entityDetail.data.entity.status)">{{ graphFactLabel(entityDetail.data.entity.status) }}</el-tag><el-tag effect="plain">{{ graphPublicationLabel(entityDetail.data.entity.publication_state) }}</el-tag></div>
            </div>
            <dl class="graph-identity-grid">
              <div><dt>知识库</dt><dd>{{ entityDetail.data.entity.library.name }}</dd></div>
              <div><dt>Ontology</dt><dd :title="entityDetail.data.entity.ontology_version_id">{{ shortCatalogId(entityDetail.data.entity.ontology_version_id) }}</dd></div>
              <div><dt>来源</dt><dd>{{ sourceTypeLabel(entityDetail.data.entity.source_type) }}</dd></div>
              <div><dt>可信度</dt><dd>{{ formatCatalogConfidence(entityDetail.data.entity.confidence) }}</dd></div>
            </dl>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>属性</h4><span>{{ graphPropertyRows(entityDetail.data.properties).length }} 项</span></div>
            <dl v-if="graphPropertyRows(entityDetail.data.properties).length" class="graph-property-list">
              <div v-for="item in graphPropertyRows(entityDetail.data.properties)" :key="item.key"><dt>{{ item.key }}</dt><dd>{{ item.value }}</dd></div>
            </dl><div v-else class="graph-inline-empty">暂无属性</div>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>别名</h4><span>{{ entityDetail.data.alias_count }} 项</span></div>
            <div v-if="entityDetail.data.aliases.length" class="graph-alias-list">
              <div v-for="alias in entityDetail.data.aliases" :key="alias.id">
                <strong>{{ alias.alias }}</strong><span>{{ sourceTypeLabel(alias.source_type) }} · {{ graphFactLabel(alias.status) }}</span>
                <el-button v-if="canManage && alias.status === 'active'" link type="danger"
                           :disabled="mutation.loading" @click="disableAlias(alias)">停用</el-button>
              </div>
            </div><div v-else class="graph-inline-empty">暂无别名</div>
            <p v-if="entityDetail.data.aliases_truncated" class="graph-truncated">仅显示前 {{ entityDetail.data.aliases.length }} 项</p>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>原文证据</h4><span>{{ entityDetail.data.evidence_count }} 项</span></div>
            <div v-if="entityDetail.data.evidence.length" class="graph-evidence-list">
              <button v-for="locator in entityDetail.data.evidence" :key="locator.evidence_id"
                      @click="openEvidence(locator, 'entity', entityDetail.data.entity)">
                <local-icon icon="mdi:text-search"></local-icon><span>{{ catalogPageLabel(locator) }}</span><small>{{ catalogTitlePath(locator) }}</small>
              </button>
            </div><div v-else class="graph-inline-empty">暂无证据</div>
            <p v-if="entityDetail.data.evidence_truncated" class="graph-truncated">仅显示前 {{ entityDetail.data.evidence.length }} 项</p>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>相关关系</h4><span>{{ entityDetail.data.relation_count }} 项</span></div>
            <div v-if="entityDetail.data.related_relations.length" class="graph-related-list">
              <button v-for="item in entityDetail.data.related_relations" :key="item.id" @click="openRelatedRelation(item)">
                <strong>{{ item.source.canonical_name }}</strong><span>{{ item.relation_type.label }}</span><strong>{{ item.target.canonical_name }}</strong>
              </button>
            </div><div v-else class="graph-inline-empty">暂无相关关系</div>
            <p v-if="entityDetail.data.relations_truncated" class="graph-truncated">仅显示前 {{ entityDetail.data.related_relations.length }} 项</p>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>相关文档</h4><span>{{ entityDetail.data.document_count }} 项</span></div>
            <div v-if="entityDetail.data.documents.length" class="graph-document-list">
              <button v-for="item in entityDetail.data.documents" :key="item.document_id"
                      @click="openCatalogDocument(item, entityDetail.data.entity.library.slug)">
                <local-icon icon="mdi:file-document-outline"></local-icon><span>{{ item.title || '未命名文档' }}</span><small>{{ item.evidence_count }} 条证据</small>
              </button>
            </div><div v-else class="graph-inline-empty">暂无相关文档</div>
            <p v-if="entityDetail.data.documents_truncated" class="graph-truncated">仅显示前 {{ entityDetail.data.documents.length }} 项</p>
          </section>
          <section v-if="entityDetail.data.extraction" class="graph-detail-section">
            <div class="graph-section-heading"><h4>抽取来源</h4>
              <el-button v-if="canManage" plain :disabled="mutation.loading"
                         @click="rerunExtraction(entityDetail.data.extraction)">重新抽取</el-button>
            </div>
            <dl class="graph-identity-grid">
              <div><dt>模型服务</dt><dd>{{ entityDetail.data.extraction.model_provider }}</dd></div>
              <div><dt>模型</dt><dd>{{ entityDetail.data.extraction.model_name }}</dd></div>
              <div><dt>抽取版本</dt><dd>{{ entityDetail.data.extraction.prompt_version }}</dd></div>
              <div><dt>任务</dt><dd :title="entityDetail.data.extraction.job_id">{{ shortCatalogId(entityDetail.data.extraction.job_id) }}</dd></div>
            </dl>
          </section>
        </template>
      </el-drawer>

      <el-drawer v-model="relationDetail.open" class="graph-detail-drawer" size="720px"
                 title="关系详情" @closed="closeRelationDetail">
        <div v-if="relationDetail.loading" class="graph-detail-loading">正在加载关系...</div>
        <div v-else-if="relationDetail.error" class="graph-state"><strong>{{ relationDetail.error.message }}</strong></div>
        <template v-else-if="relationDetail.data">
          <section class="graph-detail-identity">
            <div class="graph-detail-actions">
              <el-button v-if="canWrite" @click="openRelationCorrection">修正</el-button>
              <el-button v-if="canManage && relationDetail.data.relation.status === 'active'"
                         type="warning" plain @click="stageFactState('relation', 'disable')">停用</el-button>
              <el-button v-if="canManage && relationDetail.data.relation.status === 'disabled'"
                         type="success" plain @click="stageFactState('relation', 'restore')">恢复</el-button>
            </div>
            <div class="graph-relation-hero">
              <strong>{{ relationDetail.data.relation.source.canonical_name }}</strong>
              <span><local-icon icon="carbon:chart-relationship"></local-icon>{{ relationDetail.data.relation.relation_type.label }}</span>
              <strong>{{ relationDetail.data.relation.target.canonical_name }}</strong>
            </div>
            <div class="graph-tag-stack"><el-tag :type="statusTag(relationDetail.data.relation.status)">{{ graphFactLabel(relationDetail.data.relation.status) }}</el-tag><el-tag :type="statusTag(relationDetail.data.relation.review_status)" effect="plain">{{ graphReviewLabel(relationDetail.data.relation.review_status) }}</el-tag></div>
            <dl class="graph-identity-grid">
              <div><dt>知识库</dt><dd>{{ relationDetail.data.relation.library.name }}</dd></div>
              <div><dt>Ontology</dt><dd :title="relationDetail.data.relation.ontology_version_id">{{ shortCatalogId(relationDetail.data.relation.ontology_version_id) }}</dd></div>
              <div><dt>方向</dt><dd>{{ relationDetail.data.relation.direction === 'directed' ? '有向' : '无向' }}</dd></div>
              <div><dt>可信度</dt><dd>{{ formatCatalogConfidence(relationDetail.data.relation.confidence) }}</dd></div>
            </dl>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>属性</h4><span>{{ graphPropertyRows(relationDetail.data.properties).length }} 项</span></div>
            <dl v-if="graphPropertyRows(relationDetail.data.properties).length" class="graph-property-list">
              <div v-for="item in graphPropertyRows(relationDetail.data.properties)" :key="item.key"><dt>{{ item.key }}</dt><dd>{{ item.value }}</dd></div>
            </dl><div v-else class="graph-inline-empty">暂无属性</div>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>原文证据</h4><span>{{ relationDetail.data.evidence_count }} 项</span></div>
            <div v-if="relationDetail.data.evidence.length" class="graph-evidence-list">
              <button v-for="locator in relationDetail.data.evidence" :key="locator.evidence_id"
                      @click="openEvidence(locator, 'relation', relationDetail.data.relation)">
                <local-icon icon="mdi:text-search"></local-icon><span>{{ catalogPageLabel(locator) }}</span><small>{{ catalogTitlePath(locator) }}</small>
              </button>
            </div><div v-else class="graph-inline-empty">暂无证据</div>
            <p v-if="relationDetail.data.evidence_truncated" class="graph-truncated">仅显示前 {{ relationDetail.data.evidence.length }} 项</p>
          </section>
          <section class="graph-detail-section">
            <div class="graph-section-heading"><h4>相关文档</h4><span>{{ relationDetail.data.document_count }} 项</span></div>
            <div v-if="relationDetail.data.documents.length" class="graph-document-list">
              <button v-for="item in relationDetail.data.documents" :key="item.document_id"
                      @click="openCatalogDocument(item, relationDetail.data.relation.library.slug)">
                <local-icon icon="mdi:file-document-outline"></local-icon><span>{{ item.title || '未命名文档' }}</span><small>{{ item.evidence_count }} 条证据</small>
              </button>
            </div><div v-else class="graph-inline-empty">暂无相关文档</div>
            <p v-if="relationDetail.data.documents_truncated" class="graph-truncated">仅显示前 {{ relationDetail.data.documents.length }} 项</p>
          </section>
          <section v-if="relationDetail.data.extraction" class="graph-detail-section">
            <div class="graph-section-heading"><h4>抽取来源</h4>
              <el-button v-if="canManage" plain :disabled="mutation.loading"
                         @click="rerunExtraction(relationDetail.data.extraction)">重新抽取</el-button>
            </div>
            <dl class="graph-identity-grid">
              <div><dt>模型服务</dt><dd>{{ relationDetail.data.extraction.model_provider }}</dd></div>
              <div><dt>模型</dt><dd>{{ relationDetail.data.extraction.model_name }}</dd></div>
              <div><dt>抽取版本</dt><dd>{{ relationDetail.data.extraction.prompt_version }}</dd></div>
              <div><dt>任务</dt><dd :title="relationDetail.data.extraction.job_id">{{ shortCatalogId(relationDetail.data.extraction.job_id) }}</dd></div>
            </dl>
          </section>
        </template>
      </el-drawer>

      <el-dialog v-model="factDialog.open" class="graph-command-dialog" width="560px"
                 :title="factDialog.kind === 'entity' ? '新增实体' : '新增关系'">
        <el-alert v-if="writeContext.error" :title="writeContext.error.message"
                  type="warning" :closable="false" show-icon />
        <el-form label-position="top" @submit.prevent="submitFact">
          <el-form-item label="Ontology">
            <el-select :model-value="factDialog.ontologyId" :disabled="mutation.loading"
                       @change="changeFactOntology">
              <el-option v-for="item in writeContext.data?.ontology_versions || []" :key="item.id"
                         :label="item.version_key + ' v' + item.version_no" :value="String(item.id)" />
            </el-select>
          </el-form-item>
          <el-form-item :label="factDialog.kind === 'entity' ? '实体类型' : '关系类型'">
            <el-select v-model="factDialog.typeId" :disabled="mutation.loading">
              <el-option v-for="item in factTypeOptions" :key="item.id"
                         :label="item.label" :value="String(item.id)" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="factDialog.kind === 'entity'" label="实体名称">
            <el-input v-model="factDialog.canonicalName" maxlength="512" show-word-limit />
          </el-form-item>
          <template v-else>
            <el-form-item label="源实体">
              <el-select v-model="factDialog.sourceEntityId" filterable
                         :loading="entityOptions.loading" :disabled="mutation.loading">
                <el-option v-for="item in entityOptions.items" :key="item.id"
                           :label="item.canonical_name" :value="String(item.id)" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标实体">
              <el-select v-model="factDialog.targetEntityId" filterable
                         :loading="entityOptions.loading" :disabled="mutation.loading">
                <el-option v-for="item in entityOptions.items" :key="item.id"
                           :label="item.canonical_name" :value="String(item.id)" />
              </el-select>
            </el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="factDialog.open = false">取消</el-button>
          <el-button type="primary" :loading="mutation.loading" @click="submitFact">提交审核</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="correctionDialog.open" class="graph-command-dialog" width="560px"
                 :title="correctionDialog.kind === 'entity' ? '修正实体' : '修正关系'">
        <el-form label-position="top" @submit.prevent="submitCorrection">
          <el-form-item v-if="correctionDialog.kind === 'entity'" label="实体名称">
            <el-input v-model="correctionDialog.canonicalName" maxlength="512" show-word-limit />
          </el-form-item>
          <template v-else>
            <el-form-item label="源实体">
              <el-select v-model="correctionDialog.sourceEntityId" filterable :loading="entityOptions.loading">
                <el-option v-for="item in entityOptions.items" :key="item.id"
                           :label="item.canonical_name" :value="String(item.id)" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标实体">
              <el-select v-model="correctionDialog.targetEntityId" filterable :loading="entityOptions.loading">
                <el-option v-for="item in entityOptions.items" :key="item.id"
                           :label="item.canonical_name" :value="String(item.id)" />
              </el-select>
            </el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="correctionDialog.open = false">取消</el-button>
          <el-button type="primary" :loading="mutation.loading" @click="submitCorrection">提交审核</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="aliasDialog.open" class="graph-command-dialog" width="520px" title="新增别名">
        <el-form label-position="top" @submit.prevent="submitAlias">
          <el-form-item label="别名"><el-input v-model="aliasDialog.alias" maxlength="512" show-word-limit /></el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="aliasDialog.open = false">取消</el-button>
          <el-button type="primary" :loading="mutation.loading" @click="submitAlias">提交审核</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="mergeDialog.open" class="graph-command-dialog" width="560px" title="合并实体">
        <el-alert v-if="mutation.error?.kind === 'conflict'" :title="mutation.error.message"
                  type="warning" :closable="false" show-icon />
        <el-form label-position="top" @submit.prevent="submitMerge">
          <el-form-item label="保留实体"><el-input :model-value="mergeDialog.survivorName" disabled /></el-form-item>
          <el-form-item label="被合并实体">
            <el-select v-model="mergeDialog.loserId" filterable :loading="entityOptions.loading">
              <el-option v-for="item in entityOptions.items.filter((row) => String(row.id) !== mergeDialog.survivorId)"
                         :key="item.id" :label="item.canonical_name" :value="String(item.id)" />
            </el-select>
          </el-form-item>
          <el-form-item label="原因">
            <el-select v-model="mergeDialog.reasonCode">
              <el-option label="重复实体" value="duplicate_fact" />
              <el-option label="信息错误" value="incorrect_fact" />
              <el-option label="来源更新" value="source_updated" />
              <el-option label="管理员操作" value="operator_request" />
            </el-select>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="mergeDialog.open = false">取消</el-button>
          <el-button type="danger" :loading="mutation.loading" @click="submitMerge">确认合并</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="evidence.open" class="graph-evidence-dialog" width="680px"
                 title="原文证据" @closed="closeEvidence">
        <div v-if="evidence.loading" class="graph-detail-loading">正在加载证据...</div>
        <div v-else-if="evidence.error" class="graph-state"><strong>{{ evidence.error.message }}</strong></div>
        <template v-else-if="evidence.data">
          <div class="graph-evidence-meta"><span>{{ catalogPageLabel(evidence.data) }}</span><span>{{ catalogTitlePath(evidence.data) }}</span></div>
          <blockquote v-if="evidence.data.text_quote" class="graph-evidence-quote">{{ evidence.data.text_quote }}</blockquote>
          <pre v-if="evidenceParts.length" class="graph-evidence-window"><template v-for="(part, index) in evidenceParts" :key="index"><mark v-if="part.highlight">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></pre>
          <dl class="graph-evidence-ids">
            <div><dt>Evidence</dt><dd :title="evidence.data.evidence_id">{{ shortCatalogId(evidence.data.evidence_id) }}</dd></div>
            <div><dt>Revision</dt><dd :title="evidence.data.document_revision_id">{{ shortCatalogId(evidence.data.document_revision_id) }}</dd></div>
          </dl>
          <div class="graph-evidence-actions"><el-button type="primary" @click="openEvidenceDocument"><local-icon icon="mdi:file-document-outline"></local-icon>查看来源文档</el-button></div>
        </template>
      </el-dialog>
    </div>
    `,
};
