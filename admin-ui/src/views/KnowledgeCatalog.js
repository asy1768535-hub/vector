import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';

import * as api from '../api.js';
import {
    advanceCatalogCursor,
    capabilityEntries,
    capabilityStateLabel,
    capabilityStateTag,
    catalogErrorKind,
    catalogEvidenceParts,
    catalogOverallLabel,
    catalogOverallTag,
    catalogPageLabel,
    catalogReviewLabel,
    catalogSourceTypeLabel,
    catalogTitlePath,
    classificationPrimary,
    classificationSecondary,
    classificationStateLabel,
    collectCatalogLabels,
    formatCatalogBytes,
    formatCatalogConfidence,
    formatCatalogTime,
    processingErrorKind,
    processingErrorLabel,
    processingResponseMatches,
    processingStageLabel,
    processingStageRetryable,
    processingStatusLabel,
    processingStatusTag,
    retreatCatalogCursor,
    safeCatalogAccessUrl,
    shortCatalogId,
} from '../catalog_ui.js';
import { dataEmpty, serviceError } from '../illustrations.js';
import { canManageLibrary, readableLibraries, resolveSelectedSlug } from '../menu_access.js';
import { store } from '../store.js';
import KnowledgeAssetViewSwitch from '../components/KnowledgeAssetViewSwitch.js';

const EMPTY_FILTERS = {
    title: '',
    status: '',
    classificationState: '',
    labelId: '',
};

function copyFilters(target, source) {
    target.title = String(source?.title || '').trim();
    target.status = String(source?.status || '');
    target.classificationState = String(source?.classificationState || '');
    target.labelId = String(source?.labelId || '');
}

function fixedErrorMessage(kind) {
    if (kind === 'forbidden') return '你没有读取该知识库目录的权限';
    if (kind === 'unavailable') return '知识目录暂未启用，或当前内容已不可用';
    return '知识目录加载失败，请稍后重试';
}

function fixedProcessingErrorMessage(kind) {
    if (kind === 'forbidden') return '你没有查看该文档处理详情的权限';
    if (kind === 'unavailable') return '文档处理诊断暂不可用';
    if (kind === 'conflict') return '文档或任务状态已变化，请刷新后重试';
    return '文档处理详情加载失败，请稍后重试';
}

