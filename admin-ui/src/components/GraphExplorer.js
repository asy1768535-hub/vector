import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';

import * as api from '../api.js';
import { graphErrorProjection } from '../graph_governance_ui.js';
import {
    explorationSeedEligible,
    graphExplorationErrorProjection,
    graphTraversalResponseMatches,
    graphTraversalSummary,
    malformedGraphExplorationError,
    graphPublicationItemPageMatches,
    graphPublicationReadMatches,
    staticPublicationPanorama,
} from '../graph_exploration_ui.js';
import GraphCanvas from './GraphCanvas.js';

const PANORAMA_ENTITY_LIMIT = 300;
const PANORAMA_RELATION_LIMIT = 600;
const PANORAMA_TOTAL_ENTITY_LIMIT = 600;
const PANORAMA_TOTAL_RELATION_LIMIT = 1200;
const ITEM_PAGE_SIZE = 500;
const TRAVERSAL_CACHE_LIMIT = 50;
const EMPTY_GRAPH = Object.freeze({ nodes: [], relations: [] });
const MALFORMED_MESSAGE = '服务返回了无法识别的图谱数据，请刷新重试。';
const traversalCache = new Map();

function text(value) {
    return typeof value === 'string' ? value : '';
}

function scopeKey(organizationId, librarySlugs) {
    return JSON.stringify({ organizationId, librarySlugs: [...librarySlugs] });
}

function traversalIdentityKey(organizationId, seed, refreshKey) {
    return JSON.stringify({
        organizationId: text(organizationId),
        libraryId: text(seed?.library?.id),
        librarySlug: text(seed?.library?.slug),
        publicationId: text(seed?.publication?.id),
        ontologyVersionId: text(seed?.ontology_version_id),
        entityId: text(seed?.id),
        refreshKey,
    });
}

function cacheTraversal(key, data) {
    traversalCache.delete(key);
    traversalCache.set(key, data);
    if (traversalCache.size > TRAVERSAL_CACHE_LIMIT) {
        traversalCache.delete(traversalCache.keys().next().value);
    }
}

function panoramaFailure(kind) {
    return Object.assign(new Error(kind), { panoramaKind: kind });
}

function projectPanoramaError(error) {
    if (error?.panoramaKind === 'malformed') {
        return { kind: 'malformed', message: MALFORMED_MESSAGE };
    }
    if (error?.panoramaKind === 'too_large') {
        return {
            kind: 'too_large',
            message: '当前图谱规模较大，请从左侧实体目录选择实体查看关联网络。',
        };
    }
    if (error?.panoramaKind === 'publication_unavailable') {
        return {
            kind: 'publication_unavailable',
            message: '图谱正在根据源文档变更自动清理并刷新，请稍后重试。',
        };
    }
    if (error?.panoramaKind === 'publication_changed') {
        return graphErrorProjection({ status: 409 });
    }
    if (error?.panoramaKind === 'disabled') {
        return graphErrorProjection({ status: 503 });
    }
    return graphErrorProjection(error);
}

function combinePanoramas(panoramas) {
    const first = panoramas[0];
    const nodes = panoramas.flatMap((item) => item.nodes);
    const relations = panoramas.flatMap((item) => item.relations);
    const connectedIds = new Set(relations.flatMap((relation) => [
        relation.source_entity_id,
        relation.target_entity_id,
    ]));
    return {
        ...first,
        publication: panoramas.length === 1 ? first.publication : null,
        publications: panoramas.map((item) => item.publication),
        nodes,
        relations,
        entity_count: nodes.length,
        relation_count: relations.length,
        isolated_count: nodes.filter((node) => !connectedIds.has(node.id)).length,
        evidence_count: [...nodes, ...relations].reduce(
            (total, item) => total + item.evidence.length,
            0,
        ),
        library_count: panoramas.length,
    };
}

