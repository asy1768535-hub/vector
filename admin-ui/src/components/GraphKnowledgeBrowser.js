import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';

import * as api from '../api.js';
import {
    graphEntityPageMatches,
    graphErrorProjection,
    graphEntityTypeLabel,
    graphPublicationLabel,
} from '../graph_governance_ui.js';
import {
    explorationSeedSearchRowValid,
} from '../graph_exploration_ui.js';
import GraphExplorer from './GraphExplorer.js';

function scopeKey(organizationId, librarySlugs) {
    return JSON.stringify({ organizationId, librarySlugs: [...librarySlugs] });
}

export default {
    components: { GraphExplorer },
    props: {
        organizationId: { type: String, default: '' },
        librarySlugs: { type: Array, default: () => [] },
        selectedEntityId: { type: String, default: '' },
        selectedEntity: { type: Object, default: null },
        inspectorOpen: { type: Boolean, default: false },
        refreshKey: { type: Number, default: 0 },
        canWrite: { type: Boolean, default: false },
    },
    emits: [
        'select-entity', 'open-entity', 'open-relation',
        'create-entity', 'create-relation',
    ],
    setup(props, { emit }) {
        const directory = reactive({
            query: '', loading: false, items: [], nextCursor: '', error: null,
        });
        const narrowDirectoryMedia = globalThis.matchMedia?.('(max-width: 1024px)') || null;
        const directoryCollapsed = ref(Boolean(narrowDirectoryMedia?.matches));
        const syncResponsiveDirectory = (event) => { directoryCollapsed.value = event.matches; };
        narrowDirectoryMedia?.addEventListener('change', syncResponsiveDirectory);
        let directorySeq = 0;

        const selected = computed(() => (
            props.selectedEntity?.id === props.selectedEntityId
                ? props.selectedEntity
                : directory.items.find((item) => item.id === props.selectedEntityId) || null
        ));
        const directoryCountLabel = computed(() => (
            directory.nextCursor
                ? `已加载前 ${directory.items.length} 条`
                : `共 ${directory.items.length} 个实体`
        ));
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
                        label: graphEntityTypeLabel(item?.entity_type),
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

        async function loadDirectory(append = false) {
            if (append && !directory.nextCursor) return;
            const seq = ++directorySeq;
            const requestedCursor = append ? directory.nextCursor : '';
            const identity = {
                organizationId: props.organizationId,
                librarySlugs: [...props.librarySlugs],
                scopeKey: scopeKey(props.organizationId, props.librarySlugs),
            };
            if (!identity.organizationId || !identity.librarySlugs.length) {
                directory.items = [];
                directory.nextCursor = '';
                directory.error = null;
                directory.loading = false;
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
                    cursor: requestedCursor || undefined,
                    limit: 100,
                });
                if (seq !== directorySeq || identity.scopeKey !== scopeKey(
                    props.organizationId,
                    props.librarySlugs,
                )) return;
                if (!graphEntityPageMatches(data, identity)
                    || !data.items.every(explorationSeedSearchRowValid)) {
                    directory.items = [];
                    directory.nextCursor = '';
                    directory.error = {
                        kind: 'malformed',
                        message: '服务返回的实体目录数据不完整，请重新加载。',
                    };
                    return;
                }
                if (append) {
                    const existingIds = new Set(directory.items.map((item) => item.id));
                    directory.items.push(...data.items.filter((item) => !existingIds.has(item.id)));
                } else directory.items = data.items;
                directory.nextCursor = String(data.next_cursor || '');
            } catch (error) {
                if (seq !== directorySeq || identity.scopeKey !== scopeKey(
                    props.organizationId,
                    props.librarySlugs,
                )) return;
                if (!append) directory.items = [];
                directory.error = graphErrorProjection(error);
            } finally {
                if (seq === directorySeq) directory.loading = false;
            }
        }

        function selectEntity(row) {
            if (!row?.id || !props.librarySlugs.includes(String(row?.library?.slug || ''))) return;
            emit('select-entity', row);
            emit('open-entity', row);
        }

        function createEntity() {
            if (props.canWrite) emit('create-entity');
        }

        function createRelation() {
            if (!props.canWrite) return;
            emit('create-relation', selected.value ? {
                sourceEntityId: selected.value.id,
                ontologyVersionId: selected.value.ontology_version_id,
            } : {});
        }

        function openEntity(node) {
            emit('select-entity', node);
            emit('open-entity', node);
        }

        function openRelation(relation) {
            emit('open-relation', relation);
        }

        const stopScopeWatch = watch(
            () => [props.organizationId, JSON.stringify(props.librarySlugs), props.refreshKey],
            () => {
                directorySeq += 1;
                directory.items = [];
                directory.nextCursor = '';
                loadDirectory();
            },
            { immediate: true },
        );
        onBeforeUnmount(() => {
            directorySeq += 1;
            stopScopeWatch();
            narrowDirectoryMedia?.removeEventListener('change', syncResponsiveDirectory);
        });

        return {
            directory,
            directoryCollapsed,
            directoryCountLabel,
            directoryGroups,
            selected,
            loadDirectory,
            selectEntity,
            createEntity,
            createRelation,
            openEntity,
            openRelation,
            graphPublicationLabel,
        };
    },
    template: `
      <section class="graph-browser"
               :class="{ 'is-directory-collapsed': directoryCollapsed, 'has-inspector': inspectorOpen }"
               aria-label="知识图谱工作台">
        <aside class="graph-browser-directory" aria-label="实体目录">
          <header class="graph-browser-directory-header">
            <div>
              <h3>实体目录</h3>
              <span>{{ directoryCountLabel }}</span>
            </div>
            <div class="graph-browser-directory-actions">
              <el-button v-if="canWrite" type="primary" circle title="新增实体"
                         aria-label="新增实体" @click="createEntity">
                <local-icon icon="mdi:plus"></local-icon>
              </el-button>
              <el-button v-if="canWrite" circle title="新增关系"
                         aria-label="新增关系" @click="createRelation">
                <local-icon icon="carbon:chart-relationship"></local-icon>
              </el-button>
              <el-button circle class="graph-browser-directory-toggle"
                         :title="directoryCollapsed ? '展开实体目录' : '收起实体目录'"
                         :aria-label="directoryCollapsed ? '展开实体目录' : '收起实体目录'"
                         :aria-expanded="!directoryCollapsed"
                         @click="directoryCollapsed = !directoryCollapsed">
                <local-icon :icon="directoryCollapsed ? 'mdi:chevron-right' : 'mdi:chevron-left'"></local-icon>
              </el-button>
            </div>
          </header>
          <div class="graph-browser-search">
            <el-input v-model="directory.query" clearable maxlength="160"
                      placeholder="搜索实体名称" @keyup.enter="loadDirectory()">
              <template #prefix><local-icon icon="mdi:text-search"></local-icon></template>
            </el-input>
            <el-button :loading="directory.loading" @click="loadDirectory()">搜索</el-button>
          </div>
          <div v-if="directory.loading && !directory.items.length" class="graph-browser-state">
            <strong>正在加载实体目录…</strong>
          </div>
          <div v-else-if="directory.error && !directory.items.length" class="graph-browser-state">
            <strong>{{ directory.error.message }}</strong>
            <el-button type="primary" plain @click="loadDirectory()">重新加载</el-button>
          </div>
          <div v-else-if="!directory.items.length" class="graph-browser-state">
            <local-icon icon="carbon:chart-relationship"></local-icon>
            <strong>暂无可浏览的实体</strong>
          </div>
          <div v-else class="graph-browser-tree-grid">
            <section v-for="library in directoryGroups" :key="library.key" class="graph-browser-library-group">
              <div class="graph-browser-type-grid">
                <section v-for="type in library.types" :key="type.key" class="graph-browser-type-group">
                  <div class="graph-browser-type-heading"><strong>{{ type.label }}</strong><span>{{ type.items.length }}</span></div>
                  <button v-for="item in type.items" :key="item.id" type="button"
                          class="graph-browser-entity-row"
                          :class="{ 'is-selected': item.id === selectedEntityId }"
                          :aria-current="item.id === selectedEntityId ? 'true' : undefined"
                          @click="selectEntity(item)">
                    <span>
                      <strong>{{ item.canonical_name }}</strong>
                      <small>{{ type.label }}</small>
                      <small v-if="item.normalized_name && item.normalized_name !== item.canonical_name"
                             class="graph-browser-normalized-name">{{ item.normalized_name }}</small>
                    </span>
                    <el-tag v-if="item.publication_state !== 'published'" type="warning"
                            size="small" effect="plain" class="graph-browser-anomaly-tag">
                      {{ graphPublicationLabel(item.publication_state) }}
                    </el-tag>
                  </button>
                </section>
              </div>
            </section>
            <footer v-if="directory.nextCursor" class="graph-browser-directory-more">
              <span>{{ directory.error ? directory.error.message : '目录还有更多实体，当前仅显示前 ' + directory.items.length + ' 条' }}</span>
              <el-button :loading="directory.loading" @click="loadDirectory(true)">继续加载</el-button>
            </footer>
          </div>
        </aside>

        <section class="graph-browser-canvas" aria-label="图谱画布">
          <graph-explorer :organization-id="organizationId"
                          :library-slugs="librarySlugs"
                          :selected-entity-id="selectedEntityId"
                          :selected-seed="selected"
                          :refresh-key="refreshKey"
                          @open-entity="openEntity"
                          @open-relation="openRelation" />
        </section>
      </section>
    `,
};
