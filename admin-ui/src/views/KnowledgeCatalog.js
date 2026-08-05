import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';

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
import {
    classificationReviewErrorKind,
    classificationReviewErrorMessage,
    formatReviewConfidence,
    initialReviewSelection,
    reviewCanAccept,
    reviewPageMatches,
    reviewProposalLabel,
    reviewReasonLabel,
    reviewRoleLabel,
    validateReviewSelection,
} from '../classification_review_ui.js';
import {
    documentStatusLabel,
    documentStatusTag,
    documentTypeIcon,
} from '../documents_ui.js';
import { canManageLibrary, readableLibraries, resolveSelectedSlug } from '../menu_access.js';
import { hasPermission, store } from '../store.js';

const EMPTY_FILTERS = {
    title: '',
    status: '',
    classificationState: '',
    labelId: '',
    dateRange: [],
};

function copyFilters(target, source) {
    target.title = String(source?.title || '').trim();
    target.status = String(source?.status || '');
    target.classificationState = String(source?.classificationState || '');
    target.labelId = String(source?.labelId || '');
    target.dateRange = Array.isArray(source?.dateRange) ? [...source.dateRange] : [];
}

function fixedErrorMessage(kind) {
    if (kind === 'forbidden') return '你没有读取该知识库目录的权限';
    if (kind === 'unavailable') return '知识暂未启用，或当前内容已不可用';
    return '知识加载失败，请稍后重试';
}

function fixedProcessingErrorMessage(kind) {
    if (kind === 'forbidden') return '你没有查看该文档处理详情的权限';
    if (kind === 'unavailable') return '文档处理诊断暂不可用';
    if (kind === 'conflict') return '文档或任务状态已变化，请刷新后重试';
    return '文档处理详情加载失败，请稍后重试';
}

function rowInDateRange(row, filters) {
    const [startText, endText] = Array.isArray(filters?.dateRange) ? filters.dateRange : [];
    const start = startText ? new Date(`${startText}T00:00:00`) : null;
    const end = endText ? new Date(`${endText}T23:59:59.999`) : null;
    if (!start && !end) return true;
    if ((start && Number.isNaN(start.getTime())) || (end && Number.isNaN(end.getTime()))) return false;
    const updated = new Date(row?.updated_at);
    if (Number.isNaN(updated.getTime())) return false;
    if (start && updated < start) return false;
    if (end && updated > end) return false;
    return true;
}