export default {
    components: { GraphCanvas },
    props: {
        organizationId: { type: String, default: '' },
        librarySlugs: { type: Array, default: () => [] },
        selectedEntityId: { type: String, default: '' },
        selectedSeed: { type: Object, default: null },
        entityPreview: { type: Boolean, default: false },
        refreshKey: { type: Number, default: 0 },
    },
    emits: ['open-entity', 'open-relation'],
    setup(props, { emit }) {
        const range = ref(props.entityPreview ? 'related' : 'panorama');
        const previewExpanded = ref(false);
        const showIsolated = ref(false);
        const panoramaData = ref(null);
        const graph = reactive({ loading: false, data: null, error: null, partialError: null });
        let requestSeq = 0;
        let loadedScopeKey = '';

        const visibleGraph = computed(() => {
            if (!graph.data || showIsolated.value) return graph.data;
            const connectedIds = new Set(graph.data.relations.flatMap((relation) => [
                relation.source_entity_id,
                relation.target_entity_id,
            ]));
            return {
                ...graph.data,
                nodes: graph.data.nodes.filter((node) => connectedIds.has(node.id)),
            };
        });
        const canvasGraph = computed(() => visibleGraph.value || EMPTY_GRAPH);
        const summary = computed(() => graphTraversalSummary(graph.data));

        async function listAllItems(slug, publication, kind) {
            const identity = {
                publicationId: publication.id,
                libraryId: publication.library_id,
                ontologyVersionId: publication.ontology_version_id,
                entityCount: publication.entity_count,
                relationCount: publication.relation_count,
            };
            const expectedCount = kind === 'entity'
                ? publication.entity_count
                : publication.relation_count;
            const items = [];
            let page = 1;
            do {
                const response = await api.listGraphPublicationItems(slug, publication.id, {
                    item_kind: kind,
                    status: 'active',
                    page,
                    page_size: ITEM_PAGE_SIZE,
                });
                if (!graphPublicationItemPageMatches(response, {
                    ...identity,
                    page,
                    pageSize: ITEM_PAGE_SIZE,
                }, kind)) throw panoramaFailure('malformed');
                items.push(...response.items);
                if (!response.items.length && items.length < response.total) {
                    throw panoramaFailure('malformed');
                }
                page += 1;
            } while (items.length < expectedCount);
            return items;
        }

        async function loadPublicationPanorama(slug, knownPublication = null) {
            const publication = knownPublication || await api.getActiveGraphPublication(slug);
            const identity = {
                publicationId: publication?.id,
                libraryId: publication?.library_id,
                ontologyVersionId: publication?.ontology_version_id,
            };
            if (publication?.publication_enabled === false) throw panoramaFailure('disabled');
            if (publication?.status === 'degraded') throw panoramaFailure('publication_unavailable');
            if (!graphPublicationReadMatches(publication, identity)) {
                throw panoramaFailure('malformed');
            }
            if (publication.entity_count > PANORAMA_ENTITY_LIMIT
                || publication.relation_count > PANORAMA_RELATION_LIMIT) {
                throw panoramaFailure('too_large');
            }
            const [entityItems, relationItems] = await Promise.all([
                listAllItems(slug, publication, 'entity'),
                listAllItems(slug, publication, 'relation'),
            ]);
            const currentPublication = await api.getActiveGraphPublication(slug);
            if (!graphPublicationReadMatches(currentPublication, identity)
                || currentPublication.manifest_hash !== publication.manifest_hash) {
                throw panoramaFailure('publication_changed');
            }
            const panorama = staticPublicationPanorama(
                publication,
                entityItems,
                relationItems,
                { id: publication.library_id, slug, name: slug },
            );
            if (!panorama) throw panoramaFailure('malformed');
            return panorama;
        }

        async function loadPanorama(identity, seq) {
            const metadata = await Promise.allSettled(identity.librarySlugs.map(
                (slug) => api.getActiveGraphPublication(slug),
            ));
            const publications = metadata
                .filter((result) => result.status === 'fulfilled')
                .map((result) => result.value)
                .filter((publication) => publication && Number.isInteger(publication.entity_count)
                    && Number.isInteger(publication.relation_count));
            const totalEntities = publications.reduce(
                (total, publication) => total + publication.entity_count,
                0,
            );
            const totalRelations = publications.reduce(
                (total, publication) => total + publication.relation_count,
                0,
            );
            if (publications.some((publication) => (
                publication.entity_count > PANORAMA_ENTITY_LIMIT
                || publication.relation_count > PANORAMA_RELATION_LIMIT
            )) || totalEntities > PANORAMA_TOTAL_ENTITY_LIMIT
                || totalRelations > PANORAMA_TOTAL_RELATION_LIMIT) {
                if (seq === requestSeq) {
                    graph.error = projectPanoramaError(Object.assign(new Error('too_large'), {
                        panoramaKind: 'too_large',
                    }));
                    graph.data = null;
                }
                return;
            }
            const results = await Promise.allSettled(identity.librarySlugs.map((slug, index) => {
                const result = metadata[index];
                return result?.status === 'fulfilled'
                    ? loadPublicationPanorama(slug, result.value)
                    : Promise.reject(result?.reason || panoramaFailure('malformed'));
            }));
            if (seq !== requestSeq || identity.scopeKey !== scopeKey(
                text(props.organizationId),
                props.librarySlugs.map(text).filter(Boolean),
            )) return;
            const panoramas = results
                .filter((result) => result.status === 'fulfilled')
                .map((result) => result.value);
            const failures = results.filter((result) => result.status === 'rejected');
            if (!panoramas.length) {
                graph.error = failures[0]
                    ? projectPanoramaError(failures[0].reason)
                    : { kind: 'malformed', message: MALFORMED_MESSAGE };
                return;
            }
            graph.partialError = failures.length
                ? projectPanoramaError(failures[0].reason)
                : null;
            panoramaData.value = combinePanoramas(panoramas);
            graph.data = panoramaData.value;
            loadedScopeKey = identity.scopeKey;
        }

        async function loadTraversal(seed) {
            const seq = ++requestSeq;
            graph.loading = false;
            graph.data = null;
            graph.error = null;
            graph.partialError = null;
            range.value = 'related';
            if (!explorationSeedEligible(seed)) {
                graph.error = {
                    kind: 'unpublished',
                    message: '当前实体还没有可用的已发布关联网络。',
                };
                return;
            }
            const identity = {
                ontologyVersionId: seed.ontology_version_id,
                publicationId: seed.publication.id,
                seedEntityId: seed.id,
                maxHops: 2,
                maxNodes: 80,
                maxRelations: 120,
                scopeKey: scopeKey(text(props.organizationId), props.librarySlugs),
            };
            const cacheKey = traversalIdentityKey(
                props.organizationId,
                seed,
                props.refreshKey,
            );
            if (traversalCache.has(cacheKey)) {
                const data = traversalCache.get(cacheKey);
                traversalCache.delete(cacheKey);
                traversalCache.set(cacheKey, data);
                graph.data = data;
                return;
            }
            graph.loading = true;
            try {
                const data = await api.queryPublishedGraph(seed.library.slug, {
                    ontology_version_id: identity.ontologyVersionId,
                    expected_publication_id: identity.publicationId,
                    seeds: [{ entity_id: identity.seedEntityId }],
                    direction: 'both',
                    relation_type_keys: [],
                    max_hops: identity.maxHops,
                    max_nodes: identity.maxNodes,
                    max_relations: identity.maxRelations,
                    include_evidence_locators: true,
                });
                if (seq !== requestSeq || identity.scopeKey !== scopeKey(
                    text(props.organizationId),
                    props.librarySlugs.map(text).filter(Boolean),
                )) return;
                if (!graphTraversalResponseMatches(data, identity)) {
                    graph.error = malformedGraphExplorationError();
                    return;
                }
                cacheTraversal(cacheKey, data);
                graph.data = data;
            } catch (error) {
                if (seq !== requestSeq || identity.scopeKey !== scopeKey(
                    text(props.organizationId),
                    props.librarySlugs.map(text).filter(Boolean),
                )) return;
                graph.error = graphExplorationErrorProjection(error);
            } finally {
                if (seq === requestSeq) graph.loading = false;
            }
        }

        async function loadOverview() {
            const seq = ++requestSeq;
            const organizationId = text(props.organizationId);
            const librarySlugs = props.librarySlugs.map(text).filter(Boolean);
            const identity = {
                organizationId,
                librarySlugs,
                scopeKey: scopeKey(organizationId, librarySlugs),
            };
            panoramaData.value = null;
            graph.data = null;
            graph.error = null;
            graph.partialError = null;
            range.value = 'panorama';
            if (!organizationId || !librarySlugs.length) {
                graph.loading = false;
                return;
            }
            graph.loading = true;
            try {
                await loadPanorama(identity, seq);
            } catch (error) {
                if (seq === requestSeq) graph.error = projectPanoramaError(error);
            } finally {
                if (seq === requestSeq) graph.loading = false;
            }
        }

        async function loadScope() {
            if (!props.entityPreview) {
                await loadOverview();
                return;
            }
            if (!props.organizationId || !props.librarySlugs.length || !props.selectedSeed) {
                requestSeq += 1;
                graph.loading = false;
                graph.data = null;
                graph.error = null;
                graph.partialError = null;
                range.value = 'related';
                return;
            }
            await loadTraversal(props.selectedSeed);
        }

        async function changeRange(next) {
            if (next === 'related') {
                if (!props.selectedSeed) {
                    range.value = 'panorama';
                    return;
                }
                await loadTraversal(props.selectedSeed);
                return;
            }
            range.value = 'panorama';
            if (panoramaData.value) {
                graph.error = null;
                graph.partialError = null;
                graph.data = panoramaData.value;
                return;
            }
            await loadOverview();
        }

        function openEntity(node) {
            emit('open-entity', {
                ...node,
                library: node?.library || props.selectedSeed?.library,
                ontology_version_id: node?.ontology_version_id
                    || props.selectedSeed?.ontology_version_id,
            });
        }

        function openRelation(relation) {
            emit('open-relation', {
                ...relation,
                library: relation?.library || props.selectedSeed?.library,
                ontology_version_id: relation?.ontology_version_id
                    || props.selectedSeed?.ontology_version_id,
            });
        }

        const stopScopeWatch = watch(
            () => [
                props.entityPreview,
                props.organizationId,
                JSON.stringify(props.librarySlugs),
                props.refreshKey,
                props.entityPreview
                    ? traversalIdentityKey(props.organizationId, props.selectedSeed, props.refreshKey)
                    : '',
            ],
            loadScope,
            { immediate: true },
        );
        onBeforeUnmount(() => {
            requestSeq += 1;
            stopScopeWatch();
        });

        return {
            range,
            previewExpanded,
            showIsolated,
            graph,
            canvasGraph,
            summary,
            changeRange,
            openEntity,
            openRelation,
            panoramaEntityLimit: PANORAMA_ENTITY_LIMIT,
        };
    },
    template: `
      <section class="graph-explorer"
               :class="{ 'is-loading': graph.loading, 'is-entity-preview': entityPreview,
                   'is-preview-expanded': entityPreview && previewExpanded }"
               :aria-busy="graph.loading">
        <header class="graph-explorer-toolbar">
          <div class="graph-explorer-range">
            <el-button v-if="entityPreview" text class="graph-preview-expand-button"
                       title="Expand graph" aria-label="Expand graph"
                       @click="previewExpanded = !previewExpanded">
              <local-icon :icon="previewExpanded ? 'mdi:fullscreen-exit' : 'mdi:fullscreen'"></local-icon>
            </el-button>
            <span>{{ entityPreview ? '局部关系' : (range === 'panorama' ? '全景' : '关联') }}</span>
            <el-button v-if="!entityPreview && range === 'related'" text
                       @click="changeRange('panorama')">返回全景</el-button>
          </div>
          <div class="graph-explorer-summary" v-if="graph.data">
            <strong>{{ summary.nodes }} 个实体 · {{ summary.relations }} 条关系</strong>
            <span v-if="summary.truncationLabels.length" class="graph-explorer-truncated">
              已截断：{{ summary.truncationLabels.join('、') }}
            </span>
          </div>
          <el-checkbox v-if="!entityPreview && graph.data?.isolated_count" v-model="showIsolated"
                       class="graph-isolated-toggle">
            显示无关系实体（{{ graph.data.isolated_count }}）
          </el-checkbox>
        </header>
        <el-alert v-if="graph.error" :title="graph.error.message" type="warning"
                  :closable="false" show-icon />
        <el-alert v-if="graph.partialError" :title="'部分知识库未加载：' + graph.partialError.message"
                  type="warning" :closable="false" show-icon />
        <div v-if="graph.loading && !graph.data" class="graph-state graph-explorer-state">
          <strong>正在读取图谱…</strong>
        </div>
        <div v-else-if="!graph.data || !graph.data.nodes.length" class="graph-state graph-explorer-state">
          <strong>{{ graph.error ? '当前画布不可用' : (entityPreview ? '该实体暂无可显示的关联网络' : '当前范围暂无可显示的实体和关系') }}</strong>
        </div>
        <graph-canvas v-show="graph.data && graph.data.nodes.length"
                      :graph="canvasGraph"
                      :selected-id="selectedEntityId"
                      :center-selected="false"
                      :user-zooming-enabled="true"
                      :constrain-to-viewport="false"
                      @open-entity="openEntity"
                      @open-relation="openRelation" />
        <div v-if="graph.loading && graph.data" class="graph-explorer-loading-overlay"
             aria-hidden="true"><span>正在刷新图谱…</span></div>
      </section>
    `,
};
