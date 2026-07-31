import { computed, onMounted, reactive, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { useRoute, useRouter } from 'vue-router';

import * as api from '../api.js';
import { formatTime } from '../common_ui.js';
import { manageableLibraries } from '../menu_access.js';
import {
    normalizeSchemaRoute,
    schemaCanEdit,
    schemaErrorKind,
    schemaErrorMessage,
    schemaImpactMatches,
    schemaIntentKey,
    schemaIssueLabel,
    schemaOwnerOptions,
    schemaRouteQuery,
    schemaStatusLabel,
    schemaStatusTag,
    schemaValidationMatches,
    schemaVersionDeletionMatches,
    schemaVersionDetailMatches,
    schemaVersionListMatches,
} from '../schema_lifecycle_ui.js';
import { store } from '../store.js';

const TAB_NAMES = {
    overview: '版本概览',
    entities: '实体类型',
    relations: '关系类型',
    attributes: '属性',
    constraints: '关系约束',
    impact: '影响预览',
};
const CHILD_PATHS = {
    entity_type: 'entity-types',
    relation_type: 'relation-types',
    attribute: 'attributes',
    constraint: 'constraints',
};

function jsonText(value) {
    return value == null ? '' : JSON.stringify(value, null, 2);
}

function parseObject(value, label) {
    if (!String(value || '').trim()) return null;
    let parsed;
    try { parsed = JSON.parse(value); } catch { throw new Error(`${label}不是有效 JSON`); }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${label}必须是 JSON 对象`);
    }
    return parsed;
}

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraries = computed(() => manageableLibraries(store.permissions, store.organizations));
        const scope = reactive({ librarySlug: '', versionId: '', tab: 'overview' });
        const versions = reactive({ loading: false, items: [], libraryId: '', error: '' });
        const detail = reactive({ loading: false, data: null, error: '' });
        const validation = reactive({ loading: false, data: null, error: '' });
        const impact = reactive({ loading: false, data: null, error: '' });
        const mutation = reactive({ loading: false, kind: '', error: '' });
        const dialog = reactive({
            open: false,
            mode: 'create',
            kind: 'entity_type',
            itemId: '',
            key: '',
            label: '',
            description: '',
            propertiesSchema: '',
            direction: 'directed',
            requiresEvidence: true,
            reviewPolicy: 'pending_review',
            ownerKind: 'entity_type',
            ownerTypeId: '',
            valueType: 'string',
            required: false,
            enumValues: '',
            validationSchema: '',
            indexed: false,
            relationTypeId: '',
            sourceEntityTypeId: '',
            targetEntityTypeId: '',
            cardinality: '',
            requiresReview: false,
        });

        let listRequestSeq = 0;
        let detailRequestSeq = 0;
        let validationRequestSeq = 0;
        let impactRequestSeq = 0;
        let mutationRequestSeq = 0;

        const selectedLibrary = computed(() => libraries.value.find(
            (item) => item.slug === scope.librarySlug,
        ) || null);
        const selectedVersion = computed(() => versions.items.find(
            (item) => String(item.id) === scope.versionId,
        ) || null);
        const activeVersion = computed(() => versions.items.find(
            (item) => item.status === 'active'
                && item.version_key === detail.data?.version_key,
        ) || null);
        const canEdit = computed(() => schemaCanEdit(detail.data));
        const ownerOptions = computed(() => schemaOwnerOptions(detail.data));
        const enabledEntityTypes = computed(() => (detail.data?.entity_types || []).filter(
            (item) => item.status === 'draft',
        ));
        const enabledRelationTypes = computed(() => (detail.data?.relation_types || []).filter(
            (item) => item.status === 'draft',
        ));

        function resetWorkspace() {
            listRequestSeq += 1;
            detailRequestSeq += 1;
            validationRequestSeq += 1;
            impactRequestSeq += 1;
            mutationRequestSeq += 1;
            Object.assign(versions, { loading: false, items: [], libraryId: '', error: '' });
            Object.assign(detail, { loading: false, data: null, error: '' });
            Object.assign(validation, { loading: false, data: null, error: '' });
            Object.assign(impact, { loading: false, data: null, error: '' });
            Object.assign(mutation, { loading: false, kind: '', error: '' });
            dialog.open = false;
        }

        function invalidateVersionScope() {
            detailRequestSeq += 1;
            validationRequestSeq += 1;
            impactRequestSeq += 1;
            mutationRequestSeq += 1;
            Object.assign(detail, { loading: false, data: null, error: '' });
            Object.assign(validation, { loading: false, data: null, error: '' });
            Object.assign(impact, { loading: false, data: null, error: '' });
            Object.assign(mutation, { loading: false, kind: '', error: '' });
            dialog.open = false;
        }

        async function canonicalRoute(replace = true) {
            const target = {
                path: '/knowledge-governance/schema',
                query: schemaRouteQuery(scope),
            };
            if (replace) await router.replace(target);
            else await router.push(target);
        }

        async function loadDetail() {
            const identity = {
                libraryId: versions.libraryId,
                librarySlug: scope.librarySlug,
                versionId: scope.versionId,
            };
            if (!identity.librarySlug || !identity.versionId) { detail.data = null; return; }
            const token = ++detailRequestSeq;
            detail.loading = true;
            detail.error = '';
            validation.data = null;
            impact.data = null;
            try {
                const response = await api.getSchemaVersion(identity.librarySlug, identity.versionId);
                if (token !== detailRequestSeq || scope.versionId !== identity.versionId) return;
                if (!schemaVersionDetailMatches(response, identity)) {
                    detail.error = schemaErrorMessage('malformed');
                    detail.data = null;
                    return;
                }
                detail.data = response;
            } catch (error) {
                if (token !== detailRequestSeq || scope.versionId !== identity.versionId) return;
                detail.error = schemaErrorMessage(schemaErrorKind(error));
                detail.data = null;
            } finally {
                if (token === detailRequestSeq) detail.loading = false;
            }
        }

        async function loadVersions({ chooseVersion = true } = {}) {
            const library = selectedLibrary.value;
            if (!library) return;
            const identity = { librarySlug: library.slug };
            const token = ++listRequestSeq;
            versions.loading = true;
            versions.error = '';
            try {
                const response = await api.listSchemaVersions(identity.librarySlug);
                if (token !== listRequestSeq || scope.librarySlug !== identity.librarySlug) return;
                if (!schemaVersionListMatches(response, identity)) {
                    versions.error = schemaErrorMessage('malformed');
                    versions.items = [];
                    return;
                }
                versions.items = response.versions;
                versions.libraryId = String(response.library_id || '');
                if (chooseVersion) {
                    const retained = versions.items.find((item) => String(item.id) === scope.versionId);
                    const next = retained || versions.items[0] || null;
                    scope.versionId = next ? String(next.id) : '';
                    await canonicalRoute();
                }
                await loadDetail();
            } catch (error) {
                if (token !== listRequestSeq || scope.librarySlug !== identity.librarySlug) return;
                versions.error = schemaErrorMessage(schemaErrorKind(error));
                versions.items = [];
                detail.data = null;
            } finally {
                if (token === listRequestSeq) versions.loading = false;
            }
        }

        async function selectLibrary(slug) {
            if (!libraries.value.some((item) => item.slug === slug)) return;
            resetWorkspace();
            Object.assign(scope, { librarySlug: slug, versionId: '', tab: 'overview' });
            await canonicalRoute();
            await loadVersions();
        }

        async function selectVersion(versionId) {
            if (!versions.items.some((item) => String(item.id) === String(versionId))) return;
            invalidateVersionScope();
            scope.versionId = String(versionId);
            await canonicalRoute(false);
            await loadDetail();
        }

        async function selectTab(tab) {
            scope.tab = tab;
            await canonicalRoute();
            if (tab === 'impact' && canEdit.value && !impact.data) await loadImpact();
        }

        async function runValidation() {
            if (!detail.data) return;
            const identity = {
                libraryId: versions.libraryId,
                librarySlug: scope.librarySlug,
                versionId: scope.versionId,
                stateHash: detail.data.state_hash,
            };
            const token = ++validationRequestSeq;
            validation.loading = true;
            validation.error = '';
            try {
                const response = await api.validateSchemaVersion(identity.librarySlug, identity.versionId);
                if (token !== validationRequestSeq || scope.versionId !== identity.versionId) return;
                if (!schemaValidationMatches(response, identity)) {
                    validation.error = schemaErrorMessage('malformed');
                    validation.data = null;
                    await selectTab('overview');
                    return;
                }
                validation.data = response;
                await selectTab('overview');
            } catch (error) {
                if (token !== validationRequestSeq) return;
                validation.error = schemaErrorMessage(schemaErrorKind(error));
                await selectTab('overview');
            } finally {
                if (token === validationRequestSeq) validation.loading = false;
            }
        }

        async function loadImpact() {
            if (!detail.data || !canEdit.value) return;
            const identity = {
                libraryId: versions.libraryId,
                librarySlug: scope.librarySlug,
                versionId: scope.versionId,
                stateHash: detail.data.state_hash,
            };
            const token = ++impactRequestSeq;
            impact.loading = true;
            impact.error = '';
            try {
                const response = await api.getSchemaImpact(identity.librarySlug, identity.versionId);
                if (token !== impactRequestSeq || scope.versionId !== identity.versionId) return;
                if (!schemaImpactMatches(response, identity)) {
                    impact.error = schemaErrorMessage('malformed');
                    impact.data = null;
                    return;
                }
                impact.data = response;
            } catch (error) {
                if (token !== impactRequestSeq) return;
                impact.error = schemaErrorMessage(schemaErrorKind(error));
            } finally {
                if (token === impactRequestSeq) impact.loading = false;
            }
        }

        async function runMutation(kind, operation, successMessage) {
            if (mutation.loading || !detail.data) return null;
            const identity = { slug: scope.librarySlug, versionId: scope.versionId };
            const token = ++mutationRequestSeq;
            Object.assign(mutation, { loading: true, kind, error: '' });
            try {
                const response = await operation();
                if (token !== mutationRequestSeq
                    || scope.librarySlug !== identity.slug
                    || scope.versionId !== identity.versionId) return null;
                const version = response?.version;
                if (!schemaVersionDetailMatches(version, {
                    libraryId: versions.libraryId,
                    librarySlug: identity.slug,
                    versionId: kind === 'clone' ? version?.id : identity.versionId,
                }) || typeof response?.reused !== 'boolean'
                    || !response?.action_id
                    || (kind === 'clone'
                        && String(version?.parent_version_id || '') !== identity.versionId)) {
                    mutation.error = schemaErrorMessage('malformed');
                    return null;
                }
                detail.data = version;
                scope.versionId = String(version.id);
                validation.data = null;
                impact.data = null;
                dialog.open = false;
                ElMessage.success(successMessage);
                await loadVersions({ chooseVersion: false });
                await canonicalRoute();
                return response;
            } catch (error) {
                if (token !== mutationRequestSeq) return null;
                const errorKind = schemaErrorKind(error);
                mutation.error = schemaErrorMessage(errorKind);
                if (errorKind === 'conflict') {
                    dialog.open = false;
                    validation.data = null;
                    impact.data = null;
                    await loadVersions();
                }
                return null;
            } finally {
                if (token === mutationRequestSeq) Object.assign(mutation, { loading: false, kind: '' });
            }
        }

        async function cloneActive() {
            const version = detail.data;
            if (!version || version.status !== 'active') return;
            let description;
            try {
                const result = await ElMessageBox.prompt('新草稿说明', '克隆为草稿', {
                    confirmButtonText: '创建草稿',
                    cancelButtonText: '取消',
                    inputValue: version.description || '',
                });
                description = String(result.value || '').trim() || undefined;
            } catch { return; }
            await runMutation('clone', () => api.cloneSchemaVersion(
                scope.librarySlug,
                scope.versionId,
                {
                    expected_version_state_hash: version.state_hash,
                    idempotency_key: schemaIntentKey('schema-clone'),
                    description,
                },
            ), 'Schema 草稿已创建');
        }

        async function activateDraft() {
            if (!canEdit.value) return;
            try {
                await ElMessageBox.confirm(
                    `确认激活 ${detail.data.version_key} v${detail.data.version_no}？历史图谱和 Publication 会保留在原版本。`,
                    '激活 Schema 版本',
                    { confirmButtonText: '确认激活', cancelButtonText: '取消', type: 'warning' },
                );
            } catch { return; }
            await runMutation('activate', () => api.activateSchemaVersion(
                scope.librarySlug,
                scope.versionId,
                {
                    expected_version_state_hash: detail.data.state_hash,
                    expected_active_version_id: activeVersion.value?.id || null,
                    confirmation: 'activate_schema_version',
                    idempotency_key: schemaIntentKey('schema-activate'),
                },
            ), 'Schema 版本已激活');
        }

        async function deleteDraft() {
            const version = detail.data;
            if (!version || version.status !== 'draft' || mutation.loading) return;
            try {
                await ElMessageBox.confirm(
                    `确认删除草稿 ${version.version_key} v${version.version_no}？删除后无法恢复。`,
                    '删除 Schema 草稿',
                    { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' },
                );
            } catch { return; }

            const identity = {
                libraryId: versions.libraryId,
                slug: scope.librarySlug,
                versionId: scope.versionId,
            };
            const token = ++mutationRequestSeq;
            Object.assign(mutation, { loading: true, kind: 'delete', error: '' });
            try {
                const response = await api.deleteSchemaDraft(identity.slug, identity.versionId, {
                    expected_version_state_hash: version.state_hash,
                    confirmation: 'delete_schema_draft',
                    idempotency_key: schemaIntentKey('schema-delete-draft'),
                });
                if (token !== mutationRequestSeq
                    || scope.librarySlug !== identity.slug
                    || scope.versionId !== identity.versionId) return;
                if (!schemaVersionDeletionMatches(response, identity)) {
                    mutation.error = schemaErrorMessage('malformed');
                    return;
                }
                detailRequestSeq += 1;
                validationRequestSeq += 1;
                impactRequestSeq += 1;
                scope.versionId = '';
                detail.data = null;
                validation.data = null;
                impact.data = null;
                ElMessage.success('Schema 草稿已删除');
                await loadVersions();
            } catch (error) {
                if (token !== mutationRequestSeq) return;
                const errorKind = schemaErrorKind(error);
                mutation.error = schemaErrorMessage(errorKind);
                if (errorKind === 'conflict') await loadVersions();
            } finally {
                if (token === mutationRequestSeq) Object.assign(mutation, { loading: false, kind: '' });
            }
        }

        async function disableActiveSchema() {
            const version = detail.data;
            if (!version || version.status !== 'active') return;
            try {
                await ElMessageBox.confirm(
                    `确认停用 ${version.version_key} v${version.version_no}？历史图谱和 Publication 会保留，但该版本不再作为当前可用 Schema。`,
                    '停用 Schema',
                    { confirmButtonText: '确认停用', cancelButtonText: '取消', type: 'warning' },
                );
            } catch { return; }
            await runMutation('disable-version', () => api.disableSchemaVersion(
                scope.librarySlug,
                scope.versionId,
                {
                    expected_version_state_hash: version.state_hash,
                    confirmation: 'disable_schema_version',
                    idempotency_key: schemaIntentKey('schema-disable-version'),
                },
            ), 'Schema 已停用');
        }

        function resetDialog(kind, mode, item = null) {
            Object.assign(dialog, {
                open: true,
                mode,
                kind,
                itemId: item ? String(item.id) : '',
                key: item?.key || '',
                label: item?.label || '',
                description: item?.description || '',
                propertiesSchema: jsonText(item?.properties_schema),
                direction: item?.direction || 'directed',
                requiresEvidence: item?.requires_evidence ?? true,
                reviewPolicy: item?.default_review_policy || 'pending_review',
                ownerKind: item?.owner_kind || 'entity_type',
                ownerTypeId: item ? String(item.owner_type_id || '') : '',
                valueType: item?.value_type || 'string',
                required: item?.required ?? false,
                enumValues: Array.isArray(item?.enum_values) ? item.enum_values.join('\n') : '',
                validationSchema: jsonText(item?.validation_schema),
                indexed: item?.indexed ?? false,
                relationTypeId: item ? String(item.relation_type_id || '') : '',
                sourceEntityTypeId: item ? String(item.source_entity_type_id || '') : '',
                targetEntityTypeId: item ? String(item.target_entity_type_id || '') : '',
                cardinality: item?.cardinality || '',
                requiresReview: item?.requires_review ?? false,
            });
            if (!item && kind === 'attribute' && ownerOptions.value.length) {
                dialog.ownerKind = ownerOptions.value[0].kind;
                dialog.ownerTypeId = ownerOptions.value[0].id;
            }
        }

        function dialogPayload() {
            const common = {
                expected_version_state_hash: detail.data.state_hash,
                idempotency_key: schemaIntentKey(`schema-${dialog.mode}-${dialog.kind}`),
            };
            if (dialog.kind === 'entity_type') return {
                ...common,
                ...(dialog.mode === 'create' ? { key: dialog.key.trim() } : {}),
                label: dialog.label.trim(),
                description: dialog.description.trim() || null,
                properties_schema: parseObject(dialog.propertiesSchema, '属性结构'),
            };
            if (dialog.kind === 'relation_type') return {
                ...common,
                ...(dialog.mode === 'create' ? { key: dialog.key.trim() } : {}),
                label: dialog.label.trim(),
                description: dialog.description.trim() || null,
                properties_schema: parseObject(dialog.propertiesSchema, '属性结构'),
                direction: dialog.direction,
                requires_evidence: dialog.requiresEvidence,
                default_review_policy: dialog.reviewPolicy,
            };
            if (dialog.kind === 'attribute') return {
                ...common,
                ...(dialog.mode === 'create' ? {
                    owner_kind: dialog.ownerKind,
                    owner_type_id: dialog.ownerTypeId,
                    key: dialog.key.trim(),
                } : {}),
                label: dialog.label.trim(),
                value_type: dialog.valueType,
                required: dialog.required,
                enum_values: dialog.valueType === 'enum'
                    ? dialog.enumValues.split('\n').map((item) => item.trim()).filter(Boolean)
                    : null,
                validation_schema: parseObject(dialog.validationSchema, '校验规则'),
                indexed: dialog.indexed,
            };
            return {
                ...common,
                ...(dialog.mode === 'create' ? {
                    relation_type_id: dialog.relationTypeId,
                    source_entity_type_id: dialog.sourceEntityTypeId,
                    target_entity_type_id: dialog.targetEntityTypeId,
                } : {}),
                cardinality: dialog.cardinality || null,
                requires_review: dialog.requiresReview,
            };
        }

        async function submitDialog() {
            if (!detail.data) return;
            let payload;
            try { payload = dialogPayload(); } catch (error) { ElMessage.warning(error.message); return; }
            const createCalls = {
                entity_type: api.createSchemaEntityType,
                relation_type: api.createSchemaRelationType,
                attribute: api.createSchemaAttribute,
                constraint: api.createSchemaConstraint,
            };
            const operation = dialog.mode === 'create'
                ? () => createCalls[dialog.kind](scope.librarySlug, scope.versionId, payload)
                : () => api.updateSchemaItem(
                    scope.librarySlug,
                    scope.versionId,
                    CHILD_PATHS[dialog.kind],
                    dialog.itemId,
                    payload,
                );
            await runMutation(
                'item',
                operation,
                dialog.mode === 'create' ? 'Schema 项已创建' : 'Schema 项已更新',
            );
        }

        async function disableItem(kind, item) {
            if (!canEdit.value || item.status !== 'draft') return;
            try {
                await ElMessageBox.confirm(
                    `确认停用“${item.label || item.key || item.id}”？`,
                    '停用 Schema 项',
                    { confirmButtonText: '停用', cancelButtonText: '取消', type: 'warning' },
                );
            } catch { return; }
            await runMutation('disable', () => api.disableSchemaItem(
                scope.librarySlug,
                scope.versionId,
                CHILD_PATHS[kind],
                item.id,
                {
                    expected_version_state_hash: detail.data.state_hash,
                    idempotency_key: schemaIntentKey('schema-disable'),
                },
            ), 'Schema 项已停用');
        }

        function setOwner(ownerId) {
            const owner = ownerOptions.value.find((item) => item.id === ownerId);
            if (owner) dialog.ownerKind = owner.kind;
        }

        async function reconcileRoute() {
            const requested = normalizeSchemaRoute(route.query);
            const library = libraries.value.find((item) => item.slug === requested.librarySlug)
                || libraries.value[0]
                || null;
            if (!library) {
                resetWorkspace();
                Object.assign(scope, { librarySlug: '', versionId: '', tab: 'overview' });
                return;
            }
            const libraryChanged = scope.librarySlug !== library.slug;
            if (libraryChanged) resetWorkspace();
            scope.librarySlug = library.slug;
            scope.tab = requested.tab;
            if (libraryChanged || !versions.items.length) {
                scope.versionId = requested.versionId;
                await canonicalRoute();
                await loadVersions();
                return;
            }
            const version = versions.items.find((item) => String(item.id) === requested.versionId)
                || versions.items[0]
                || null;
            if (String(version?.id || '') !== scope.versionId) {
                invalidateVersionScope();
                scope.versionId = String(version?.id || '');
                await canonicalRoute();
                await loadDetail();
            }
        }

        watch(() => route.query, reconcileRoute, { deep: true });
        watch(() => libraries.value.map((item) => item.slug).join('|'), reconcileRoute);
        onMounted(reconcileRoute);

        return {
            TAB_NAMES,
            libraries,
            scope,
            versions,
            detail,
            validation,
            impact,
            mutation,
            dialog,
            selectedLibrary,
            selectedVersion,
            activeVersion,
            canEdit,
            ownerOptions,
            enabledEntityTypes,
            enabledRelationTypes,
            formatTime,
            schemaStatusLabel,
            schemaStatusTag,
            schemaIssueLabel,
            selectLibrary,
            selectVersion,
            selectTab,
            loadVersions,
            runValidation,
            loadImpact,
            cloneActive,
            activateDraft,
            deleteDraft,
            disableActiveSchema,
            resetDialog,
            submitDialog,
            disableItem,
            setOwner,
        };
    },
    template: `
    <div class="schema-lifecycle-workspace">
      <header class="schema-lifecycle-header">
        <div class="schema-lifecycle-heading">
          <span class="schema-lifecycle-heading-icon"><local-icon icon="mdi:cog-sync-outline"></local-icon></span>
          <div><h2>Schema 管理</h2><p>Ontology 版本、类型、约束与激活</p></div>
        </div>
        <div class="schema-lifecycle-header-actions">
          <el-select class="schema-lifecycle-library" :model-value="scope.librarySlug"
                     placeholder="选择知识库" @change="selectLibrary">
            <el-option v-for="item in libraries" :key="item.slug" :label="item.name" :value="item.slug" />
          </el-select>
          <el-button title="刷新 Schema" :loading="versions.loading" @click="loadVersions()">
            <local-icon icon="mdi:refresh"></local-icon><span>刷新</span>
          </el-button>
        </div>
      </header>

      <el-alert v-if="!libraries.length" title="当前账号没有可管理的知识库"
                type="info" :closable="false" show-icon />
      <el-alert v-else-if="versions.error" :title="versions.error"
                type="error" :closable="false" show-icon />
      <el-alert v-if="mutation.error" :title="mutation.error"
                type="error" :closable="false" show-icon />

      <div v-if="libraries.length" class="schema-lifecycle-grid">
        <aside class="schema-lifecycle-versions" aria-label="Schema 版本">
          <div class="schema-lifecycle-section-heading">
            <div><h3>版本</h3><span>{{ versions.items.length }}</span></div>
          </div>
          <div v-if="versions.loading" class="schema-lifecycle-state">正在加载版本...</div>
          <div v-else-if="!versions.items.length" class="schema-lifecycle-state">暂无 Schema 版本</div>
          <button v-for="version in versions.items" :key="version.id" type="button"
                  class="schema-version-row"
                  :class="{ 'is-selected': String(version.id) === scope.versionId }"
                  @click="selectVersion(version.id)">
            <span><strong>{{ version.version_key }} v{{ version.version_no }}</strong>
              <small>{{ formatTime(version.updated_at || version.created_at) }}</small></span>
            <el-tag size="small" :type="schemaStatusTag(version.status)">
              {{ schemaStatusLabel(version.status) }}
            </el-tag>
          </button>
        </aside>

        <main class="schema-lifecycle-main">
          <div v-if="detail.loading" class="schema-lifecycle-state schema-lifecycle-detail-state">
            正在加载 Schema...
          </div>
          <div v-else-if="detail.error" class="schema-lifecycle-state schema-lifecycle-detail-state">
            {{ detail.error }}
          </div>
          <div v-else-if="!detail.data" class="schema-lifecycle-state schema-lifecycle-detail-state">
            选择一个版本查看详情
          </div>
          <template v-else>
            <div class="schema-lifecycle-version-head">
              <div><span>Ontology</span><h3>{{ detail.data.version_key }} v{{ detail.data.version_no }}</h3>
                <p>{{ detail.data.description || '未填写版本说明' }}</p></div>
              <div class="schema-lifecycle-version-actions">
                <el-tag :type="schemaStatusTag(detail.data.status)">
                  {{ schemaStatusLabel(detail.data.status) }}
                </el-tag>
                <el-button v-if="detail.data.status === 'active'"
                           :disabled="mutation.loading" @click="cloneActive">
                  <local-icon icon="mdi:content-copy"></local-icon>克隆草稿
                </el-button>
                <el-button v-if="detail.data.status === 'active'" type="danger" plain
                           :loading="mutation.kind === 'disable-version'" @click="disableActiveSchema">
                  <local-icon icon="mdi:archive-arrow-down-outline"></local-icon>停用 Schema
                </el-button>
                <el-button v-if="canEdit" :loading="validation.loading" @click="runValidation">
                  <local-icon icon="mdi:certificate-outline"></local-icon>校验
                </el-button>
                <el-button v-if="canEdit" type="primary"
                           :loading="mutation.kind === 'activate'" @click="activateDraft">
                  <local-icon icon="mdi:publish"></local-icon>激活
                </el-button>
                <el-button v-if="canEdit" type="danger" plain
                           :loading="mutation.kind === 'delete'" @click="deleteDraft">
                  <local-icon icon="mdi:trash-can-outline"></local-icon>删除草稿
                </el-button>
              </div>
            </div>

            <nav class="schema-lifecycle-tabs" aria-label="Schema 视图">
              <button v-for="(label, key) in TAB_NAMES" :key="key" type="button"
                      :class="{ 'is-active': scope.tab === key }"
                      @click="selectTab(key)">{{ label }}</button>
            </nav>

            <section v-if="scope.tab === 'overview'" class="schema-lifecycle-panel">
              <dl class="schema-lifecycle-metrics">
                <div><dt>实体类型</dt><dd>{{ detail.data.entity_type_count }}</dd></div>
                <div><dt>关系类型</dt><dd>{{ detail.data.relation_type_count }}</dd></div>
                <div><dt>属性</dt><dd>{{ detail.data.attribute_count }}</dd></div>
                <div><dt>约束</dt><dd>{{ detail.data.constraint_count }}</dd></div>
              </dl>
              <dl class="schema-lifecycle-identity">
                <div><dt>版本 ID</dt><dd>{{ detail.data.id }}</dd></div>
                <div><dt>父版本</dt><dd>{{ detail.data.parent_version_id || '无' }}</dd></div>
                <div><dt>激活时间</dt><dd>{{ formatTime(detail.data.published_at) || '未激活' }}</dd></div>
                <div><dt>状态哈希</dt><dd><code>{{ detail.data.state_hash }}</code></dd></div>
              </dl>
              <div v-if="validation.data || validation.error" class="schema-validation-block">
                <el-alert v-if="validation.error" :title="validation.error"
                          type="error" :closable="false" show-icon />
                <el-alert v-else-if="validation.data.valid" title="当前草稿校验通过"
                          type="success" :closable="false" show-icon />
                <template v-else>
                  <el-alert title="当前草稿不能激活" type="warning" :closable="false" show-icon />
                  <ul><li v-for="item in validation.data.issues"
                          :key="item.code + item.item_id + item.field">
                    <strong>{{ schemaIssueLabel(item) }}</strong>
                    <code v-if="item.item_id">{{ item.item_id }}</code>
                  </li></ul>
                </template>
              </div>
            </section>

            <section v-if="scope.tab === 'entities'" class="schema-lifecycle-panel">
              <div class="schema-lifecycle-section-heading">
                <div><h3>实体类型</h3><span>{{ detail.data.entity_types.length }}</span></div>
                <el-button v-if="canEdit" type="primary" @click="resetDialog('entity_type', 'create')">
                  <local-icon icon="mdi:plus"></local-icon>新增
                </el-button>
              </div>
              <div class="schema-lifecycle-table-shell">
                <el-table :data="detail.data.entity_types" row-key="id">
                  <el-table-column prop="key" label="标识" min-width="150" />
                  <el-table-column prop="label" label="名称" min-width="160" />
                  <el-table-column label="状态" width="100"><template #default="{ row }">
                    <el-tag size="small" :type="schemaStatusTag(row.status)">{{ schemaStatusLabel(row.status) }}</el-tag>
                  </template></el-table-column>
                  <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }">
                    <el-button text :disabled="!canEdit || row.status !== 'draft'"
                               @click="resetDialog('entity_type', 'update', row)">编辑</el-button>
                    <el-button text type="danger" :disabled="!canEdit || row.status !== 'draft'"
                               @click="disableItem('entity_type', row)">停用</el-button>
                  </template></el-table-column>
                </el-table>
              </div>
            </section>

            <section v-if="scope.tab === 'relations'" class="schema-lifecycle-panel">
              <div class="schema-lifecycle-section-heading">
                <div><h3>关系类型</h3><span>{{ detail.data.relation_types.length }}</span></div>
                <el-button v-if="canEdit" type="primary" @click="resetDialog('relation_type', 'create')">
                  <local-icon icon="mdi:plus"></local-icon>新增
                </el-button>
              </div>
              <div class="schema-lifecycle-table-shell">
                <el-table :data="detail.data.relation_types" row-key="id">
                  <el-table-column prop="key" label="标识" min-width="150" />
                  <el-table-column prop="label" label="名称" min-width="150" />
                  <el-table-column prop="direction" label="方向" width="100" />
                  <el-table-column prop="default_review_policy" label="审核策略" min-width="150" />
                  <el-table-column label="状态" width="100"><template #default="{ row }">
                    <el-tag size="small" :type="schemaStatusTag(row.status)">{{ schemaStatusLabel(row.status) }}</el-tag>
                  </template></el-table-column>
                  <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }">
                    <el-button text :disabled="!canEdit || row.status !== 'draft'"
                               @click="resetDialog('relation_type', 'update', row)">编辑</el-button>
                    <el-button text type="danger" :disabled="!canEdit || row.status !== 'draft'"
                               @click="disableItem('relation_type', row)">停用</el-button>
                  </template></el-table-column>
                </el-table>
              </div>
            </section>

            <section v-if="scope.tab === 'attributes'" class="schema-lifecycle-panel">
              <div class="schema-lifecycle-section-heading">
                <div><h3>属性</h3><span>{{ detail.data.attributes.length }}</span></div>
                <el-button v-if="canEdit" type="primary" @click="resetDialog('attribute', 'create')">
                  <local-icon icon="mdi:plus"></local-icon>新增
                </el-button>
              </div>
              <div class="schema-lifecycle-table-shell">
                <el-table :data="detail.data.attributes" row-key="id">
                  <el-table-column prop="key" label="标识" min-width="140" />
                  <el-table-column prop="label" label="名称" min-width="150" />
                  <el-table-column prop="owner_kind" label="所属" width="120" />
                  <el-table-column prop="value_type" label="值类型" width="110" />
                  <el-table-column label="状态" width="100"><template #default="{ row }">
                    <el-tag size="small" :type="schemaStatusTag(row.status)">{{ schemaStatusLabel(row.status) }}</el-tag>
                  </template></el-table-column>
                  <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }">
                    <el-button text :disabled="!canEdit || row.status !== 'draft'"
                               @click="resetDialog('attribute', 'update', row)">编辑</el-button>
                    <el-button text type="danger" :disabled="!canEdit || row.status !== 'draft'"
                               @click="disableItem('attribute', row)">停用</el-button>
                  </template></el-table-column>
                </el-table>
              </div>
            </section>

            <section v-if="scope.tab === 'constraints'" class="schema-lifecycle-panel">
              <div class="schema-lifecycle-section-heading">
                <div><h3>关系约束</h3><span>{{ detail.data.constraints.length }}</span></div>
                <el-button v-if="canEdit" type="primary" @click="resetDialog('constraint', 'create')">
                  <local-icon icon="mdi:plus"></local-icon>新增
                </el-button>
              </div>
              <div class="schema-lifecycle-table-shell">
                <el-table :data="detail.data.constraints" row-key="id">
                  <el-table-column prop="source_entity_type_key" label="源类型" min-width="140" />
                  <el-table-column prop="relation_type_key" label="关系" min-width="140" />
                  <el-table-column prop="target_entity_type_key" label="目标类型" min-width="140" />
                  <el-table-column prop="cardinality" label="基数" width="130" />
                  <el-table-column label="状态" width="100"><template #default="{ row }">
                    <el-tag size="small" :type="schemaStatusTag(row.status)">{{ schemaStatusLabel(row.status) }}</el-tag>
                  </template></el-table-column>
                  <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }">
                    <el-button text :disabled="!canEdit || row.status !== 'draft'"
                               @click="resetDialog('constraint', 'update', row)">编辑</el-button>
                    <el-button text type="danger" :disabled="!canEdit || row.status !== 'draft'"
                               @click="disableItem('constraint', row)">停用</el-button>
                  </template></el-table-column>
                </el-table>
              </div>
            </section>

            <section v-if="scope.tab === 'impact'" class="schema-lifecycle-panel">
              <div class="schema-lifecycle-section-heading">
                <div><h3>影响预览</h3><span>历史知识不会自动迁移</span></div>
                <el-button :disabled="!canEdit" :loading="impact.loading" @click="loadImpact">
                  <local-icon icon="mdi:refresh"></local-icon>刷新
                </el-button>
              </div>
              <el-alert v-if="!canEdit" title="克隆为草稿后才能预览变更影响"
                        type="info" :closable="false" show-icon />
              <el-alert v-else-if="impact.error" :title="impact.error"
                        type="error" :closable="false" show-icon />
              <div v-else-if="impact.loading" class="schema-lifecycle-state">正在计算影响...</div>
              <template v-else-if="impact.data">
                <dl class="schema-lifecycle-metrics">
                  <div><dt>旧版抽取任务</dt><dd>{{ impact.data.extraction_jobs.source_version_count }}</dd></div>
                  <div><dt>旧版 Publication</dt><dd>{{ impact.data.publications.source_version_count }}</dd></div>
                  <div><dt>Schema 匹配知识库</dt><dd>{{ impact.data.compatibility.filter(item => item.result === 'match').length }}</dd></div>
                  <div><dt>Schema 不匹配</dt><dd>{{ impact.data.compatibility.filter(item => item.result === 'mismatch').length }}</dd></div>
                </dl>
                <div class="schema-impact-grid">
                  <div><h4>实体类型</h4><p>新增 {{ impact.data.entity_types.added.length }} · 删除 {{ impact.data.entity_types.removed.length }} · 变更 {{ impact.data.entity_types.changed.length }}</p></div>
                  <div><h4>关系类型</h4><p>新增 {{ impact.data.relation_types.added.length }} · 删除 {{ impact.data.relation_types.removed.length }} · 变更 {{ impact.data.relation_types.changed.length }}</p></div>
                  <div><h4>属性</h4><p>新增 {{ impact.data.attributes.added.length }} · 删除 {{ impact.data.attributes.removed.length }} · 变更 {{ impact.data.attributes.changed.length }}</p></div>
                  <div><h4>关系约束</h4><p>新增 {{ impact.data.constraints.added.length }} · 删除 {{ impact.data.constraints.removed.length }} · 变更 {{ impact.data.constraints.changed.length }}</p></div>
                </div>
                <div class="schema-compatibility-list">
                  <div v-for="item in impact.data.compatibility" :key="item.library_id">
                    <strong>{{ item.library_slug }}</strong>
                    <el-tag :type="item.result === 'match' ? 'success' : item.result === 'mismatch' ? 'warning' : 'info'">
                      {{ item.result === 'match' ? '匹配' : item.result === 'mismatch' ? '不匹配' : '不可用' }}
                    </el-tag>
                  </div>
                </div>
              </template>
            </section>
          </template>
        </main>
      </div>

      <el-dialog v-model="dialog.open" class="schema-lifecycle-dialog" width="620px"
                 :title="dialog.mode === 'create' ? '新增 Schema 项' : '编辑 Schema 项'"
                 :close-on-click-modal="false">
        <el-form label-position="top" :disabled="mutation.loading">
          <template v-if="dialog.kind === 'entity_type' || dialog.kind === 'relation_type'">
            <el-form-item v-if="dialog.mode === 'create'" label="标识">
              <el-input v-model="dialog.key" maxlength="128" />
            </el-form-item>
            <el-form-item label="名称"><el-input v-model="dialog.label" maxlength="255" /></el-form-item>
            <el-form-item label="说明">
              <el-input v-model="dialog.description" type="textarea" :rows="2" maxlength="2000" />
            </el-form-item>
            <el-form-item v-if="dialog.kind === 'relation_type'" label="方向">
              <el-radio-group v-model="dialog.direction">
                <el-radio-button value="directed">有向</el-radio-button>
                <el-radio-button value="undirected">无向</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="dialog.kind === 'relation_type'" label="审核策略">
              <el-select v-model="dialog.reviewPolicy">
                <el-option label="自动生效" value="auto_active" />
                <el-option label="待审核" value="pending_review" />
                <el-option label="仅人工" value="manual_only" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="dialog.kind === 'relation_type'" label="必须绑定证据">
              <el-switch v-model="dialog.requiresEvidence" />
            </el-form-item>
            <el-form-item label="属性结构">
              <el-input v-model="dialog.propertiesSchema" type="textarea" :rows="5" />
            </el-form-item>
          </template>

          <template v-else-if="dialog.kind === 'attribute'">
            <el-form-item v-if="dialog.mode === 'create'" label="所属类型">
              <el-select v-model="dialog.ownerTypeId" @change="setOwner">
                <el-option v-for="item in ownerOptions" :key="item.kind + item.id"
                           :label="item.label" :value="item.id" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="dialog.mode === 'create'" label="标识">
              <el-input v-model="dialog.key" maxlength="128" />
            </el-form-item>
            <el-form-item label="名称"><el-input v-model="dialog.label" maxlength="255" /></el-form-item>
            <el-form-item label="值类型">
              <el-select v-model="dialog.valueType">
                <el-option v-for="item in ['string','text','integer','number','boolean','date','datetime','enum','json']"
                           :key="item" :label="item" :value="item" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="dialog.valueType === 'enum'" label="枚举值">
              <el-input v-model="dialog.enumValues" type="textarea" :rows="4" />
            </el-form-item>
            <div class="schema-dialog-switches">
              <label><span>必填</span><el-switch v-model="dialog.required" /></label>
              <label><span>建立索引</span><el-switch v-model="dialog.indexed" /></label>
            </div>
            <el-form-item label="校验规则">
              <el-input v-model="dialog.validationSchema" type="textarea" :rows="5" />
            </el-form-item>
          </template>

          <template v-else>
            <el-form-item v-if="dialog.mode === 'create'" label="关系类型">
              <el-select v-model="dialog.relationTypeId">
                <el-option v-for="item in enabledRelationTypes" :key="item.id"
                           :label="item.label" :value="String(item.id)" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="dialog.mode === 'create'" label="源实体类型">
              <el-select v-model="dialog.sourceEntityTypeId">
                <el-option v-for="item in enabledEntityTypes" :key="item.id"
                           :label="item.label" :value="String(item.id)" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="dialog.mode === 'create'" label="目标实体类型">
              <el-select v-model="dialog.targetEntityTypeId">
                <el-option v-for="item in enabledEntityTypes" :key="item.id"
                           :label="item.label" :value="String(item.id)" />
              </el-select>
            </el-form-item>
            <el-form-item label="关系基数">
              <el-select v-model="dialog.cardinality" clearable>
                <el-option label="一对一" value="one_to_one" />
                <el-option label="一对多" value="one_to_many" />
                <el-option label="多对一" value="many_to_one" />
                <el-option label="多对多" value="many_to_many" />
              </el-select>
            </el-form-item>
            <el-form-item label="需要审核"><el-switch v-model="dialog.requiresReview" /></el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="dialog.open = false">取消</el-button>
          <el-button type="primary" :loading="mutation.kind === 'item'" @click="submitDialog">保存</el-button>
        </template>
      </el-dialog>
    </div>
    `,
};
