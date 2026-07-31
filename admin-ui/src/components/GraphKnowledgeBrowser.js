import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';

import * as api from '../api.js';
import { formatCatalogConfidence, formatCatalogTime } from '../catalog_ui.js';
import {
    graphEntityDetailMatches,
    graphEntityPageMatches,
    graphErrorProjection,
    graphFactLabel,
    graphPropertyRows,
    graphPublicationLabel,
    graphRelationPageMatches,
} from '../graph_governance_ui.js';
import {
    explorationSeedEligible,
    explorationSeedSearchRowValid,
    graphExplorationErrorProjection,
    graphTraversalResponseMatches,
    graphTraversalSummary,
    malformedGraphExplorationError,
} from '../graph_exploration_ui.js';
import GraphCanvas from './GraphCanvas.js';

function sourceTypeLabel(value) {
    return { manual: '人工', imported: '导入', extracted: '模型抽取' }[value] || '未知来源';
}

function evidenceLabel(locator, index) {
    if (Number.isInteger(locator?.page_start)) {
        return locator.page_end && locator.page_end !== locator.page_start
            ? `第 ${locator.page_start}-${locator.page_end} 页`
            : `第 ${locator.page_start} 页`;
    }
    return `证据 ${index + 1}`;
}

export default {
    components: { GraphCanvas },
    props: {
        organizationId: { type: String, default: '' },
        librarySlugs: { type: Array, default: () => [] },
        selectedEntityId: { type: String, default: '' },
        refreshKey: { type: Number, default: 0 },
        canWrite: { type: Boolean, default: false },
    },
    emits: ['select-entity', 'edit-entity', 'open-relation', 'open-evidence', 'open-document', 'open-import'],
    setup(props, { emit }) {
        const mode = ref('directory');
        const directory = reactive({ query: '', loading: false, items: [], error: null });
        const overview = reactive({
            loading: false,
            documentCount: null,
            extractionCount: null,
            runningCount: 0,
            failedCount: 0,
            relationCount: null,
            relationCountBounded: false,
            error: null,
        });
        const selected = ref(null);
        const detail = reactive({ loading: false, data: null, error: null });
        const graph = reactive({ loading: false, data: null, error: null });
        const controls = reactive({ maxHops: 1, direction: 'both' });
        let directorySeq = 0;
        let overviewSeq = 0;
        let detailSeq = 0;
        let graphSeq = 0;

        const directoryGroups = computed(() => {
            const libraries = new Map();
            for (const item of directory.items) {
                const libraryKey = String(item?.library?.slug || '');
                const typeKey = String(item?.entity_type?.key || 'unknown');
                if (!libraries.has(libraryKey)) {
                    libraries.set(libraryKey, {
                        key: libraryKey,
                        name: item?.library?.name || libraryKey,
                        types: new Map(),
                    });
                }
                const library = libraries.get(libraryKey);
                if (!library.types.has(typeKey)) {
                    library.types.set(typeKey, {
                        key: typeKey,
                        label: item?.entity_type?.label || typeKey,
                        items: [],
                    });
                }
                library.types.get(typeKey).items.push(item);
            }
            return [...libraries.values()].map((library) => ({
                ...library,
                types: [...library.types.values()],
            }));
        });
        const summary = computed(() => graphTraversalSummary(graph.data));
        const publishedEntityCount = computed(() => directory.items.filter(
            (item) => item.publication_state === 'published',
        ).length);
        const overviewStatus = computed(() => {
            const entityCount = directory.items.length;
            if (overview.loading) {
                return { tone: 'loading', title: '正在检查图谱构建状态', description: '', action: false };
            }
            if (overview.error) {
                return {
                    tone: 'warning',
                    title: '暂时无法读取完整的图谱状态',
                    description: overview.error.message,
                    action: false,
                };
            }
            if (overview.documentCount === 0) {
                return {
                    tone: 'empty',
                    title: '知识库中还没有文档',
                    description: '导入文件并开启知识图谱后，系统会抽取实体和关系。',
                    action: true,
                };
            }
            if (overview.documentCount > 0 && overview.extractionCount === 0) {
                return {
                    tone: 'warning',
                    title: `${overview.documentCount} 个文档已入库，但尚未创建图谱抽取任务`,
                    description: '重新导入文件并开启知识图谱后，系统才会抽取实体和关系。',
                    action: true,
                };
            }
            if (overview.runningCount > 0) {
                return {
                    tone: 'running',
                    title: overview.extractionCount + ' 个抽取任务中有 ' + overview.runningCount + ' 个正在处理',
                    description: '实体和关系会在抽取完成后出现在这里。',
                    action: false,
                };
            }
            if (overview.failedCount > 0 && entityCount === 0) {
                return {
                    tone: 'danger',
                    title: overview.extractionCount + ' 个抽取任务中有 ' + overview.failedCount + ' 个失败',
                    description: '请到文件导入页面查看失败原因并重新导入。',
                    action: true,
                };
            }
            if (overview.extractionCount > 0 && entityCount === 0) {
                return {
                    tone: 'empty',
                    title: '抽取任务已完成，但没有生成可展示的实体',
                    description: '请检查所选 Schema 是否适合文档内容，或重新执行抽取。',
                    action: true,
                };
            }
            if (entityCount > 0 && overview.relationCount === 0) {
                return {
                    tone: 'warning',
                    title: `已生成 ${entityCount} 个实体，暂未形成关系`,
                    description: '实体可以浏览，但需要检查关系类型配置和抽取结果。',
                    action: false,
                };
            }
            if (entityCount > 0 && publishedEntityCount.value === 0) {
                return {
                    tone: 'ready',
                    title: `已抽取 ${entityCount} 个实体和 ${overview.relationCount || 0} 条关系`,
                    description: '当前结果尚未发布，审核发布后才会进入问答图谱。',
                    action: false,
                };
            }
            return {
                tone: 'ready',
                title: `图谱可浏览：${entityCount} 个实体，${overview.relationCount || 0} 条关系`,
                description: '',
                action: false,
            };
        });
        const relationCountLabel = computed(() => (
            overview.relationCountBounded ? `${overview.relationCount}+` : (overview.relationCount ?? '—')
        ));

        function scopeIdentity() {
            return JSON.stringify({
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
            });
        }

        function clearSelection() {
            detailSeq += 1;
            graphSeq += 1;
            selected.value = null;
            Object.assign(detail, { loading: false, data: null, error: null });
            Object.assign(graph, { loading: false, data: null, error: null });
            mode.value = 'directory';
        }

        async function loadDetail(row) {
            const seq = ++detailSeq;
            const identity = {
                organizationId: props.organizationId,
                librarySlug: String(row.library.slug),
                entityId: String(row.id),
                ontologyVersionId: String(row.ontology_version_id || ''),
                scopeKey: scopeIdentity(),
            };
            Object.assign(detail, { loading: true, data: null, error: null });
            try {
                const data = await api.getGraphEntity(
                    identity.organizationId,
                    identity.librarySlug,
                    identity.entityId,
                );
                if (seq !== detailSeq || identity.scopeKey !== scopeIdentity()
                    || selected.value?.id !== identity.entityId) return;
                if (!graphEntityDetailMatches(data, identity)) {
                    detail.error = malformedGraphExplorationError();
                    return;
                }
                detail.data = data;
            } catch (error) {
                if (seq !== detailSeq || identity.scopeKey !== scopeIdentity()) return;
                detail.error = graphErrorProjection(error);
            } finally {
                if (seq === detailSeq) detail.loading = false;
            }
        }

        async function loadGraph(row = selected.value) {
            const seq = ++graphSeq;
            Object.assign(graph, { loading: false, data: null, error: null });
            if (!row || !explorationSeedEligible(row)) {
                graph.error = {
                    kind: 'unpublished',
                    message: '当前实体还没有可用的已发布关系图。',
                };
                return;
            }
            const identity = {
                ontologyVersionId: row.ontology_version_id,
                publicationId: row.publication.id,
                seedEntityId: row.id,
                maxHops: controls.maxHops,
                maxNodes: 80,
                maxRelations: 120,
                scopeKey: scopeIdentity(),
            };
            graph.loading = true;
            try {
                const data = await api.queryPublishedGraph(row.library.slug, {
                    ontology_version_id: identity.ontologyVersionId,
                    expected_publication_id: identity.publicationId,
                    seeds: [{ entity_id: identity.seedEntityId }],
                    direction: controls.direction,
                    relation_type_keys: [],
                    max_hops: identity.maxHops,
                    max_nodes: identity.maxNodes,
                    max_relations: identity.maxRelations,
                    include_evidence_locators: true,
                });
                if (seq !== graphSeq || identity.scopeKey !== scopeIdentity()
                    || selected.value?.id !== identity.seedEntityId) return;
                if (!graphTraversalResponseMatches(data, identity)) {
                    graph.error = malformedGraphExplorationError();
                    return;
                }
                graph.data = data;
            } catch (error) {
                if (seq !== graphSeq || identity.scopeKey !== scopeIdentity()) return;
                graph.error = graphExplorationErrorProjection(error);
            } finally {
                if (seq === graphSeq) graph.loading = false;
            }
        }

        async function selectEntity(row, announce = true) {
            if (!row?.id || !props.librarySlugs.includes(String(row?.library?.slug || ''))) return;
            const changed = selected.value?.id !== row.id;
            selected.value = row;
            mode.value = 'content';
            if (announce) emit('select-entity', row);
            if (changed) {
                graphSeq += 1;
                Object.assign(graph, { loading: false, data: null, error: null });
            }
            await loadDetail(row);
        }

        async function loadDirectory() {
            const seq = ++directorySeq;
            const identity = {
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
                scopeKey: scopeIdentity(),
            };
            if (!identity.organizationId || !identity.librarySlugs.length) {
                directory.items = [];
                directory.error = null;
                clearSelection();
                return;
            }
            directory.loading = true;
            directory.error = null;
            try {
                const data = await api.searchGraphEntities(identity.organizationId, {
                    library_slugs: identity.librarySlugs,
                    query: directory.query.trim() || undefined,
                    ontology_version_ids: [],
                    type_keys: [],
                    statuses: ['active'],
                    source_types: [],
                    publication_state: 'all',
                    limit: 100,
                });
                if (seq !== directorySeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphEntityPageMatches(data, identity)
                    || !data.items.every(explorationSeedSearchRowValid)) {
                    directory.items = [];
                    directory.error = malformedGraphExplorationError();
                    clearSelection();
                    return;
                }
                directory.items = data.items;
                const requested = directory.items.find(
                    (item) => item.id === props.selectedEntityId,
                );
                if (requested) await selectEntity(requested, false);
                else if (!directory.items.length) clearSelection();
            } catch (error) {
                if (seq !== directorySeq || identity.scopeKey !== scopeIdentity()) return;
                directory.items = [];
                directory.error = graphErrorProjection(error);
                clearSelection();
            } finally {
                if (seq === directorySeq) directory.loading = false;
            }
        }

        async function loadOverview() {
            const seq = ++overviewSeq;
            const identity = {
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
                scopeKey: scopeIdentity(),
            };
            Object.assign(overview, {
                loading: false,
                documentCount: null,
                extractionCount: null,
                runningCount: 0,
                failedCount: 0,
                relationCount: null,
                relationCountBounded: false,
                error: null,
            });
            if (!identity.organizationId || identity.librarySlugs.length !== 1) return;
            overview.loading = true;
            const librarySlug = identity.librarySlugs[0];
            try {
                const [documents, extractions, relations] = await Promise.all([
                    api.listDocuments(librarySlug, { limit: 500 }),
                    api.listGraphExtractions(librarySlug, { limit: 100 }),
                    api.searchGraphRelations(identity.organizationId, {
                        library_slugs: identity.librarySlugs,
                        ontology_version_ids: [],
                        type_keys: [],
                        statuses: ['active'],
                        review_statuses: [],
                        source_types: [],
                        publication_state: 'all',
                        limit: 100,
                    }),
                ]);
                if (seq !== overviewSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!Array.isArray(documents) || !Array.isArray(extractions?.items)
                    || !Number.isInteger(extractions?.total)
                    || !graphRelationPageMatches(relations, identity)) {
                    throw new Error('服务返回了无法识别的图谱状态');
                }
                overview.documentCount = documents.length;
                overview.extractionCount = extractions.total;
                overview.runningCount = extractions.items.filter(
                    (item) => item.status === 'queued' || item.status === 'processing',
                ).length;
                overview.failedCount = extractions.items.filter(
                    (item) => item.status === 'failed',
                ).length;
                overview.relationCount = relations.items.length;
                overview.relationCountBounded = Boolean(relations.next_cursor);
            } catch (error) {
                if (seq !== overviewSeq || identity.scopeKey !== scopeIdentity()) return;
                overview.error = graphErrorProjection(error);
            } finally {
                if (seq === overviewSeq) overview.loading = false;
            }
        }

        function changeMode(next) {
            if (!selected.value && next !== 'directory') {
                mode.value = 'directory';
                return;
            }
            if (next === 'graph' && !graph.data && !graph.loading) loadGraph();
        }

        function selectGraphNode(node) {
            if (!selected.value || !node?.id) return;
            selectEntity({
                ...node,
                library: selected.value.library,
                ontology_version_id: selected.value.ontology_version_id,
                publication_state: 'published',
                publication: selected.value.publication,
            });
        }

        function openGraphRelation(relation) {
            if (!selected.value || !relation?.id) return;
            emit('open-relation', {
                ...relation,
                ontology_version_id: selected.value.ontology_version_id,
                library: selected.value.library,
            });
        }

        function openEvidence(locator) {
            if (detail.data?.entity) emit('open-evidence', locator, 'entity', detail.data.entity);
        }

        function openDocument(item) {
            if (selected.value) emit('open-document', item, selected.value.library.slug);
        }

        function openImport() {
            if (props.canWrite && props.librarySlugs.length === 1) emit('open-import');
        }

        function editEntity() {
            if (props.canWrite && detail.data?.entity) emit('edit-entity', detail.data);
        }

        const stopScopeWatch = watch(
            () => [props.organizationId, JSON.stringify(props.librarySlugs)],
            () => {
                directorySeq += 1;
                overviewSeq += 1;
                clearSelection();
                loadDirectory();
                loadOverview();
            },
            { immediate: true },
        );
        const stopEntityWatch = watch(() => props.selectedEntityId, (id) => {
            const row = directory.items.find((item) => item.id === id);
            if (row && selected.value?.id !== id) selectEntity(row, false);
        });
        const stopRefreshWatch = watch(() => props.refreshKey, () => {
            loadOverview();
            if (mode.value === 'directory') loadDirectory();
            else if (mode.value === 'content' && selected.value) loadDetail(selected.value);
            else if (mode.value === 'graph' && selected.value) loadGraph();
        });
        onBeforeUnmount(() => {
            stopScopeWatch();
            stopEntityWatch();
            stopRefreshWatch();
            directorySeq += 1;
            overviewSeq += 1;
            clearSelection();
        });

        return {
            mode,
            directory,
            directoryGroups,
            overview,
            overviewStatus,
            publishedEntityCount,
            relationCountLabel,
            selected,
            detail,
            graph,
            controls,
            summary,
            loadDirectory,
            selectEntity,
            loadGraph,
            changeMode,
            selectGraphNode,
            openGraphRelation,
            openEvidence,
            openDocument,
            openImport,
            editEntity,
            graphFactLabel,
            graphPublicationLabel,
            graphPropertyRows,
            formatCatalogConfidence,
            formatCatalogTime,
            sourceTypeLabel,
            evidenceLabel,
        };
    },
    template: `
      <section class="graph-browser" aria-label="知识图谱浏览工作台">
        <header class="graph-browser-toolbar">
          <el-radio-group v-model="mode" class="graph-browser-mode-switch" @change="changeMode">
            <el-radio-button value="directory">
              <local-icon icon="mdi:bookshelf"></local-icon><span>实体目录</span>
            </el-radio-button>
            <el-radio-button value="content" :disabled="!selected">
              <local-icon icon="mdi:file-document-outline"></local-icon><span>实体详情</span>
            </el-radio-button>
            <el-radio-button value="graph" :disabled="!selected">
              <local-icon icon="carbon:chart-relationship"></local-icon><span>关系网络</span>
            </el-radio-button>
          </el-radio-group>
          <div class="graph-browser-current">
            <span v-if="selected">当前：<strong>{{ selected.canonical_name }}</strong></span>
            <span v-else>尚未选择实体</span>
          </div>
        </header>

        <section v-if="mode === 'directory'" class="graph-browser-directory">
          <section v-if="librarySlugs.length === 1" class="graph-browser-overview"
                   :class="'is-' + overviewStatus.tone" aria-label="图谱构建状态">
            <div class="graph-browser-overview-message">
              <local-icon :icon="overviewStatus.tone === 'danger' ? 'status:failed' : 'carbon:chart-relationship'"></local-icon>
              <div><strong>{{ overviewStatus.title }}</strong><span v-if="overviewStatus.description">{{ overviewStatus.description }}</span></div>
            </div>
            <ol class="graph-browser-pipeline" aria-label="图谱构建流程">
              <li><span>文档来源</span><strong>{{ overview.documentCount ?? '—' }}</strong><small>个文档</small></li>
              <li><span>抽取任务</span><strong>{{ overview.extractionCount ?? '—' }}</strong><small>{{ overview.runningCount }} 个处理中</small></li>
              <li><span>图谱事实</span><strong>{{ directory.items.length }} / {{ relationCountLabel }}</strong><small>实体 / 关系</small></li>
              <li><span>发布状态</span><strong>{{ publishedEntityCount ? '已发布' : '未发布' }}</strong><small>{{ publishedEntityCount }} 个实体可用</small></li>
            </ol>
            <el-button v-if="overviewStatus.action && canWrite" type="primary" plain @click="openImport">
              <local-icon icon="mdi:database-import-outline"></local-icon>前往导入并开启图谱
            </el-button>
          </section>
          <div class="graph-browser-directory-tools">
            <div><h3>实体目录</h3><span>{{ directory.items.length }} 个实体</span></div>
            <div class="graph-browser-search">
              <el-input v-model="directory.query" clearable maxlength="160"
                        placeholder="搜索实体名称" @keyup.enter="loadDirectory">
                <template #prefix><local-icon icon="mdi:text-search"></local-icon></template>
              </el-input>
              <el-button type="primary" :loading="directory.loading" @click="loadDirectory">搜索</el-button>
            </div>
          </div>
          <div v-if="directory.error" class="graph-browser-state"><strong>{{ directory.error.message }}</strong></div>
          <div v-else-if="!directory.loading && !directory.items.length" class="graph-browser-state">
            <local-icon icon="carbon:chart-relationship"></local-icon>
            <strong>暂无可浏览的实体</strong>
          </div>
          <div v-else class="graph-browser-tree-grid">
            <section v-for="library in directoryGroups" :key="library.key" class="graph-browser-library-group">
              <header><local-icon icon="mdi:bookshelf"></local-icon><h4>{{ library.name }}</h4></header>
              <div class="graph-browser-type-grid">
                <section v-for="type in library.types" :key="type.key" class="graph-browser-type-group">
                  <div class="graph-browser-type-heading"><strong>{{ type.label }}</strong><span>{{ type.items.length }}</span></div>
                  <button v-for="item in type.items" :key="item.id" type="button"
                          class="graph-browser-entity-row" @click="selectEntity(item)">
                    <local-icon icon="mdi:file-document-outline"></local-icon>
                    <span><strong>{{ item.canonical_name }}</strong><small>{{ item.normalized_name }}</small></span>
                    <el-tag :type="item.publication_state === 'published' ? 'success' : 'warning'" size="small" effect="plain">
                      {{ graphPublicationLabel(item.publication_state) }}
                    </el-tag>
                  </button>
                </section>
              </div>
            </section>
          </div>
        </section>

        <article v-else-if="mode === 'content'" class="graph-browser-content">
          <div v-if="detail.loading" class="graph-browser-state"><strong>正在读取实体详情...</strong></div>
          <div v-else-if="detail.error" class="graph-browser-state"><strong>{{ detail.error.message }}</strong></div>
          <div v-else-if="detail.data" class="graph-browser-article">
            <div class="graph-browser-title-row">
              <div><span>{{ detail.data.entity.entity_type.label }}</span><h3>{{ detail.data.entity.canonical_name }}</h3></div>
              <div class="graph-tag-stack">
                <el-button v-if="canWrite" type="primary" plain @click="editEntity">
                  修改实体
                </el-button>
                <el-tag :type="detail.data.entity.status === 'active' ? 'success' : 'info'">{{ graphFactLabel(detail.data.entity.status) }}</el-tag>
                <el-tag effect="plain">{{ graphPublicationLabel(detail.data.entity.publication_state) }}</el-tag>
              </div>
            </div>
            <p class="graph-browser-lead">{{ detail.data.entity.normalized_name }}</p>
            <dl class="graph-browser-meta">
              <div><dt>知识库</dt><dd>{{ detail.data.entity.library.name }}</dd></div>
              <div><dt>来源</dt><dd>{{ sourceTypeLabel(detail.data.entity.source_type) }}</dd></div>
              <div><dt>可信度</dt><dd>{{ formatCatalogConfidence(detail.data.entity.confidence) }}</dd></div>
              <div><dt>更新时间</dt><dd>{{ formatCatalogTime(detail.data.entity.updated_at) }}</dd></div>
            </dl>
            <section class="graph-browser-section">
              <div class="graph-section-heading"><h4>属性</h4><span>{{ graphPropertyRows(detail.data.properties).length }} 项</span></div>
              <dl v-if="graphPropertyRows(detail.data.properties).length" class="graph-property-list">
                <div v-for="item in graphPropertyRows(detail.data.properties)" :key="item.key"><dt>{{ item.key }}</dt><dd>{{ item.value }}</dd></div>
              </dl>
              <div v-else class="graph-inline-empty">暂无属性</div>
            </section>
            <section class="graph-browser-section">
              <div class="graph-section-heading"><h4>别名</h4><span>{{ detail.data.alias_count || 0 }} 项</span></div>
              <div v-if="detail.data.aliases?.length" class="graph-browser-aliases">
                <span v-for="alias in detail.data.aliases" :key="alias.id">{{ alias.alias }}</span>
              </div>
              <div v-else class="graph-inline-empty">暂无别名</div>
            </section>
            <section class="graph-browser-section">
              <div class="graph-section-heading"><h4>相关文档</h4><span>{{ detail.data.document_count || 0 }} 项</span></div>
              <div v-if="detail.data.documents?.length" class="graph-document-list">
                <button v-for="item in detail.data.documents" :key="item.document_id" type="button" @click="openDocument(item)">
                  <local-icon icon="mdi:file-document-outline"></local-icon><span>{{ item.title || '未命名文档' }}</span><small>{{ item.evidence_count }} 条证据</small>
                </button>
              </div>
              <div v-else class="graph-inline-empty">暂无相关文档</div>
            </section>
            <section class="graph-browser-section">
              <div class="graph-section-heading"><h4>原文证据</h4><span>{{ detail.data.evidence_count || 0 }} 项</span></div>
              <div v-if="detail.data.evidence?.length" class="graph-evidence-list">
                <button v-for="(locator, index) in detail.data.evidence" :key="locator.evidence_id" type="button" @click="openEvidence(locator)">
                  <local-icon icon="mdi:text-search"></local-icon><span>{{ evidenceLabel(locator, index) }}</span><small>{{ locator.evidence_kind || '原文定位' }}</small>
                </button>
              </div>
              <div v-else class="graph-inline-empty">暂无原文证据</div>
            </section>
          </div>
        </article>

        <section v-else class="graph-browser-map">
          <header class="graph-browser-map-tools">
            <div><h3>关系网络</h3><span v-if="graph.data">{{ summary.nodes }} 个实体 · {{ summary.relations }} 条关系</span></div>
            <div>
              <el-radio-group v-model="controls.maxHops" size="small" @change="loadGraph()">
                <el-radio-button :value="1">1 跳</el-radio-button><el-radio-button :value="2">2 跳</el-radio-button>
              </el-radio-group>
              <el-select v-model="controls.direction" size="small" title="关系方向" @change="loadGraph()">
                <el-option label="双向" value="both" /><el-option label="向外" value="outbound" /><el-option label="向内" value="inbound" />
              </el-select>
              <el-button circle title="重新构建关系图" :loading="graph.loading" @click="loadGraph()"><local-icon icon="status:retry"></local-icon></el-button>
            </div>
          </header>
          <div v-if="graph.loading" class="graph-browser-state"><strong>正在构建关系图...</strong></div>
          <div v-else-if="graph.error" class="graph-browser-state"><local-icon icon="carbon:chart-relationship"></local-icon><strong>{{ graph.error.message }}</strong></div>
          <template v-else-if="graph.data">
            <graph-canvas :graph="graph.data" :selected-id="selected?.id || ''" @open-entity="selectGraphNode" @open-relation="openGraphRelation" />
            <footer class="graph-browser-map-footer">
              <span><i class="graph-browser-node-key is-current"></i>当前实体</span>
              <span><i class="graph-browser-node-key is-related"></i>关联实体</span>
              <span v-if="summary.evidence">{{ summary.evidence }} 条证据定位</span>
            </footer>
          </template>
        </section>
      </section>
    `,
};