function hasFilterValues(filters) {
    return Boolean(
        filters.title
        || filters.status
        || filters.classificationState
        || filters.labelId
        || (Array.isArray(filters.dateRange) && filters.dateRange.length)
    );
}

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraries = computed(() => readableLibraries(store.permissions));
        const selectedSlug = ref(null);
        const filterDraft = reactive({ ...EMPTY_FILTERS });
        const appliedFilters = reactive({ ...EMPTY_FILTERS });
        const pageSize = ref(20);
        const stats = ref(null);
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
        const classificationEditor = reactive({
            labels: [],
            primaryLabelId: '',
            secondaryLabelIds: [],
            editing: false,
            loading: false,
            saving: false,
            error: '',
        });
        const classificationReview = reactive({
            run: null,
            taxonomyVersionId: '',
            loading: false,
            error: '',
            mutatingAction: '',
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
        const sourceReader = reactive({
            open: false,
            loading: false,
            row: null,
            data: null,
            error: '',
            keyword: '',
        });
        const dialog = reactive({
            open: false,
            mode: 'create',
            docId: null,
            row: null,
            form: { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' },
        });
        const fileLoadingId = ref('');

        let requestSeq = 0;
        let detailRequestSeq = 0;
        let evidenceRequestSeq = 0;
        let fileRequestSeq = 0;
        let processingRequestSeq = 0;
        let processingMutationSeq = 0;
        let classificationReviewSeq = 0;
        let routeReady = false;

        const documentId = computed(() => String(route.query.document || route.query.open || ''));
        const showingDetail = computed(() => Boolean(documentId.value));
        const pageNumber = computed(() => cursor.history.length + 1);
        const selectedLibrary = computed(() => (
            libraries.value.find((item) => item.slug === selectedSlug.value) || null
        ));
        const canInsert = computed(() => (
            Boolean(selectedSlug.value)
            && (store.user?.is_superuser || hasPermission(selectedSlug.value, 'insert'))
        ));
        const canDelete = computed(() => (
            Boolean(selectedSlug.value)
            && (store.user?.is_superuser || hasPermission(selectedSlug.value, 'delete'))
        ));
        const canManageProcessing = computed(() => canManageLibrary(
            store.permissions,
            store.organizations,
            selectedSlug.value,
        ));
        const processingCount = computed(() => (
            Number(stats.value?.pending_jobs || 0) + Number(stats.value?.processing_jobs || 0)
        ));
        const hasAppliedFilters = computed(() => hasFilterValues(appliedFilters));
        const hasDraftFilters = computed(() => hasFilterValues(filterDraft));
        const visibleItems = computed(() => (page.items || []).filter((row) => rowInDateRange(row, appliedFilters)));
        const processingIssues = computed(() => (processing.data?.stages || []).filter((stage) => (
            stage?.safe_error_code
            || processingStageRetryable(stage)
            || ['failed', 'partially_succeeded'].includes(stage?.status)
        )));
        const detailPrimary = computed(() => classificationPrimary(detail.data?.classification));
        const detailSecondary = computed(() => classificationSecondary(detail.data?.classification));
        const evidenceParts = computed(() => catalogEvidenceParts(evidence.data));
        const sourceText = computed(() => String(sourceReader.data?.normalized_text || ''));
        const sourceMatchCount = computed(() => {
            const keyword = sourceReader.keyword.trim().toLowerCase();
            if (!keyword) return 0;
            const text = sourceText.value.toLowerCase();
            let count = 0;
            let pos = 0;
            while (pos < text.length) {
                const idx = text.indexOf(keyword, pos);
                if (idx < 0) break;
                count += 1;
                pos = idx + keyword.length;
            }
            return count;
        });
        const highlightedSourceParts = computed(() => {
            const text = sourceText.value;
            const keyword = sourceReader.keyword.trim();
            if (!keyword) return [{ text, match: false }];
            const lowerText = text.toLowerCase();
            const lowerKeyword = keyword.toLowerCase();
            const parts = [];
            let pos = 0;
            while (pos < text.length) {
                const idx = lowerText.indexOf(lowerKeyword, pos);
                if (idx < 0) break;
                if (idx > pos) parts.push({ text: text.slice(pos, idx), match: false });
                parts.push({ text: text.slice(idx, idx + keyword.length), match: true });
                pos = idx + keyword.length;
            }
            if (pos < text.length) parts.push({ text: text.slice(pos), match: false });
            return parts.length ? parts : [{ text, match: false }];
        });
        const canAcceptClassificationReview = computed(() => reviewCanAccept(
            classificationReview.run,
            {
                taxonomy_version_id: classificationReview.taxonomyVersionId,
                available_labels: classificationEditor.labels,
            },
        ));

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
        let classificationMutationSeq = 0;

        function resetClassificationEditor(classification = null) {
            classificationMutationSeq += 1;
            classificationEditor.primaryLabelId = String(
                classificationPrimary(classification)?.id || '',
            );
            classificationEditor.secondaryLabelIds = classificationSecondary(classification)
                .map((item) => String(item.id));
            classificationEditor.editing = false;
            classificationEditor.loading = false;
            classificationEditor.saving = false;
            classificationEditor.error = '';
        }

        function clearClassificationReview() {
            classificationReviewSeq += 1;
            classificationReview.run = null;
            classificationReview.taxonomyVersionId = '';
            classificationReview.loading = false;
            classificationReview.error = '';
            classificationReview.mutatingAction = '';
        }

        async function loadCurrentClassificationReview(id) {
            if (!canManageProcessing.value || !selectedSlug.value || !id) return;
            const token = ++classificationReviewSeq;
            const slug = selectedSlug.value;
            classificationReview.loading = true;
            classificationReview.run = null;
            classificationReview.error = '';
            try {
                let offset = 0;
                let response;
                do {
                    response = await api.listClassificationReviews(slug, { limit: 100, offset });
                    if (token !== classificationReviewSeq || selectedSlug.value !== slug) return;
                    if (!reviewPageMatches(response, { limit: 100, offset })) {
                        classificationReview.error = classificationReviewErrorMessage('malformed');
                        return;
                    }
                    const run = response.items.find((item) => (
                        String(item.document_id) === String(id)
                        && String(item.document_revision_id) === String(detail.data?.revision_id || '')
                    ));
                    classificationReview.taxonomyVersionId = String(
                        response.taxonomy_version_id || '',
                    );
                    classificationEditor.labels = response.available_labels.map((label) => ({
                        ...label,
                        id: String(label.id),
                        label: String(label.label || label.key || label.id),
                    }));
                    if (run) {
                        classificationReview.run = run;
                        return;
                    }
                    offset += response.items.length;
                } while (response.items.length && offset < response.total);
                classificationReview.error = '没有找到当前文档的待审核记录，请刷新后重试';
            } catch (error) {
                if (token !== classificationReviewSeq) return;
                classificationReview.error = classificationReviewErrorMessage(
                    classificationReviewErrorKind(error),
                );
            } finally {
                if (token === classificationReviewSeq) classificationReview.loading = false;
            }
        }

        async function loadClassificationOptions() {
            if (!canManageProcessing.value || !selectedSlug.value) return;
            const token = ++classificationMutationSeq;
            const slug = selectedSlug.value;
            classificationEditor.loading = true;
            classificationEditor.error = '';
            try {
                const response = await api.listClassificationReviews(slug, {
                    limit: 1,
                    offset: 0,
                });
                if (token !== classificationMutationSeq || selectedSlug.value !== slug) return;
                classificationEditor.labels = Array.isArray(response?.available_labels)
                    ? response.available_labels.map((label) => ({
                        id: String(label.id),
                        label: String(label.label || label.key || label.id),
                    }))
                    : [];
            } catch (error) {
                if (token !== classificationMutationSeq) return;
                classificationEditor.error = error?.status === 403
                    ? '\u4f60\u6ca1\u6709\u4fee\u6539\u8be5\u77e5\u8bc6\u5e93\u5206\u7c7b\u7684\u6743\u9650'
                    : '\u5206\u7c7b\u9009\u9879\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5';
            } finally {
                if (token === classificationMutationSeq) classificationEditor.loading = false;
            }
        }

        async function startEditingClassification() {
            resetClassificationEditor(detail.data?.classification);
            classificationEditor.editing = true;
            if (!classificationEditor.labels.length) await loadClassificationOptions();
        }

        function startEditingClassificationReview() {
            const selection = initialReviewSelection(classificationReview.run, {
                available_labels: classificationEditor.labels,
            });
            classificationEditor.primaryLabelId = selection.primaryLabelId;
            classificationEditor.secondaryLabelIds = selection.secondaryLabelIds;
            classificationEditor.error = '';
            classificationEditor.editing = true;
        }

        async function submitClassificationReview(action) {
            const run = classificationReview.run;
            if (!run || classificationReview.mutatingAction || !selectedSlug.value) return;
            if (action === 'accept' && !canAcceptClassificationReview.value) {
                ElMessage.warning('模型建议包含无效分类，请调整后再确认');
                return;
            }
            if (action === 'change') {
                const validation = validateReviewSelection(
                    classificationEditor.primaryLabelId,
                    classificationEditor.secondaryLabelIds,
                    classificationEditor.labels,
                );
                if (validation) {
                    ElMessage.warning(validation);
                    return;
                }
            }
            if (run.status === 'blocked_manual' && action !== 'reject') {
                try {
                    await ElMessageBox.confirm(
                        '当前文档已有人工分类，继续后将替换现有结果。',
                        '确认替换分类',
                        { confirmButtonText: '继续审核', cancelButtonText: '取消', type: 'warning' },
                    );
                } catch {
                    return;
                }
            }
            const token = ++classificationReviewSeq;
            const slug = selectedSlug.value;
            const id = String(detail.data.document_id);
            classificationReview.mutatingAction = action;
            classificationReview.error = '';
            try {
                await api.reviewClassificationRun(slug, run.id, {
                    expected_run_status: run.status,
                    expected_effective_decision_set_id: run.effective_decision_set_id || null,
                    action,
                    primary_label_id: action === 'change'
                        ? classificationEditor.primaryLabelId : null,
                    secondary_label_ids: action === 'change'
                        ? classificationEditor.secondaryLabelIds : [],
                });
                if (token !== classificationReviewSeq || selectedSlug.value !== slug) return;
                ElMessage.success(action === 'reject' ? '分类建议已驳回' : '分类审核已确认');
                classificationEditor.editing = false;
                await loadDetail(id, true);
                await loadList(true);
            } catch (error) {
                if (token !== classificationReviewSeq) return;
                classificationReview.error = classificationReviewErrorMessage(
                    classificationReviewErrorKind(error),
                );
            } finally {
                if (token === classificationReviewSeq) {
                    classificationReview.mutatingAction = '';
                }
            }
        }

        async function saveClassification() {
            if (!selectedSlug.value || !detail.data || classificationEditor.saving) return;
            if (!classificationEditor.primaryLabelId) {
                classificationEditor.error = '\u8bf7\u9009\u62e9\u4e3b\u5206\u7c7b';
                return;
            }
            const token = ++classificationMutationSeq;
            const slug = selectedSlug.value;
            const id = String(detail.data.document_id);
            classificationEditor.saving = true;
            classificationEditor.error = '';
            try {
                await api.setDocumentClassification(slug, id, {
                    expected_effective_decision_set_id: (
                        detail.data.classification?.decision_set_id || null
                    ),
                    primary_label_id: classificationEditor.primaryLabelId,
                    secondary_label_ids: classificationEditor.secondaryLabelIds
                        .filter((labelId) => labelId !== classificationEditor.primaryLabelId)
                        .slice(0, 8),
                });
                if (token !== classificationMutationSeq || selectedSlug.value !== slug) return;
                ElMessage.success('\u5206\u7c7b\u5df2\u66f4\u65b0');
                await loadDetail(id, true);
                await loadList(true);
            } catch (error) {
                if (token !== classificationMutationSeq) return;
                classificationEditor.error = error?.status === 409
                    ? '\u5206\u7c7b\u5df2\u53d1\u751f\u53d8\u5316\uff0c\u8bf7\u5237\u65b0\u540e\u91cd\u65b0\u4fee\u6539'
                    : '\u5206\u7c7b\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5';
            } finally {
                if (token === classificationMutationSeq) classificationEditor.saving = false;
            }
        }

        function clearDetail() {
            detailRequestSeq += 1;
            fileRequestSeq += 1;
            fileLoadingId.value = '';
            detail.data = null;
            detail.loading = false;
            detail.errorKind = '';
            detail.errorMessage = '';
            resetClassificationEditor();
            clearClassificationReview();
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

        async function loadStats(forceRefresh = false) {
            if (!selectedSlug.value) {
                stats.value = null;
                return;
            }
            try {
                stats.value = await api.libraryStats(selectedSlug.value, forceRefresh);
            } catch (_) {
                stats.value = null;
            }
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
                stats.value = null;
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
                void loadStats(forceRefresh);
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
                resetClassificationEditor(response.classification);
                clearClassificationReview();
                if (canManageProcessing.value
                    && response.classification?.state === 'pending_review') {
                    void loadCurrentClassificationReview(id);
                } else if (canManageProcessing.value) {
                    void loadClassificationOptions();
                }
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
            const requestedSlug = String(route.query.library || route.query.slug || '');
            const requestedDocument = String(route.query.document || route.query.open || '');
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
                    query: requestedDocument
                        ? { library: nextSlug, document: requestedDocument }
                        : { library: nextSlug },
                });
                return;
            }
            const libraryChanged = selectedSlug.value !== nextSlug;
            if (libraryChanged) {
                selectedSlug.value = nextSlug;
                labelOptions.value = [];
                classificationEditor.labels = [];
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

        function documentIdOf(row) {
            return String(row?.document_id || row?.id || '');
        }

        function openFileImport() {
            if (!selectedSlug.value || !canInsert.value) return;
            router.push({
                path: '/knowledge-assets/import',
                query: { library: selectedSlug.value, mode: 'add' },
            });
        }

        function openIngest() {
            if (!selectedSlug.value || !canInsert.value) return;
            dialog.mode = 'create';
            dialog.docId = null;
            dialog.row = null;
            dialog.form = { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' };
            dialog.open = true;
        }

        function openEdit(row) {
            if (!selectedSlug.value || !canInsert.value) return;
            dialog.mode = 'edit';
            dialog.docId = documentIdOf(row);
            dialog.row = row;
            dialog.form = {
                title: row?.title || '',
                external_id: '',
                text: '',
                splitter: 'text',
                metadata_json: '',
            };
            dialog.open = true;
        }

        function openReplaceImport(row) {
            if (!selectedSlug.value || !canInsert.value) return;
            router.push({
                path: '/knowledge-assets/import',
                query: {
                    library: selectedSlug.value,
                    mode: 'replace',
                    replaceDocumentId: documentIdOf(row),
                    replaceTitle: row?.title || '',
                },
            });
        }

        async function submitIngest() {
            if (!selectedSlug.value) {
                ElMessage.warning('请先选择知识库');
                return;
            }
            if (!dialog.form.text.trim()) {
                ElMessage.warning('请输入正文');
                return;
            }
            let metadata = null;
            if (dialog.form.metadata_json.trim()) {
                try { metadata = JSON.parse(dialog.form.metadata_json); }
                catch (_) { ElMessage.error('metadata 不是合法 JSON'); return; }
            }
            try {
                const body = {
                    title: dialog.form.title || null,
                    external_id: dialog.mode === 'edit' ? null : (dialog.form.external_id || null),
                    text: dialog.form.text,
                    splitter: dialog.form.splitter,
                    metadata,
                };
                const resp = dialog.mode === 'edit'
                    ? await api.updateDocument(selectedSlug.value, dialog.docId, body)
                    : await api.ingestDocument(selectedSlug.value, body);
                ElMessage.success(dialog.mode === 'edit'
                    ? `已覆盖正文并重新入队 ${resp.chunk_count} 个分片`
                    : `已入队 ${resp.chunk_count} 个分片`);
                dialog.open = false;
                if (dialog.mode === 'edit' && documentId.value === dialog.docId) {
                    await loadDetail(dialog.docId, true);
                }
                await loadList(true, true);
            } catch (e) { ElMessage.error(e.message || String(e)); }
        }

        async function deleteDocument(row) {
            if (!selectedSlug.value || !row) return;
            if (!canDelete.value) {
                ElMessage.warning('没有删除权限');
                return;
            }
            const id = documentIdOf(row);
            try {
                await ElMessageBox.confirm(
                    `确认删除文档 "${row.title || id}"？\n\n删除后文档将不可用于后续检索；向量清理为异步执行。`,
                    '删除文档',
                    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
                );
                await api.deleteDocument(selectedSlug.value, id);
                ElMessage.success('已删除，向量清理将异步完成');
                if (documentId.value === id) await backToList();
                await loadList(true, true);
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        function handleRowCommand(command, row) {
            if (command === 'edit') openEdit(row);
            else if (command === 'replace') openReplaceImport(row);
            else if (command === 'delete') void deleteDocument(row);
        }

        async function openFullSource(row) {
            if (!selectedSlug.value || !row) return;
            sourceReader.open = true;
            sourceReader.loading = true;
            sourceReader.row = row;
            sourceReader.data = null;
            sourceReader.error = '';
            sourceReader.keyword = '';
            try {
                sourceReader.data = await api.getDocumentFullSource(selectedSlug.value, documentIdOf(row));
            } catch (e) {
                sourceReader.error = e.status === 404
                    ? '该文档缺少原文快照，请重新导入后再阅读。'
                    : (e.message || '加载原文失败');
            } finally {
                sourceReader.loading = false;
            }
        }

        async function downloadOriginalFile(row) {
            if (!selectedSlug.value || !row) return;
            try {
                const { blob, filename } = await api.downloadDocumentFile(selectedSlug.value, documentIdOf(row));
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement('a');
                anchor.href = url;
                anchor.download = filename || row.title || 'document-file';
                document.body.appendChild(anchor);
                anchor.click();
                anchor.remove();
                URL.revokeObjectURL(url);
            } catch (e) {
                ElMessage.error(e.status === 404
                    ? '该文档缺少原始文件快照，无法下载。'
                    : (e.message || '下载原文件失败'));
            }
        }

        function closeFullSource() {
            sourceReader.open = false;
            sourceReader.loading = false;
            sourceReader.data = null;
            sourceReader.error = '';
            sourceReader.keyword = '';
        }

        async function applyFilters() {
            const serverChanged = (
                appliedFilters.title !== String(filterDraft.title || '').trim()
                || appliedFilters.status !== String(filterDraft.status || '')
                || appliedFilters.classificationState !== String(filterDraft.classificationState || '')
                || appliedFilters.labelId !== String(filterDraft.labelId || '')
            );
            copyFilters(appliedFilters, filterDraft);
            if (serverChanged) await loadList(false, true);
        }

        async function resetFilters() {
            const serverChanged = Boolean(
                appliedFilters.title
                || appliedFilters.status
                || appliedFilters.classificationState
                || appliedFilters.labelId
            );
            copyFilters(filterDraft, EMPTY_FILTERS);
            copyFilters(appliedFilters, EMPTY_FILTERS);
            labelOptions.value = [];
            if (serverChanged) await loadList(false, true);
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
            () => [route.query.library, route.query.slug, route.query.document, route.query.open],
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
            stats, processingCount, canInsert, canDelete,
            filterDraft, appliedFilters, hasAppliedFilters, hasDraftFilters, labelOptions,
            pageSize, page, pageNumber, cursor, visibleItems,
            showingDetail, detail, detailPrimary, detailSecondary,
            sourceReader, sourceText, sourceMatchCount, highlightedSourceParts, dialog,
            evidence, evidenceParts, fileLoadingId,
            canManageProcessing, processing, processingIssues,
            classificationEditor, classificationReview, canAcceptClassificationReview,
            selectLibrary, applyFilters, resetFilters, loadList,
            nextPage, previousPage, changePageSize,
            openDocument, backToList, refreshCurrent, openEvidence, closeEvidence,
            openFileImport, openIngest, openEdit, openReplaceImport, submitIngest,
            deleteDocument, handleRowCommand,
            openFullSource, downloadOriginalFile, closeFullSource,
            loadProcessing, retryProcessing,
            startEditingClassification, resetClassificationEditor, saveClassification,
            startEditingClassificationReview, submitClassificationReview,
            isCurrentFile, openFileById,
            listClassification, listSecondary, rowCapabilities,
            catalogOverallLabel, catalogOverallTag,
            capabilityStateLabel, capabilityStateTag,
            documentStatusLabel, documentStatusTag, documentTypeIcon,
            classificationStateLabel, formatCatalogConfidence, formatCatalogTime,
            formatCatalogBytes, catalogSourceTypeLabel, catalogReviewLabel,
            catalogPageLabel, catalogTitlePath, shortCatalogId,
            processingStageLabel, processingStageRetryable,
            processingStatusLabel, processingStatusTag, processingErrorLabel,
            reviewProposalLabel, reviewReasonLabel, reviewRoleLabel, formatReviewConfidence,
            dataEmpty, serviceError,
        };
    },
    template: `
    <div class="catalog-workspace">
      <header class="catalog-page-header">
        <div class="catalog-page-title">
          <el-button v-if="showingDetail" class="catalog-back-button" text title="返回目录"
                     aria-label="返回目录" @click="backToList">
            <local-icon icon="mdi:chevron-left"></local-icon>
          </el-button>
          <div>
            <h2>{{ showingDetail ? (detail.data?.title || '文档详情') : '知识资产' }}</h2>
            <div class="catalog-page-context">
              {{ selectedLibrary ? selectedLibrary.name : '未选择知识库' }}
              <template v-if="showingDetail && detail.data"> · v{{ detail.data.revision_no }}</template>
            </div>
          </div>
        </div>
        <div class="catalog-page-actions">
          <el-button v-if="!showingDetail" :disabled="!canInsert" @click="openFileImport">文件导入</el-button>
          <el-button v-if="!showingDetail" type="primary" :disabled="!canInsert" @click="openIngest">提交文本</el-button>
          <el-select :model-value="selectedSlug" class="catalog-library-select"
                     placeholder="选择知识库" @change="selectLibrary">
            <el-option v-for="library in libraries" :key="library.slug"
                       :label="library.name" :value="library.slug" />
          </el-select>
          <el-button class="app-refresh-button" :loading="page.loading || detail.loading" title="刷新当前内容"
                     aria-label="刷新当前内容" @click="refreshCurrent">
            <span class="app-refresh-icon" aria-hidden="true"></span><span>刷新</span>
          </el-button>
        </div>
      </header>

      <el-alert v-if="!libraries.length" class="catalog-scope-alert"
                title="当前账号没有可读取的知识库" type="info" :closable="false" show-icon />

      <template v-if="!showingDetail && libraries.length">
        <section class="catalog-asset-stats">
          <div><span>文档总数</span><b>{{ stats?.document_count || page.total || 0 }}</b></div>
          <div><span>处理中</span><b>{{ processingCount }}</b></div>
          <div><span>已完成</span><b>{{ stats?.done_jobs || 0 }}</b></div>
          <div><span>失败</span><b class="catalog-stat-danger">{{ stats?.failed_jobs || 0 }}</b></div>
        </section>
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
          <el-date-picker v-model="filterDraft.dateRange" type="daterange" value-format="YYYY-MM-DD"
                          start-placeholder="开始日期" end-placeholder="结束日期" />
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
            <el-table :data="visibleItems" v-loading="page.loading" row-key="document_id">
              <template #empty>
                <div class="illustration-empty-wrapper">
                  <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                  <p>{{ hasAppliedFilters ? '当前筛选下没有文档' : '当前知识中暂无文档' }}</p>
                </div>
              </template>
              <el-table-column label="文档" min-width="270">
                <template #default="{row}">
                  <button type="button" class="catalog-document-link" @click="openDocument(row)">
                    <img v-if="documentTypeIcon(row)" class="catalog-document-icon"
                         :src="documentTypeIcon(row)" alt="" aria-hidden="true" />
                    <local-icon v-else class="catalog-document-icon" icon="mdi:file-document-outline"></local-icon>
                    <span class="catalog-document-copy">
                      <b :title="row.title">{{ row.title }}</b>
                      <small v-if="row.summary_excerpt" class="catalog-document-summary">{{ row.summary_excerpt }}</small>
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
                    <el-tag :type="documentStatusTag(row.document_status)" size="small">
                      {{ documentStatusLabel(row.document_status) }}
                    </el-tag>
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
              <el-table-column label="版本/更新时间" width="170">
                <template #default="{row}">
                  <div class="catalog-version-cell">
                    <b>v{{ row.revision_no }}</b>
                    <span>{{ formatCatalogTime(row.updated_at) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="150" fixed="right">
                <template #default="{row}">
                  <div class="catalog-row-actions">
                    <el-button link :type="row.classification?.state === 'pending_review'
                                 && canManageProcessing ? 'warning' : 'primary'"
                               @click="openDocument(row)">
                      {{ row.classification?.state === 'pending_review' && canManageProcessing ? '审核' : '详情' }}
                    </el-button>
                    <el-dropdown trigger="click" @command="(command) => handleRowCommand(command, row)">
                      <el-button link>更多<local-icon icon="mdi:chevron-down"></local-icon></el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item command="edit" :disabled="!canInsert">编辑</el-dropdown-item>
                          <el-dropdown-item command="replace" :disabled="!canInsert">替换导入</el-dropdown-item>
                          <el-dropdown-item command="delete" :disabled="!canDelete" divided>删除</el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                  </div>
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
              <div class="catalog-detail-actions">
                <el-button plain @click="openFullSource(detail.data)">阅读原文</el-button>
                <el-button plain :disabled="!canInsert" @click="openEdit(detail.data)">编辑</el-button>
                <el-button plain :disabled="!canInsert" @click="openReplaceImport(detail.data)">替换导入</el-button>
                <el-button type="danger" plain :disabled="!canDelete" @click="deleteDocument(detail.data)">删除</el-button>
              </div>
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

          <section v-if="canManageProcessing && (processing.errorKind || processing.retryError || processingIssues.length)"
                   class="catalog-detail-section catalog-processing-section">
            <div class="catalog-section-heading"><h3>处理异常</h3></div>
            <el-alert v-if="processing.errorKind" :title="processing.errorMessage"
                      type="warning" :closable="false" show-icon>
              <template #default>
                <el-button link type="primary" @click="loadProcessing(detail.data.document_id)">重试</el-button>
              </template>
            </el-alert>
            <template v-else>
              <el-alert v-if="processing.retryError" class="catalog-processing-alert"
                        :title="processing.retryError" type="warning" :closable="false" show-icon />
              <div class="catalog-processing-list">
                <article v-for="stage in processingIssues" :key="stage.stage"
                         class="catalog-processing-row">
                  <span class="catalog-processing-marker"
                        :class="'is-' + stage.status" aria-hidden="true"></span>
                  <div class="catalog-processing-copy">
                    <div class="catalog-processing-title">
                      <strong>{{ processingStageLabel(stage.stage) }}</strong>
                      <el-tag :type="processingStatusTag(stage.status)" size="small">
                        {{ processingStatusLabel(stage.status) }}
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
              <el-button v-if="canManageProcessing && !classificationEditor.editing
                               && detail.data.classification.state !== 'pending_review'"
                         link type="primary" @click="startEditingClassification">
                &#x4FEE;&#x6539;&#x5206;&#x7C7B;
              </el-button>
            </div>
            <div class="catalog-classification-line">
              <strong v-if="detailPrimary">{{ detailPrimary.label }}</strong>
              <span v-else>未设置主分类</span>
              <el-tag v-for="label in detailSecondary" :key="label.id" size="small" type="info">
                {{ label.label }}
              </el-tag>
            </div>
            <div v-if="detail.data.classification.state === 'pending_review' && canManageProcessing"
                 class="catalog-classification-review">
              <div v-if="classificationReview.loading" class="catalog-inline-empty">
                正在加载模型分类建议...
              </div>
              <el-alert v-else-if="classificationReview.error" :title="classificationReview.error"
                        type="warning" :closable="false" show-icon />
              <template v-else-if="classificationReview.run">
                <div class="classification-review-proposals">
                  <article v-for="proposal in classificationReview.run.proposals"
                           :key="proposal.id || proposal.rank"
                           class="classification-review-proposal">
                    <span class="classification-review-proposal-role">
                      {{ reviewRoleLabel(proposal.role) }}
                    </span>
                    <div>
                      <strong>{{ reviewProposalLabel(proposal) }}</strong>
                    </div>
                    <b>{{ formatReviewConfidence(proposal.confidence_micros) }}</b>
                    <div v-if="proposal.reason_codes?.length" class="classification-review-reasons">
                      <span v-for="reason in proposal.reason_codes" :key="reason">
                        {{ reviewReasonLabel(reason) }}
                      </span>
                    </div>
                  </article>
                </div>
                <div v-if="!classificationEditor.editing" class="catalog-classification-actions">
                  <el-button type="primary" :disabled="!canAcceptClassificationReview"
                             :loading="classificationReview.mutatingAction === 'accept'"
                             @click="submitClassificationReview('accept')">确认建议</el-button>
                  <el-button :disabled="Boolean(classificationReview.mutatingAction)"
                             @click="startEditingClassificationReview">调整后确认</el-button>
                  <el-button type="danger" plain
                             :loading="classificationReview.mutatingAction === 'reject'"
                             :disabled="Boolean(classificationReview.mutatingAction)"
                             @click="submitClassificationReview('reject')">驳回</el-button>
                </div>
              </template>
            </div>
            <div v-if="classificationEditor.editing" class="catalog-classification-editor">
              <el-alert v-if="classificationEditor.error" :title="classificationEditor.error"
                        type="warning" :closable="false" show-icon />
              <div v-if="classificationEditor.loading" class="catalog-inline-empty">
                &#x6B63;&#x5728;&#x52A0;&#x8F7D;&#x5206;&#x7C7B;&#x9009;&#x9879;...
              </div>
              <el-alert v-else-if="!classificationEditor.labels.length"
                        title="&#x5F53;&#x524D;&#x77E5;&#x8BC6;&#x5E93;&#x6CA1;&#x6709;&#x53EF;&#x7528;&#x5206;&#x7C7B;"
                        type="info" :closable="false" show-icon />
              <template v-else>
                <label>
                  <span>&#x4E3B;&#x5206;&#x7C7B;</span>
                  <el-select v-model="classificationEditor.primaryLabelId"
                             placeholder="&#x8BF7;&#x9009;&#x62E9;&#x4E3B;&#x5206;&#x7C7B;">
                    <el-option v-for="label in classificationEditor.labels" :key="label.id"
                               :label="label.label" :value="label.id" />
                  </el-select>
                </label>
                <label>
                  <span>&#x9644;&#x52A0;&#x5206;&#x7C7B;</span>
                  <el-select v-model="classificationEditor.secondaryLabelIds" multiple
                             collapse-tags collapse-tags-tooltip :max-collapse-tags="3"
                             :multiple-limit="8"
                             placeholder="&#x6700;&#x591A;&#x9009;&#x62E9; 8 &#x9879;">
                    <el-option v-for="label in classificationEditor.labels" :key="label.id"
                               :label="label.label" :value="label.id"
                               :disabled="label.id === classificationEditor.primaryLabelId" />
                  </el-select>
                </label>
                <div class="catalog-classification-actions">
                  <el-button type="primary"
                             :loading="classificationEditor.saving
                               || classificationReview.mutatingAction === 'change'"
                             :disabled="Boolean(classificationReview.mutatingAction)"
                             @click="detail.data.classification.state === 'pending_review'
                               ? submitClassificationReview('change') : saveClassification()">
                    {{ detail.data.classification.state === 'pending_review' ? '确认调整' : '保存' }}
                  </el-button>
                  <el-button :disabled="classificationEditor.saving
                               || Boolean(classificationReview.mutatingAction)"
                             @click="resetClassificationEditor(detail.data.classification)">&#x53D6;&#x6D88;</el-button>
                </div>
              </template>
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

      <el-dialog v-model="sourceReader.open" title="阅读原文" width="860px" class="documents-source-dialog" @closed="closeFullSource">
        <div v-if="sourceReader.loading" class="documents-source-loading">正在加载原文...</div>
        <template v-else>
          <el-alert v-if="sourceReader.error" type="warning" :closable="false" show-icon :title="sourceReader.error" />
          <template v-else-if="sourceReader.data">
            <div class="documents-source-meta">
              <span>{{ sourceReader.data.file_name || sourceReader.data.document_title || sourceReader.row?.title || '文档' }}</span>
              <span v-if="sourceReader.data.file_type">{{ sourceReader.data.file_type }}</span>
              <span>v{{ sourceReader.data.revision || sourceReader.row?.revision_no || 0 }}</span>
              <span>{{ sourceReader.data.text_length || sourceText.length }} 字</span>
            </div>
            <div class="documents-source-toolbar">
              <el-input v-model="sourceReader.keyword" clearable placeholder="搜索原文关键词" />
              <span class="documents-source-match-count">{{ sourceReader.keyword ? ('匹配 ' + sourceMatchCount + ' 处') : '输入关键词后高亮匹配' }}</span>
            </div>
            <el-empty v-if="!sourceText" description="原文快照为空" />
            <pre v-else class="documents-source-text"><template v-for="(part, pi) in highlightedSourceParts" :key="pi"><mark v-if="part.match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></pre>
          </template>
          <el-empty v-else description="暂无原文内容" />
        </template>
      </el-dialog>

      <el-dialog v-model="dialog.open" :title="dialog.mode === 'edit' ? '编辑文档' : (selectedSlug ? ('向 ' + selectedSlug + ' 提交文档') : '提交文档')" width="720px" class="documents-edit-dialog">
        <template v-if="dialog.mode === 'edit'">
          <el-alert type="warning" :closable="false" class="documents-edit-alert"
                    title="当前保存是文本覆盖：会替换正文、重新切分、重新向量化，旧问答引用不会自动更新。" />
        </template>
        <el-form label-width="110px">
          <section class="documents-edit-section">
            <h3>{{ dialog.mode === 'edit' ? '基础信息编辑' : '基础信息' }}</h3>
            <p v-if="dialog.mode === 'edit'" class="documents-edit-hint">标题和元数据会随下方文本覆盖一起提交；外部文档编号当前版本不可修改。</p>
            <el-form-item label="标题"><el-input v-model="dialog.form.title" /></el-form-item>
            <el-form-item label="外部文档编号">
              <el-input v-model="dialog.form.external_id" :disabled="dialog.mode === 'edit'" placeholder="可选；用于与外部业务系统建立对应关系" />
              <div v-if="dialog.mode === 'edit'" class="documents-form-help">当前不支持在文档页单独修改外部文档编号。</div>
            </el-form-item>
            <el-form-item label="metadata">
              <el-input v-model="dialog.form.metadata_json" type="textarea" :rows="3"
                        placeholder='可选 JSON，例如 {"author":"...","year":2024}' />
            </el-form-item>
          </section>

          <section class="documents-edit-section documents-content-overwrite">
            <h3>{{ dialog.mode === 'edit' ? '覆盖文档内容（文本覆盖）' : '正文' }}</h3>
            <p v-if="dialog.mode === 'edit'" class="documents-edit-hint">此操作会用下面的完整正文替换旧正文，重新切分并重新向量化。</p>
            <el-form-item label="切分方式">
              <el-radio-group v-model="dialog.form.splitter">
                <el-radio value="text">text</el-radio>
                <el-radio value="markdown">markdown</el-radio>
                <el-radio value="none">none（单一切片）</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="正文" required>
              <el-input v-model="dialog.form.text" type="textarea" :rows="10"
                        placeholder="粘贴完整文本 / Markdown / JSON 字符串" />
            </el-form-item>
          </section>

          <section v-if="dialog.mode === 'edit'" class="documents-edit-section documents-reimport-zone">
            <h3>覆盖导入新文件</h3>
            <p>如果需要用 PDF、Word、Excel 等文件覆盖，请使用导入页的替换模式。</p>
            <el-button type="warning" plain @click="openReplaceImport(dialog.row)">重新导入并覆盖此文档</el-button>
          </section>
        </el-form>
        <template #footer>
          <el-button @click="dialog.open = false">取消</el-button>
          <el-button type="primary" @click="submitIngest">{{ dialog.mode === 'edit' ? '保存文本覆盖并重新向量化' : '提交（异步 embed）' }}</el-button>
        </template>
      </el-dialog>

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