export default {
    components: { KnowledgeAssetViewSwitch },
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraries = computed(() => readableLibraries(store.permissions));
        const selectedSlug = ref(null);
        const filterDraft = reactive({ ...EMPTY_FILTERS });
        const appliedFilters = reactive({ ...EMPTY_FILTERS });
        const pageSize = ref(20);
        const cursor = reactive({ history: [], current: null });
        const page = reactive({
            items: [],
            total: 0,
            next_cursor: null,
            loaded: false,
            loading: false,
            errorKind: '',
            errorMessage: '',
        });
        const labelOptions = ref([]);
        const detail = reactive({
            data: null,
            loading: false,
            errorKind: '',
            errorMessage: '',
        });
        const evidence = reactive({
            open: false,
            data: null,
            loading: false,
            errorKind: '',
            errorMessage: '',
        });
        const processing = reactive({
            data: null,
            loading: false,
            errorKind: '',
            errorMessage: '',
            retryingStage: '',
            retryError: '',
        });
        const fileLoadingId = ref('');

        let requestSeq = 0;
        let detailRequestSeq = 0;
        let evidenceRequestSeq = 0;
        let fileRequestSeq = 0;
        let processingRequestSeq = 0;
        let processingMutationSeq = 0;
        let routeReady = false;

        const documentId = computed(() => String(route.query.document || ''));
        const showingDetail = computed(() => Boolean(documentId.value));
        const pageNumber = computed(() => cursor.history.length + 1);
        const selectedLibrary = computed(() => (
            libraries.value.find((item) => item.slug === selectedSlug.value) || null
        ));
        const canManageProcessing = computed(() => canManageLibrary(
            store.permissions,
            store.organizations,
            selectedSlug.value,
        ));
        const hasAppliedFilters = computed(() => Object.values(appliedFilters).some(Boolean));
        const hasDraftFilters = computed(() => Object.values(filterDraft).some(Boolean));
        const detailCapabilities = computed(() => capabilityEntries(detail.data?.capabilities));
        const detailPrimary = computed(() => classificationPrimary(detail.data?.classification));
        const detailSecondary = computed(() => classificationSecondary(detail.data?.classification));
        const evidenceParts = computed(() => catalogEvidenceParts(evidence.data));

        function setCursorState(next) {
            cursor.history.splice(0, cursor.history.length, ...(next.history || []));
            cursor.current = next.current || null;
        }

        function resetCursor() {
            setCursorState({ history: [], current: null });
        }

        function clearProcessing(resetMutation = true) {
            processingRequestSeq += 1;
            if (resetMutation) processingMutationSeq += 1;
            processing.data = null;
            processing.loading = false;
            processing.errorKind = '';
            processing.errorMessage = '';
            processing.retryError = '';
            if (resetMutation) processing.retryingStage = '';
        }

        function clearDetail() {
            detailRequestSeq += 1;
            fileRequestSeq += 1;
            fileLoadingId.value = '';
            detail.data = null;
            detail.loading = false;
            detail.errorKind = '';
            detail.errorMessage = '';
            clearProcessing();
            closeEvidence();
        }

        function closeEvidence() {
            evidenceRequestSeq += 1;
            evidence.open = false;
            evidence.data = null;
            evidence.loading = false;
            evidence.errorKind = '';
            evidence.errorMessage = '';
        }

        function mergeLabelOptions(items) {
            const merged = collectCatalogLabels([
                ...page.items,
                ...(items || []),
                ...labelOptions.value.map((label) => ({ classification: { labels: [label] } })),
            ]);
            labelOptions.value = merged;
        }

        function listParams() {
            return {
                title: appliedFilters.title || null,
                status: appliedFilters.status || null,
                classification_state: appliedFilters.classificationState || null,
                label_id: appliedFilters.labelId || null,
                cursor: cursor.current || null,
                limit: pageSize.value,
            };
        }

        async function loadList(forceRefresh = false, reset = false) {
            if (reset) resetCursor();
            if (!selectedSlug.value) {
                requestSeq += 1;
                page.items = [];
                page.total = 0;
                page.next_cursor = null;
                page.loaded = true;
                page.loading = false;
                return;
            }
            const token = ++requestSeq;
            page.loading = true;
            page.errorKind = '';
            page.errorMessage = '';
            try {
                const response = await api.listCatalogDocuments(
                    selectedSlug.value,
                    listParams(),
                    forceRefresh,
                );
                if (token !== requestSeq) return;
                page.items = Array.isArray(response?.items) ? response.items : [];
                page.total = Number(response?.total || 0);
                page.next_cursor = response?.next_cursor || null;
                page.loaded = true;
                mergeLabelOptions(page.items);
            } catch (error) {
                if (token !== requestSeq) return;
                const kind = catalogErrorKind(error);
                page.items = [];
                page.total = 0;
                page.next_cursor = null;
                page.loaded = true;
                page.errorKind = kind;
                page.errorMessage = kind === 'error'
                    ? (error?.message || fixedErrorMessage(kind))
                    : fixedErrorMessage(kind);
            } finally {
                if (token === requestSeq) page.loading = false;
            }
        }

        async function loadDetail(id, forceRefresh = false) {
            if (!selectedSlug.value || !id) return;
            const token = ++detailRequestSeq;
            clearProcessing(false);
            detail.loading = true;
            detail.data = null;
            detail.errorKind = '';
            detail.errorMessage = '';
            closeEvidence();
            try {
                const response = await api.getCatalogDocument(
                    selectedSlug.value,
                    id,
                    forceRefresh,
                );
                if (token !== detailRequestSeq) return;
                detail.data = response;
                if (canManageProcessing.value) void loadProcessing(id);
            } catch (error) {
                if (token !== detailRequestSeq) return;
                const kind = catalogErrorKind(error);
                detail.errorKind = kind;
                detail.errorMessage = kind === 'error'
                    ? (error?.message || fixedErrorMessage(kind))
                    : fixedErrorMessage(kind);
            } finally {
                if (token === detailRequestSeq) detail.loading = false;
            }
        }

        async function loadProcessing(id) {
            if (!canManageProcessing.value || !selectedSlug.value || !id || !detail.data) {
                clearProcessing(false);
                return;
            }
            const token = ++processingRequestSeq;
            const slug = selectedSlug.value;
            const identity = {
                libraryId: detail.data.library_id,
                documentId: id,
                revisionId: detail.data.revision_id,
            };
            processing.loading = true;
            processing.data = null;
            processing.errorKind = '';
            processing.errorMessage = '';
            processing.retryError = '';
            try {
                const response = await api.getCatalogDocumentProcessing(slug, id);
                if (token !== processingRequestSeq || selectedSlug.value !== slug
                    || documentId.value !== String(id)) return;
                if (!processingResponseMatches(response, identity)) {
                    processing.errorKind = 'conflict';
                    processing.errorMessage = fixedProcessingErrorMessage('conflict');
                    return;
                }
                processing.data = response;
            } catch (error) {
                if (token !== processingRequestSeq) return;
                const kind = processingErrorKind(error);
                processing.errorKind = kind;
                processing.errorMessage = fixedProcessingErrorMessage(kind);
            } finally {
                if (token === processingRequestSeq) processing.loading = false;
            }
        }

        async function retryProcessing(stage) {
            if (!processingStageRetryable(stage) || processing.retryingStage
                || !selectedSlug.value || !detail.data || !canManageProcessing.value) return;
            const token = ++processingMutationSeq;
            const slug = selectedSlug.value;
            const id = String(detail.data.document_id);
            const identity = {
                libraryId: detail.data.library_id,
                documentId: id,
                revisionId: detail.data.revision_id,
            };
            processing.retryingStage = stage.stage;
            processing.retryError = '';
            try {
                const response = await api.retryCatalogDocumentProcessing(
                    slug,
                    id,
                    stage.stage,
                    {
                        source_job_id: stage.job_id,
                        retry_generation: stage.retry_generation,
                    },
                );
                if (token !== processingMutationSeq || selectedSlug.value !== slug
                    || documentId.value !== id) return;
                if (!processingResponseMatches(response, identity)) {
                    processing.retryError = fixedProcessingErrorMessage('conflict');
                    return;
                }
                processing.data = response;
                ElMessage.success(`${processingStageLabel(stage.stage)}已重新排队`);
                await loadDetail(id, true);
            } catch (error) {
                if (token !== processingMutationSeq) return;
                processing.retryError = fixedProcessingErrorMessage(processingErrorKind(error));
            } finally {
                if (token === processingMutationSeq) processing.retryingStage = '';
            }
        }

        async function syncFromRoute() {
            const requestedSlug = String(route.query.library || '');
            const nextSlug = resolveSelectedSlug(requestedSlug, libraries.value);
            if (!nextSlug) {
                selectedSlug.value = null;
                page.loaded = true;
                page.items = [];
                page.total = 0;
                clearDetail();
                return;
            }
            if (requestedSlug !== nextSlug) {
                await router.replace({
                    path: '/knowledge-assets/catalog',
                    query: { library: nextSlug },
                });
                return;
            }
            const libraryChanged = selectedSlug.value !== nextSlug;
            if (libraryChanged) {
                selectedSlug.value = nextSlug;
                labelOptions.value = [];
                page.loaded = false;
                copyFilters(filterDraft, EMPTY_FILTERS);
                copyFilters(appliedFilters, EMPTY_FILTERS);
                resetCursor();
                clearDetail();
            }
            if (libraryChanged || !page.loaded) await loadList(false, true);
            if (documentId.value) await loadDetail(documentId.value);
            else clearDetail();
        }

        async function selectLibrary(value) {
            if (!value || value === selectedSlug.value) return;
            await router.push({
                path: '/knowledge-assets/catalog',
                query: { library: value },
            });
        }

        async function applyFilters() {
            copyFilters(appliedFilters, filterDraft);
            await loadList(false, true);
        }

        async function resetFilters() {
            copyFilters(filterDraft, EMPTY_FILTERS);
            copyFilters(appliedFilters, EMPTY_FILTERS);
            labelOptions.value = [];
            await loadList(false, true);
        }

        async function nextPage() {
            if (!page.next_cursor || page.loading) return;
            setCursorState(advanceCatalogCursor(cursor, page.next_cursor));
            await loadList();
        }

        async function previousPage() {
            if (!cursor.history.length || page.loading) return;
            setCursorState(retreatCatalogCursor(cursor));
            await loadList();
        }

        async function changePageSize() {
            await loadList(false, true);
        }

        async function openDocument(row) {
            if (!row?.document_id || !selectedSlug.value) return;
            await router.push({
                path: '/knowledge-assets/catalog',
                query: { library: selectedSlug.value, document: row.document_id },
            });
        }

        async function backToList() {
            if (!selectedSlug.value) return;
            await router.push({
                path: '/knowledge-assets/catalog',
                query: { library: selectedSlug.value },
            });
        }

        async function refreshCurrent() {
            if (showingDetail.value) await loadDetail(documentId.value, true);
            else await loadList(true);
        }

        async function openEvidence(locator) {
            if (!selectedSlug.value || !locator?.evidence_id) return;
            const token = ++evidenceRequestSeq;
            evidence.open = true;
            evidence.data = null;
            evidence.loading = true;
            evidence.errorKind = '';
            evidence.errorMessage = '';
            try {
                const response = await api.getCatalogEvidence(
                    selectedSlug.value,
                    locator.evidence_id,
                );
                if (token !== evidenceRequestSeq) return;
                evidence.data = response;
            } catch (error) {
                if (token !== evidenceRequestSeq) return;
                const kind = catalogErrorKind(error);
                evidence.errorKind = kind;
                evidence.errorMessage = kind === 'error'
                    ? (error?.message || fixedErrorMessage(kind))
                    : fixedErrorMessage(kind);
            } finally {
                if (token === evidenceRequestSeq) evidence.loading = false;
            }
        }

        function isCurrentFile(revisionFileId) {
            const currentId = detail.data?.file?.id;
            return Boolean(currentId && revisionFileId && String(currentId) === String(revisionFileId));
        }

        async function openFileById(revisionFileId) {
            if (!selectedSlug.value || !isCurrentFile(revisionFileId) || fileLoadingId.value) return;
            const token = ++fileRequestSeq;
            const slug = selectedSlug.value;
            const expectedFileId = String(revisionFileId);
            fileLoadingId.value = expectedFileId;
            try {
                const access = await api.getCatalogFileAccess(slug, expectedFileId);
                if (token !== fileRequestSeq || selectedSlug.value !== slug
                    || !isCurrentFile(expectedFileId)) return;
                if (String(access?.revision_file_id || '') !== expectedFileId) {
                    throw new Error('文件访问身份不匹配');
                }
                const url = safeCatalogAccessUrl(access);
                if (!url) throw new Error('文件访问地址无效');
                const anchor = document.createElement('a');
                anchor.href = url;
                anchor.target = '_blank';
                anchor.rel = 'noopener noreferrer';
                document.body.appendChild(anchor);
                anchor.click();
                anchor.remove();
            } catch (error) {
                if (token !== fileRequestSeq) return;
                ElMessage.error(error?.message || '源文件暂不可用');
            } finally {
                if (token === fileRequestSeq) fileLoadingId.value = '';
            }
        }

        function listClassification(row) {
            const primary = classificationPrimary(row?.classification);
            return primary?.label || classificationStateLabel(row?.classification?.state);
        }

        function listSecondary(row) {
            return classificationSecondary(row?.classification);
        }

        function rowCapabilities(row) {
            return capabilityEntries(row?.capabilities).filter((item) => (
                ['summary', 'outline', 'classification', 'graph'].includes(item.key)
            ));
        }

        watch(
            () => [route.query.library, route.query.document],
            () => { if (routeReady) syncFromRoute(); },
        );

        watch(canManageProcessing, (allowed) => {
            if (!allowed) clearProcessing();
            else if (detail.data && documentId.value) void loadProcessing(documentId.value);
        });

        onMounted(async () => {
            routeReady = true;
            await syncFromRoute();
        });

        return {
            libraries, selectedSlug, selectedLibrary,
            filterDraft, appliedFilters, hasAppliedFilters, hasDraftFilters, labelOptions,
            pageSize, page, pageNumber, cursor,
            showingDetail, detail, detailCapabilities, detailPrimary, detailSecondary,
            evidence, evidenceParts, fileLoadingId,
            canManageProcessing, processing,
            selectLibrary, applyFilters, resetFilters, loadList,
            nextPage, previousPage, changePageSize,
            openDocument, backToList, refreshCurrent, openEvidence, closeEvidence,
            loadProcessing, retryProcessing,
            isCurrentFile, openFileById,
            listClassification, listSecondary, rowCapabilities,
            catalogOverallLabel, catalogOverallTag,
            capabilityStateLabel, capabilityStateTag,
            classificationStateLabel, formatCatalogConfidence, formatCatalogTime,
            formatCatalogBytes, catalogSourceTypeLabel, catalogReviewLabel,
            catalogPageLabel, catalogTitlePath, shortCatalogId,
            processingStageLabel, processingStageRetryable,
            processingStatusLabel, processingStatusTag, processingErrorLabel,
            dataEmpty, serviceError,
        };
    },
    template: `
    <div class="catalog-workspace">
      <knowledge-asset-view-switch :library="selectedSlug || ''" />
      <header class="catalog-page-header">
        <div class="catalog-page-title">
          <el-button v-if="showingDetail" class="catalog-back-button" text title="返回目录"
                     aria-label="返回目录" @click="backToList">
            <local-icon icon="mdi:chevron-left"></local-icon>
          </el-button>
          <div>
            <h2>{{ showingDetail ? (detail.data?.title || '文档详情') : '知识目录' }}</h2>
            <div class="catalog-page-context">
              {{ selectedLibrary ? selectedLibrary.name : '未选择知识库' }}
              <template v-if="showingDetail && detail.data"> · v{{ detail.data.revision_no }}</template>
            </div>
          </div>
        </div>
        <div class="catalog-page-actions">
          <el-select :model-value="selectedSlug" class="catalog-library-select"
                     placeholder="选择知识库" @change="selectLibrary">
            <el-option v-for="library in libraries" :key="library.slug"
                       :label="library.name" :value="library.slug" />
          </el-select>
          <el-button :loading="page.loading || detail.loading" title="刷新当前内容"
                     aria-label="刷新当前内容" @click="refreshCurrent">
            <local-icon icon="status:retry"></local-icon><span>刷新</span>
          </el-button>
        </div>
      </header>

      <el-alert v-if="!libraries.length" class="catalog-scope-alert"
                title="当前账号没有可读取的知识库" type="info" :closable="false" show-icon />

      <template v-if="!showingDetail && libraries.length">
        <section class="catalog-filter-band">
          <el-input v-model="filterDraft.title" clearable maxlength="160"
                    placeholder="搜索文档标题" @keyup.enter="applyFilters">
            <template #prefix><local-icon icon="mdi:text-search"></local-icon></template>
          </el-input>
          <el-select v-model="filterDraft.status" clearable placeholder="文档状态">
            <el-option label="等待中" value="pending" />
            <el-option label="处理中" value="processing" />
            <el-option label="可用" value="ready" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-select v-model="filterDraft.classificationState" clearable placeholder="分类状态">
            <el-option label="未分类" value="unclassified" />
            <el-option label="待审核" value="pending_review" />
            <el-option label="已分类" value="classified" />
            <el-option label="分类失败" value="failed" />
          </el-select>
          <el-select v-model="filterDraft.labelId" clearable filterable placeholder="分类标签">
            <el-option v-for="label in labelOptions" :key="label.id"
                       :label="label.label" :value="label.id" />
          </el-select>
          <div class="catalog-filter-actions">
            <el-button :disabled="!hasAppliedFilters && !hasDraftFilters"
                       @click="resetFilters">重置</el-button>
            <el-button type="primary" :loading="page.loading" @click="applyFilters">筛选</el-button>
          </div>
        </section>

        <section v-if="page.errorKind" class="catalog-result-state">
          <img :src="serviceError" alt="" aria-hidden="true" />
          <h3>{{ page.errorMessage }}</h3>
          <el-button v-if="page.errorKind === 'error'" @click="loadList(true)">重试</el-button>
        </section>

        <section v-else class="catalog-list-band">
          <div class="catalog-list-summary">
            <span>共 {{ page.total }} 份当前文档</span>
            <span>第 {{ pageNumber }} 页</span>
          </div>
          <div class="catalog-table-shell">
            <el-table :data="page.items" v-loading="page.loading" row-key="document_id">
              <template #empty>
                <div class="illustration-empty-wrapper">
                  <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                  <p>{{ hasAppliedFilters ? '当前筛选下没有文档' : '知识目录中暂无文档' }}</p>
                </div>
              </template>
              <el-table-column label="文档" min-width="270">
                <template #default="{row}">
                  <button type="button" class="catalog-document-link" @click="openDocument(row)">
                    <local-icon icon="mdi:file-document-outline"></local-icon>
                    <span class="catalog-document-copy">
                      <b :title="row.title">{{ row.title }}</b>
                      <small v-if="row.summary_excerpt">{{ row.summary_excerpt }}</small>
                    </span>
                  </button>
                </template>
              </el-table-column>
              <el-table-column label="分类" min-width="170">
                <template #default="{row}">
                  <div class="catalog-label-cell">
                    <el-tag size="small" effect="plain">{{ listClassification(row) }}</el-tag>
                    <span v-for="label in listSecondary(row).slice(0, 2)" :key="label.id"
                          class="catalog-secondary-label" :title="label.label">{{ label.label }}</span>
                    <span v-if="listSecondary(row).length > 2" class="catalog-more-count">
                      +{{ listSecondary(row).length - 2 }}
                    </span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态与能力" min-width="250">
                <template #default="{row}">
                  <div class="catalog-state-cell">
                    <el-tag :type="catalogOverallTag(row.overall_state)" size="small">
                      {{ catalogOverallLabel(row.overall_state) }}
                    </el-tag>
                    <span v-for="capability in rowCapabilities(row)" :key="capability.key"
                          class="catalog-capability-dot"
                          :class="'is-' + capability.state"
                          :title="capability.label + '：' + capabilityStateLabel(capability.state)">
                      {{ capability.label }}
                    </span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="图谱" width="120">
                <template #default="{row}">
                  <div class="catalog-count-cell">
                    <span>实体 {{ row.graph_counts.entities }}</span>
                    <span>关系 {{ row.graph_counts.relations }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="版本" width="90">
                <template #default="{row}">v{{ row.revision_no }}</template>
              </el-table-column>
              <el-table-column label="更新时间" width="164">
                <template #default="{row}">{{ formatCatalogTime(row.updated_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{row}">
                  <el-button link type="primary" @click="openDocument(row)">
                    查看<local-icon icon="mdi:chevron-right"></local-icon>
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="catalog-cursor-bar">
            <el-select v-model="pageSize" class="catalog-page-size" @change="changePageSize">
              <el-option label="每页 10 条" :value="10" />
              <el-option label="每页 20 条" :value="20" />
              <el-option label="每页 50 条" :value="50" />
            </el-select>
            <div class="catalog-cursor-actions">
              <el-button :disabled="!cursor.history.length || page.loading" @click="previousPage">
                <local-icon icon="mdi:chevron-left"></local-icon>上一页
              </el-button>
              <span>第 {{ pageNumber }} 页</span>
              <el-button :disabled="!page.next_cursor || page.loading" @click="nextPage">
                下一页<local-icon icon="mdi:chevron-right"></local-icon>
              </el-button>
            </div>
          </div>
        </section>
      </template>

      <template v-if="showingDetail && libraries.length">
        <section v-if="detail.loading" class="catalog-result-state catalog-detail-loading">
          <span>正在加载文档详情...</span>
        </section>
        <section v-else-if="detail.errorKind" class="catalog-result-state">
          <img :src="serviceError" alt="" aria-hidden="true" />
          <h3>{{ detail.errorMessage }}</h3>
          <div class="catalog-result-actions">
            <el-button @click="backToList">返回目录</el-button>
            <el-button v-if="detail.errorKind === 'error'" type="primary" @click="refreshCurrent">重试</el-button>
          </div>
        </section>
        <template v-else-if="detail.data">
          <section class="catalog-detail-identity">
            <div class="catalog-detail-title-row">
              <div>
                <el-tag :type="catalogOverallTag(detail.data.overall_state)">
                  {{ catalogOverallLabel(detail.data.overall_state) }}
                </el-tag>
                <span>当前 Revision v{{ detail.data.revision_no }}</span>
              </div>
              <el-button v-if="detail.data.file" type="primary" plain
                         :loading="fileLoadingId === String(detail.data.file.id)"
                         @click="openFileById(detail.data.file.id)">
                <local-icon icon="mdi:archive-arrow-down-outline"></local-icon>打开源文件
              </el-button>
            </div>
            <dl class="catalog-identity-grid">
              <div><dt>文档 ID</dt><dd :title="detail.data.document_id">{{ shortCatalogId(detail.data.document_id) }}</dd></div>
              <div><dt>Revision ID</dt><dd :title="detail.data.revision_id">{{ shortCatalogId(detail.data.revision_id) }}</dd></div>
              <div><dt>内容哈希</dt><dd :title="detail.data.revision_content_hash">{{ shortCatalogId(detail.data.revision_content_hash, 16) }}</dd></div>
              <div><dt>更新时间</dt><dd>{{ formatCatalogTime(detail.data.updated_at) }}</dd></div>
              <div v-if="detail.data.file"><dt>源文件</dt><dd :title="detail.data.file.file_name">{{ detail.data.file.file_name }}</dd></div>
              <div v-if="detail.data.file"><dt>文件大小</dt><dd>{{ formatCatalogBytes(detail.data.file.size_bytes) }}</dd></div>
            </dl>
          </section>

          <section class="catalog-detail-section">
            <div class="catalog-section-heading"><h3>知识能力</h3></div>
            <div class="catalog-capability-grid">
              <div v-for="capability in detailCapabilities" :key="capability.key"
                   class="catalog-capability-item">
                <span>{{ capability.label }}</span>
                <el-tag :type="capabilityStateTag(capability.state)" size="small">
                  {{ capabilityStateLabel(capability.state) }}
                </el-tag>
              </div>
            </div>
          </section>

          <section v-if="canManageProcessing" class="catalog-detail-section catalog-processing-section">
            <div class="catalog-section-heading">
              <h3>文档处理</h3>
              <span v-if="processing.loading">正在更新...</span>
            </div>
            <div v-if="processing.loading && !processing.data" class="catalog-processing-loading">
              正在加载处理状态...
            </div>
            <el-alert v-else-if="processing.errorKind" :title="processing.errorMessage"
                      type="warning" :closable="false" show-icon>
              <template #default>
                <el-button link type="primary" @click="loadProcessing(detail.data.document_id)">重试</el-button>
              </template>
            </el-alert>
            <template v-else-if="processing.data">
              <el-alert v-if="processing.retryError" class="catalog-processing-alert"
                        :title="processing.retryError" type="warning" :closable="false" show-icon />
              <div class="catalog-processing-list">
                <article v-for="stage in processing.data.stages" :key="stage.stage"
                         class="catalog-processing-row">
                  <span class="catalog-processing-marker"
                        :class="'is-' + stage.status" aria-hidden="true"></span>
                  <div class="catalog-processing-copy">
                    <div class="catalog-processing-title">
                      <strong>{{ processingStageLabel(stage.stage) }}</strong>
                      <el-tag :type="processingStatusTag(stage.status)" size="small">
                        {{ stage.availability === 'disabled' ? '未启用' : processingStatusLabel(stage.status) }}
                      </el-tag>
                    </div>
                    <div class="catalog-processing-meta">
                      <span v-if="stage.updated_at">更新 {{ formatCatalogTime(stage.updated_at) }}</span>
                      <span v-if="stage.retry_generation">重试代次 {{ stage.retry_generation }}</span>
                      <span v-if="stage.attempt_count !== null">尝试 {{ stage.attempt_count }} 次</span>
                      <span v-if="stage.graph_counts">
                        单元 {{ stage.graph_counts.succeeded }}/{{ stage.graph_counts.total }} 完成
                      </span>
                    </div>
                    <p v-if="stage.safe_error_code" class="catalog-processing-error">
                      {{ processingErrorLabel(stage.safe_error_code) }}
                    </p>
                  </div>
                  <el-button v-if="processingStageRetryable(stage)" type="primary" plain
                             :loading="processing.retryingStage === stage.stage"
                             :disabled="Boolean(processing.retryingStage)"
                             :title="'重试' + processingStageLabel(stage.stage)"
                             @click="retryProcessing(stage)">
                    <local-icon icon="status:retry"></local-icon>重试
                  </el-button>
                </article>
              </div>
            </template>
          </section>

          <section class="catalog-detail-section catalog-knowledge-grid">
            <div class="catalog-summary-panel">
              <div class="catalog-section-heading"><h3>摘要</h3></div>
              <p v-if="detail.data.summary" class="catalog-summary-text">{{ detail.data.summary.summary }}</p>
              <div v-else class="catalog-inline-empty">暂无摘要</div>
            </div>
            <div class="catalog-outline-panel">
              <div class="catalog-section-heading"><h3>大纲</h3></div>
              <ol v-if="detail.data.outline?.items?.length" class="catalog-outline-list">
                <li v-for="(item, index) in detail.data.outline.items" :key="index"
                    :class="'is-level-' + item.level" :title="item.path.join(' / ')">
                  {{ item.title }}
                </li>
              </ol>
              <div v-else class="catalog-inline-empty">暂无大纲</div>
            </div>
          </section>

          <section class="catalog-detail-section">
            <div class="catalog-section-heading">
              <h3>分类</h3>
              <el-tag size="small" effect="plain">
                {{ classificationStateLabel(detail.data.classification.state) }}
              </el-tag>
            </div>
            <div class="catalog-classification-line">
              <strong v-if="detailPrimary">{{ detailPrimary.label }}</strong>
              <span v-else>未设置主分类</span>
              <el-tag v-for="label in detailSecondary" :key="label.id" size="small" type="info">
                {{ label.label }}
              </el-tag>
            </div>
          </section>

          <section class="catalog-detail-section">
            <div class="catalog-section-heading">
              <h3>实体</h3>
              <span>{{ detail.data.graph.counts.entities }} 项</span>
            </div>
            <div v-if="detail.data.graph.entities.length" class="catalog-graph-grid">
              <article v-for="entity in detail.data.graph.entities" :key="entity.item_id"
                       class="catalog-graph-card">
                <div class="catalog-graph-card-head">
                  <local-icon icon="mdi:account-group-outline"></local-icon>
                  <div><strong :title="entity.canonical_name">{{ entity.canonical_name }}</strong><span>{{ entity.entity_type_label }}</span></div>
                </div>
                <div class="catalog-fact-meta">
                  <span>{{ catalogSourceTypeLabel(entity.source_type) }}</span>
                  <span>可信度 {{ formatCatalogConfidence(entity.confidence) }}</span>
                </div>
                <div class="catalog-evidence-actions">
                  <el-button v-for="(locator, index) in entity.evidence" :key="locator.evidence_id"
                             link type="primary" @click="openEvidence(locator)">
                    <local-icon icon="mdi:text-search"></local-icon>证据 {{ index + 1 }}
                  </el-button>
                </div>
              </article>
            </div>
            <div v-else class="catalog-inline-empty">当前文档没有已发布实体</div>
            <div v-if="detail.data.graph.entities_truncated" class="catalog-truncation-note">
              当前仅展示部分实体，共 {{ detail.data.graph.counts.entities }} 项
            </div>
          </section>

          <section class="catalog-detail-section">
            <div class="catalog-section-heading">
              <h3>关系</h3>
              <span>{{ detail.data.graph.counts.relations }} 项</span>
            </div>
            <div v-if="detail.data.graph.relations.length" class="catalog-graph-grid">
              <article v-for="relation in detail.data.graph.relations" :key="relation.item_id"
                       class="catalog-graph-card catalog-relation-card">
                <div class="catalog-relation-path">
                  <strong :title="relation.source_entity_name">{{ relation.source_entity_name }}</strong>
                  <span><local-icon icon="carbon:chart-relationship"></local-icon>{{ relation.relation_type_label }}</span>
                  <strong :title="relation.target_entity_name">{{ relation.target_entity_name }}</strong>
                </div>
                <div class="catalog-fact-meta">
                  <span>{{ catalogReviewLabel(relation.review_status) }}</span>
                  <span>{{ catalogSourceTypeLabel(relation.source_type) }}</span>
                  <span>可信度 {{ formatCatalogConfidence(relation.confidence) }}</span>
                </div>
                <div class="catalog-evidence-actions">
                  <el-button v-for="(locator, index) in relation.evidence" :key="locator.evidence_id"
                             link type="primary" @click="openEvidence(locator)">
                    <local-icon icon="mdi:text-search"></local-icon>证据 {{ index + 1 }}
                  </el-button>
                </div>
              </article>
            </div>
            <div v-else class="catalog-inline-empty">当前文档没有已发布关系</div>
            <div v-if="detail.data.graph.relations_truncated" class="catalog-truncation-note">
              当前仅展示部分关系，共 {{ detail.data.graph.counts.relations }} 项
            </div>
          </section>
        </template>
      </template>

      <el-drawer v-model="evidence.open" class="catalog-evidence-drawer" size="560px"
                 title="原文证据" @closed="closeEvidence">
        <div v-if="evidence.loading" class="catalog-evidence-loading">正在加载证据...</div>
        <section v-else-if="evidence.errorKind" class="catalog-result-state catalog-evidence-error">
          <h3>{{ evidence.errorMessage }}</h3>
        </section>
        <template v-else-if="evidence.data">
          <div class="catalog-evidence-meta">
            <span>{{ catalogPageLabel(evidence.data) }}</span>
            <span :title="catalogTitlePath(evidence.data)">{{ catalogTitlePath(evidence.data) }}</span>
            <span>关联 {{ evidence.data.fact_refs.length }} 个已发布事实</span>
          </div>
          <blockquote v-if="evidence.data.text_quote" class="catalog-evidence-quote">
            {{ evidence.data.text_quote }}
          </blockquote>
          <pre v-if="evidenceParts.length" class="catalog-evidence-window"><template v-for="(part, index) in evidenceParts" :key="index"><mark v-if="part.highlight">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></pre>
          <div class="catalog-evidence-identity">
            <span>Evidence</span><code :title="evidence.data.evidence_id">{{ shortCatalogId(evidence.data.evidence_id) }}</code>
            <span>Revision</span><code :title="evidence.data.document_revision_id">{{ shortCatalogId(evidence.data.document_revision_id) }}</code>
          </div>
          <div v-if="isCurrentFile(evidence.data.revision_file_id)"
               class="catalog-evidence-file-action">
            <el-button type="primary" plain
                       :loading="fileLoadingId === String(evidence.data.revision_file_id)"
                       @click="openFileById(evidence.data.revision_file_id)">
              <local-icon icon="mdi:archive-arrow-down-outline"></local-icon>打开证据源文件
            </el-button>
          </div>
        </template>
      </el-drawer>
    </div>
    `,
};
