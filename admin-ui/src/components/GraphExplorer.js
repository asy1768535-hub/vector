import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';

import * as api from '../api.js';
import { formatCatalogConfidence, shortCatalogId } from '../catalog_ui.js';
import {
    graphEntityPageMatches,
    graphErrorProjection,
} from '../graph_governance_ui.js';
import {
    explorationSeedEligible,
    explorationSeedKey,
    explorationSeedSearchRowValid,
    graphExplorationErrorProjection,
    graphTraversalResponseMatches,
    graphTraversalSummary,
    malformedGraphExplorationError,
    selectExplorationSeed,
} from '../graph_exploration_ui.js';
import GraphCanvas from './GraphCanvas.js';

const RELATION_KEY_RE = /^[^\s,]{1,128}$/;

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
        refreshKey: { type: Number, default: 0 },
    },
    emits: ['open-entity', 'open-relation', 'open-evidence'],
    setup(props, { emit }) {
        const search = reactive({ query: '', loading: false, items: [], error: null });
        const selectedSeeds = ref([]);
        const controls = reactive({
            maxHops: 1,
            direction: 'both',
            relationTypeKeys: '',
            maxNodes: 60,
            maxRelations: 100,
        });
        const groups = ref([]);
        const controlError = ref('');
        let searchRequestSeq = 0;
        let traversalRequestSeq = 0;

        const selectedKeys = computed(() => new Set(
            selectedSeeds.value.map(explorationSeedKey),
        ));
        const hasRunningGroup = computed(() => groups.value.some((group) => group.loading));

        function scopeIdentity() {
            return JSON.stringify({
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
            });
        }

        function resetExplorer() {
            searchRequestSeq += 1;
            traversalRequestSeq += 1;
            search.query = '';
            search.loading = false;
            search.items = [];
            search.error = null;
            selectedSeeds.value = [];
            groups.value = [];
            controlError.value = '';
        }

        async function searchSeeds() {
            if (!props.organizationId || !props.librarySlugs.length) return;
            const seq = ++searchRequestSeq;
            const identity = {
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
                scopeKey: scopeIdentity(),
            };
            search.loading = true;
            search.error = null;
            try {
                const data = await api.searchGraphEntities(identity.organizationId, {
                    library_slugs: identity.librarySlugs,
                    query: search.query.trim() || undefined,
                    ontology_version_ids: [],
                    type_keys: [],
                    statuses: ['active'],
                    source_types: [],
                    publication_state: 'all',
                    limit: 50,
                });
                if (seq !== searchRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                if (!graphEntityPageMatches(data, identity)
                    || !data.items.every(explorationSeedSearchRowValid)) {
                    search.items = [];
                    search.error = malformedGraphExplorationError();
                    return;
                }
                search.items = data.items;
            } catch (error) {
                if (seq !== searchRequestSeq || identity.scopeKey !== scopeIdentity()) return;
                search.items = [];
                search.error = graphErrorProjection(error);
            } finally {
                if (seq === searchRequestSeq) search.loading = false;
            }
        }

        function seedSelected(seed) {
            return selectedKeys.value.has(explorationSeedKey(seed));
        }

        function toggleSeed(seed) {
            const key = explorationSeedKey(seed);
            if (selectedKeys.value.has(key)) {
                selectedSeeds.value = selectedSeeds.value.filter(
                    (item) => explorationSeedKey(item) !== key,
                );
            } else {
                selectedSeeds.value = selectExplorationSeed(selectedSeeds.value, seed);
            }
            traversalRequestSeq += 1;
            groups.value = [];
            controlError.value = '';
        }

        function removeSeed(seed) {
            if (seedSelected(seed)) toggleSeed(seed);
        }

        function relationKeys() {
            const result = [];
            for (const item of controls.relationTypeKeys.split(/[\n,]/)) {
                const value = item.trim();
                if (!value || result.includes(value)) continue;
                if (!RELATION_KEY_RE.test(value) || result.length >= 8) return null;
                result.push(value);
            }
            return result;
        }

        async function runExploration() {
            if (!selectedSeeds.value.length || hasRunningGroup.value) return;
            const keys = relationKeys();
            if (!keys) {
                controlError.value = '关系类型 Key 最多 8 个，使用逗号分隔且不能包含空格。';
                return;
            }
            controlError.value = '';
            const seq = ++traversalRequestSeq;
            const scopeKey = scopeIdentity();
            const selected = [...selectedSeeds.value];
            groups.value = selected.map((seed) => ({
                key: explorationSeedKey(seed),
                seed,
                loading: true,
                data: null,
                error: null,
            }));

            await Promise.allSettled(groups.value.map(async (group) => {
                const identity = {
                    ontologyVersionId: group.seed.ontology_version_id,
                    publicationId: group.seed.publication.id,
                    seedEntityId: group.seed.id,
                    maxHops: controls.maxHops,
                    maxNodes: controls.maxNodes,
                    maxRelations: controls.maxRelations,
                };
                try {
                    const data = await api.queryPublishedGraph(group.seed.library.slug, {
                        ontology_version_id: identity.ontologyVersionId,
                        expected_publication_id: identity.publicationId,
                        seeds: [{ entity_id: identity.seedEntityId }],
                        direction: controls.direction,
                        relation_type_keys: keys,
                        max_hops: identity.maxHops,
                        max_nodes: identity.maxNodes,
                        max_relations: identity.maxRelations,
                        include_evidence_locators: true,
                    });
                    if (seq !== traversalRequestSeq || scopeKey !== scopeIdentity()) return;
                    if (!graphTraversalResponseMatches(data, identity)) {
                        group.error = malformedGraphExplorationError();
                        return;
                    }
                    group.data = data;
                } catch (error) {
                    if (seq !== traversalRequestSeq || scopeKey !== scopeIdentity()) return;
                    group.error = graphExplorationErrorProjection(error);
                } finally {
                    if (seq === traversalRequestSeq && scopeKey === scopeIdentity()) {
                        group.loading = false;
                    }
                }
            }));
        }

        function entityIdentity(group, node) {
            return {
                ...node,
                ontology_version_id: group.seed.ontology_version_id,
                library: group.seed.library,
            };
        }

        function relationIdentity(group, relation) {
            return {
                ...relation,
                ontology_version_id: group.seed.ontology_version_id,
                library: group.seed.library,
            };
        }

        function openEntity(group, node) {
            emit('open-entity', entityIdentity(group, node));
        }

        function openRelation(group, relation) {
            emit('open-relation', relationIdentity(group, relation));
        }

        function openEvidence(group, locator, kind, fact) {
            const identity = kind === 'entity'
                ? entityIdentity(group, fact)
                : relationIdentity(group, fact);
            emit('open-evidence', locator, kind, identity);
        }

        function nodeFor(group, entityId) {
            return group.data?.nodes.find((item) => item.id === entityId) || null;
        }

        function refreshExplorer() {
            if (selectedSeeds.value.length) runExploration();
            else if (search.items.length || search.query.trim()) searchSeeds();
        }

        watch(
            () => [props.organizationId, JSON.stringify(props.librarySlugs)],
            resetExplorer,
        );
        watch(() => props.refreshKey, refreshExplorer);
        onBeforeUnmount(resetExplorer);

        return {
            search,
            selectedSeeds,
            selectedKeys,
            controls,
            groups,
            controlError,
            hasRunningGroup,
            searchSeeds,
            seedSelected,
            toggleSeed,
            removeSeed,
            runExploration,
            openEntity,
            openRelation,
            openEvidence,
            nodeFor,
            explorationSeedEligible,
            graphTraversalSummary,
            formatCatalogConfidence,
            shortCatalogId,
            sourceTypeLabel,
            evidenceLabel,
            explorationSeedKey,
        };
    },
    template: `
      <section class="graph-explorer">
        <div class="graph-explorer-seed-band">
          <div class="graph-explorer-search">
            <el-input v-model="search.query" clearable maxlength="160"
                      placeholder="搜索实体名称" @keyup.enter="searchSeeds">
              <template #prefix><local-icon icon="mdi:text-search"></local-icon></template>
            </el-input>
            <el-button type="primary" :loading="search.loading" @click="searchSeeds">查找实体</el-button>
          </div>
          <el-alert v-if="search.error" :title="search.error.message"
                    type="warning" :closable="false" show-icon />
          <div v-if="search.items.length" class="graph-explorer-seed-results">
            <label v-for="item in search.items" :key="explorationSeedKey(item)"
                   class="graph-explorer-seed-row">
              <el-checkbox :model-value="seedSelected(item)"
                           :disabled="(!seedSelected(item) && selectedSeeds.length >= 4) || !explorationSeedEligible(item)"
                           @change="toggleSeed(item)" />
              <span class="graph-explorer-seed-name"><strong>{{ item.canonical_name }}</strong><small>{{ item.entity_type.label }} · {{ item.entity_type.key }}</small></span>
              <span class="graph-explorer-seed-library"><strong>{{ item.library.name }}</strong><small>{{ item.library.slug }}</small></span>
              <span class="graph-explorer-seed-version"><small>Ontology</small><code :title="item.ontology_version_id">{{ shortCatalogId(item.ontology_version_id) }}</code></span>
              <el-tag v-if="explorationSeedEligible(item)" type="success" size="small" effect="plain">可探查</el-tag>
              <el-tag v-else type="warning" size="small" effect="plain">发布不可用</el-tag>
            </label>
          </div>
          <div v-else-if="!search.loading && search.query && !search.error" class="graph-inline-empty">没有找到匹配实体</div>
        </div>

        <div class="graph-explorer-selected">
          <div class="graph-section-heading"><h4>探查起点</h4><span>{{ selectedSeeds.length }} / 4</span></div>
          <div v-if="selectedSeeds.length" class="graph-explorer-seed-tags">
            <el-tag v-for="seed in selectedSeeds" :key="explorationSeedKey(seed)" closable
                    effect="plain" @close="removeSeed(seed)">
              {{ seed.canonical_name }} · {{ seed.library.name }}
            </el-tag>
          </div>
          <div v-else class="graph-inline-empty">尚未选择实体</div>
        </div>

        <div class="graph-explorer-controls">
          <label class="graph-field"><span>探查跳数</span>
            <el-radio-group v-model="controls.maxHops">
              <el-radio-button :value="1">一跳</el-radio-button>
              <el-radio-button :value="2">两跳</el-radio-button>
            </el-radio-group>
          </label>
          <label class="graph-field"><span>关系方向</span>
            <el-radio-group v-model="controls.direction">
              <el-radio-button value="both">双向</el-radio-button>
              <el-radio-button value="outbound">向外</el-radio-button>
              <el-radio-button value="inbound">向内</el-radio-button>
            </el-radio-group>
          </label>
          <label class="graph-field graph-explorer-relation-filter"><span>关系类型 Key</span>
            <el-input v-model="controls.relationTypeKeys" clearable maxlength="1031" placeholder="invests,controls" />
          </label>
          <label class="graph-field"><span>实体上限</span>
            <el-input-number v-model="controls.maxNodes" :min="1" :max="100" :step="10" controls-position="right" />
          </label>
          <label class="graph-field"><span>关系上限</span>
            <el-input-number v-model="controls.maxRelations" :min="1" :max="200" :step="10" controls-position="right" />
          </label>
          <el-button type="primary" :loading="hasRunningGroup" :disabled="!selectedSeeds.length"
                     @click="runExploration">
            <local-icon icon="carbon:chart-relationship"></local-icon>开始探查
          </el-button>
        </div>
        <el-alert v-if="controlError" :title="controlError" type="warning" :closable="false" show-icon />

        <div v-if="groups.length" class="graph-explorer-groups">
          <article v-for="group in groups" :key="group.key" class="graph-explorer-group">
            <header class="graph-explorer-group-header">
              <div><strong>{{ group.seed.canonical_name }}</strong><span>{{ group.seed.library.name }} · {{ group.seed.library.slug }}</span></div>
              <el-tag v-if="group.loading" type="info" effect="plain">查询中</el-tag>
              <el-tag v-else-if="group.error" type="warning" effect="plain">未完成</el-tag>
              <el-tag v-else type="success" effect="plain">已完成</el-tag>
            </header>
            <div v-if="group.loading" class="graph-state graph-explorer-group-state"><strong>正在读取已发布图谱...</strong></div>
            <div v-else-if="group.error" class="graph-state graph-explorer-group-state">
              <strong>{{ group.error.message }}</strong>
            </div>
            <template v-else-if="group.data">
              <dl class="graph-explorer-identity">
                <div><dt>Publication</dt><dd :title="group.data.publication.id">{{ shortCatalogId(group.data.publication.id) }}</dd></div>
                <div><dt>Ontology</dt><dd :title="group.data.publication.ontology_version_id">{{ shortCatalogId(group.data.publication.ontology_version_id) }}</dd></div>
                <div><dt>Manifest</dt><dd :title="group.data.publication.manifest_hash">{{ shortCatalogId(group.data.publication.manifest_hash) }}</dd></div>
                <div><dt>实体</dt><dd>{{ graphTraversalSummary(group.data).nodes }}</dd></div>
                <div><dt>关系</dt><dd>{{ graphTraversalSummary(group.data).relations }}</dd></div>
                <div><dt>证据</dt><dd>{{ graphTraversalSummary(group.data).evidence }}</dd></div>
              </dl>
              <el-alert v-if="graphTraversalSummary(group.data).truncationLabels.length"
                        :title="'结果已截断：' + graphTraversalSummary(group.data).truncationLabels.join('、')"
                        type="warning" :closable="false" show-icon />
              <graph-canvas :graph="group.data"
                            @open-entity="openEntity(group, $event)"
                            @open-relation="openRelation(group, $event)" />

              <section class="graph-explorer-table-section">
                <div class="graph-section-heading"><h4>实体</h4><span>{{ group.data.nodes.length }} 项</span></div>
                <div class="graph-explorer-table-shell">
                  <table>
                    <thead><tr><th scope="col">实体</th><th scope="col">类型</th><th scope="col">跳数</th><th scope="col">来源</th><th scope="col">可信度</th><th scope="col">Evidence</th></tr></thead>
                    <tbody><tr v-for="node in group.data.nodes" :key="node.id">
                      <td><button class="graph-explorer-link" @click="openEntity(group, node)">{{ node.canonical_name }}</button></td>
                      <td>{{ node.entity_type.label }}<small>{{ node.entity_type.key }}</small></td>
                      <td>{{ node.depth }}</td><td>{{ sourceTypeLabel(node.source_type) }}</td>
                      <td>{{ formatCatalogConfidence(node.confidence) }}</td>
                      <td><div class="graph-explorer-evidence-actions">
                        <button v-for="(locator, index) in node.evidence" :key="locator.evidence_id"
                                @click="openEvidence(group, locator, 'entity', node)">
                          <local-icon icon="mdi:text-search"></local-icon>{{ evidenceLabel(locator, index) }}
                        </button><span v-if="!node.evidence.length">无</span>
                      </div></td>
                    </tr></tbody>
                  </table>
                </div>
              </section>

              <section class="graph-explorer-table-section">
                <div class="graph-section-heading"><h4>关系</h4><span>{{ group.data.relations.length }} 项</span></div>
                <div class="graph-explorer-table-shell">
                  <table>
                    <thead><tr><th scope="col">源实体</th><th scope="col">关系</th><th scope="col">目标实体</th><th scope="col">跳数</th><th scope="col">可信度</th><th scope="col">Evidence</th></tr></thead>
                    <tbody><tr v-for="relation in group.data.relations" :key="relation.id">
                      <td><button class="graph-explorer-link" @click="openEntity(group, nodeFor(group, relation.source_entity_id))">{{ nodeFor(group, relation.source_entity_id)?.canonical_name }}</button></td>
                      <td><button class="graph-explorer-link graph-explorer-relation-link" @click="openRelation(group, relation)">{{ relation.relation_type.label }}<small>{{ relation.relation_type.key }}</small></button></td>
                      <td><button class="graph-explorer-link" @click="openEntity(group, nodeFor(group, relation.target_entity_id))">{{ nodeFor(group, relation.target_entity_id)?.canonical_name }}</button></td>
                      <td>{{ relation.depth }}</td><td>{{ formatCatalogConfidence(relation.confidence) }}</td>
                      <td><div class="graph-explorer-evidence-actions">
                        <button v-for="(locator, index) in relation.evidence" :key="locator.evidence_id"
                                @click="openEvidence(group, locator, 'relation', relation)">
                          <local-icon icon="mdi:text-search"></local-icon>{{ evidenceLabel(locator, index) }}
                        </button><span v-if="!relation.evidence.length">无</span>
                      </div></td>
                    </tr></tbody>
                  </table>
                </div>
              </section>
            </template>
          </article>
        </div>
      </section>
    `,
};
