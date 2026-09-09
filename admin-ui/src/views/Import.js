import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';
import { canManageLibrary } from '../menu_access.js';
import { resolveImportEntry } from '../import_query.js';
import { humanizeError } from './import_errors.js';
import {
    ST_LABEL, ST_TAG, OP_LABEL, OP_TAG,
    createBatchValidationState, validateBatchChunk, fileKey, formatSize, fileTypeIcon,
    MAX_BATCH_SIZE, validateFile, graphJobProgress, graphProgressDetail,
    securityLevelLabel, BUILD_MODE_LABEL, buildModeLabel,
} from '../import_ui.js';
import {
    createImportBatchIds,
    createImportSessionForFile,
    DEFAULT_IMPORT_CONFIGURATION,
    IMPORT_PROFILE_DAILY,
    IMPORT_PROFILE_INITIAL,
    importDisplayStatus,
    importStageLabel,
    importStageProgress,
    nextImportProgressBatch,
    runConcurrent,
    supportedExtensionsAccept,
    supportedExtensionsLabel,
    uploadFileInChunks,
} from '../folder_import.js';
import {
    BATCH_REPLACE_MATCH_LABEL, BATCH_REPLACE_MATCH_TAG,
    BATCH_REPLACE_STATUS_LABEL, BATCH_REPLACE_STATUS_TAG,
    createBatchReplaceItems, setBatchReplaceTarget,
    submittableBatchReplaceItems, submitBatchReplaceItems,
} from '../batch_replace.js';
import { uploadEmpty } from '../illustrations.js';

const GRAPH_CONFIG_REASON = {
    runtime_disabled: '图谱抽取服务未开启',
    provider_unconfigured: '抽取模型未配置',
    library_disabled: '当前知识库未开启图谱抽取',
    external_model_disabled: '当前知识库未允许外部模型',
    security_levels_missing: '当前知识库未配置允许的安全级别',
    active_ontology_missing: '当前知识库没有生效中的知识结构（Schema）',
};
const SCHEMA_MODE_LABEL = { disabled: '普通上传', explore: 'AI 探索', governed: 'Schema 治理' };
const IMPORT_PROGRESS_POLL_DELAYS = [2000, 5000, 10000, 30000];
const FILE_SELECTION_CHUNK_SIZE = 500;

function yieldToBrowser() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

function schemaModeLabel(config) {
    if (config?.exploration_available) return 'AI 探索';
    return SCHEMA_MODE_LABEL[config?.schema_mode] || SCHEMA_MODE_LABEL.disabled;
}

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libs = ref([]);
        const slug = ref(null);
        const mode = ref('add');
        const fileInput = ref(null);
        const folderInput = ref(null);
        const batchReplaceFileInput = ref(null);
        const externalId = ref('');
        const replaceDocId = ref(null);
        const replaceFile = ref(null);
        const replaceFileError = ref('');
        const docs = ref([]);
        const docsLoading = ref(false);
        const loading = ref(false);
        const importResult = ref(null);
        const docQuery = ref('');
        const queue = ref([]);
        const queuePage = ref(1);
        const queuePageSize = 100;
        const uploading = ref(false);
        const addingFiles = ref(false);
        const stats = ref(null);
        const dragOver = ref(false);
        const showExtId = ref(false);
        const routeReplaceDocumentId = ref('');
        const routeReplaceTitle = ref('');
        const routeReplaceError = ref('');
        const applyingRouteReplace = ref(false);
        const batchReplaceItems = ref([]);
        const batchReplacing = ref(false);
        const graphExtractionRequested = ref(false);
        const graphExtractionConfig = ref(null);
        const graphExtractionConfigLoading = ref(false);
        const graphExtractionConfigError = ref('');
        const graphExtractionSecurityLevel = ref('');
        const importConfiguration = ref(DEFAULT_IMPORT_CONFIGURATION);
        const importConfigurationLoading = ref(false);
        const importConfigurationDialogVisible = ref(false);
        const importConfigurationSaving = ref(false);
        const importConfigurationProfile = ref('daily');
        const importConfigurationForm = ref({ max_file_mib: 500, max_files_per_selection: 1000 });
        let graphConfigRequestSeq = 0;
        let graphProgressRequestSeq = 0;
        let graphProgressTimer = null;
        let importJobsTimer = null;
        let importJobsPollCursor = 0;
        let importJobsPollController = null;
        let importJobsPollPromise = null;
        let importJobsPollSequence = 0;
        let importJobsPollDelayIndex = 0;
        let activeUploadController = null;

        // ── Computed ─────────────────────────────────────────
        const displayDocs = computed(() => {
            const q = docQuery.value.toLowerCase();
            if (!q) return docs.value;
            return docs.value.filter((d) =>
                (d.title || '').toLowerCase().includes(q) ||
                (d.external_id || '').toLowerCase().includes(q) ||
                (d.id || '').toLowerCase().includes(q)
            );
        });

        const selectedReplaceDoc = computed(() =>
            docs.value.find((d) => String(d.id) === String(replaceDocId.value)) || null
        );
        const routeReplaceActive = computed(() =>
            mode.value === 'replace' && Boolean(routeReplaceDocumentId.value)
        );
        const replaceTargetTitle = computed(() =>
            selectedReplaceDoc.value?.title || routeReplaceTitle.value || replaceDocId.value || ''
        );

        const extIdSet = computed(() => Boolean(externalId.value.trim()));
        const multiBlockedByExtId = computed(() =>
            mode.value === 'add' && queue.value.length > 1 && extIdSet.value
        );
        const canConfigureImport = computed(() => Boolean(
            store.user?.is_superuser
            || canManageLibrary(store.permissions, store.organizations, slug.value)
        ));
        const importAccept = computed(() =>
            supportedExtensionsAccept(importConfiguration.value)
        );
        const importFormatLabel = computed(() =>
            supportedExtensionsLabel(importConfiguration.value)
        );

        const pendingCount = computed(() =>
            queue.value.filter((it) => it.status === 'pending').length
        );
        const displayQueue = computed(() => {
            const start = (queuePage.value - 1) * queuePageSize;
            return queue.value.slice(start, start + queuePageSize);
        });
        const submittedCount = computed(() =>
            queue.value.filter((it) => it.status === 'submitted').length
        );
        const skippedCount = computed(() =>
            queue.value.filter((it) => it.status === 'skipped').length
        );
        const failedCount = computed(() =>
            queue.value.filter((it) => it.status === 'failed').length
        );
        const invalidCount = computed(() =>
            queue.value.filter((it) => it.status === 'invalid').length
        );
        const hasFailed = computed(() => failedCount.value > 0);
        const graphExplorationMode = computed(() =>
            graphExtractionConfig.value?.schema_mode === 'explore'
        );
        const formalGraphExtractionRequested = computed(() =>
            graphExtractionRequested.value &&
            graphExtractionConfig.value?.schema_mode === 'governed' &&
            graphExtractionConfig.value?.available === true
        );
        const graphJobRequested = computed(() =>
            graphExtractionRequested.value &&
            (
                graphExtractionConfig.value?.available === true ||
                graphExtractionConfig.value?.exploration_available === true
            )
        );
        const graphExtractionReady = computed(() => (
            !graphExtractionRequested.value || (
                graphExplorationMode.value
                    ? graphExtractionConfig.value?.exploration_available === true &&
                        graphExtractionConfig.value.allowed_security_levels.includes(
                            graphExtractionSecurityLevel.value,
                        )
                    : graphExtractionConfig.value?.available === true &&
                        graphExtractionConfig.value.allowed_security_levels.includes(
                            graphExtractionSecurityLevel.value,
                        )
            )
        ));
        const graphExtractionStatus = computed(() => {
            if (!graphExtractionRequested.value) return '';
            if (graphExtractionConfigLoading.value) return '正在检查当前知识库配置...';
            if (graphExtractionConfigError.value) return graphExtractionConfigError.value;
            if (graphExplorationMode.value && graphExtractionConfig.value?.exploration_available) {
                return 'AI 自动发现：将根据首次文件自动生成候选知识结构，随后按本次任务冻结的 Schema 抽取';
            }
            if (!graphExtractionConfig.value?.available) {
                const reasons = (graphExtractionConfig.value?.reasons || [])
                    .filter((reason) => !(
                        graphExtractionConfig.value?.schema_mode === 'explore'
                        && reason === 'active_ontology_missing'
                    ));
                if (!reasons.length && graphExtractionConfig.value?.schema_mode === 'governed') {
                    return '严格 Schema 模式需要已确认并生效的 Schema，当前不可上传图谱文件';
                }
                return reasons
                    .map((reason) => GRAPH_CONFIG_REASON[reason] || '图谱抽取配置不可用')
                    .join('；');
            }
            return `配置已确认：将按${buildModeLabel(graphExtractionConfig.value.default_build_mode)}模式抽取，系统验证合格后自动发布`;
        });

        const canStart = computed(() =>
            mode.value === 'add' && slug.value && pendingCount.value > 0 &&
            !uploading.value && !addingFiles.value && !multiBlockedByExtId.value && graphExtractionReady.value &&
            !graphExtractionConfigLoading.value
        );

        const canReplace = computed(() =>
            mode.value === 'replace' && slug.value && replaceDocId.value &&
            replaceFile.value && !replaceFileError.value && !loading.value &&
            !docsLoading.value && !routeReplaceError.value && !batchReplacing.value &&
            graphExtractionReady.value && !graphExtractionConfigLoading.value
        );

        const batchReplaceReadyItems = computed(() =>
            submittableBatchReplaceItems(batchReplaceItems.value)
        );
        const canBatchReplace = computed(() =>
            mode.value === 'replace' && slug.value && batchReplaceReadyItems.value.length > 0 &&
            !batchReplacing.value && !loading.value && !docsLoading.value &&
            graphExtractionReady.value && !graphExtractionConfigLoading.value
        );

        // ── Library loading ──────────────────────────────────
        async function loadLibs() {
            try {
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = (store.permissions || [])
                        .filter((p) => (p.actions || []).includes('insert'))
                        .map((p) => ({ slug: p.library_slug, name: p.library_name || p.library_slug }));
                }
                const entry = resolveImportEntry(route.query, libs.value, slug.value);
                slug.value = entry.slug;
                mode.value = entry.mode;
                routeReplaceDocumentId.value = entry.replaceDocumentId || '';
                routeReplaceTitle.value = entry.replaceTitle || '';
                if (entry.mode === 'replace' && entry.replaceDocumentId) {
                    replaceDocId.value = entry.replaceDocumentId;
                    applyingRouteReplace.value = true;
                }
            } catch (e) { ElMessage.error(e.message); }
        }

        async function loadDocs() {
            if (!slug.value) { docs.value = []; return; }
            docsLoading.value = true;
            routeReplaceError.value = '';
            try {
                docs.value = await api.listDocuments(slug.value, { limit: 500 });
                applyRouteReplaceTarget();
            } catch (_) {
                docs.value = [];
                if (routeReplaceActive.value) routeReplaceError.value = '目标文档不存在或已被删除，请返回文档管理重新选择';
            }
            finally { docsLoading.value = false; applyingRouteReplace.value = false; }
        }

        function applyRouteReplaceTarget() {
            if (!routeReplaceActive.value) return;
            const target = docs.value.find((d) => String(d.id) === String(routeReplaceDocumentId.value));
            if (target) {
                replaceDocId.value = target.id;
                routeReplaceTitle.value = target.title || routeReplaceTitle.value || target.id;
                routeReplaceError.value = '';
            } else {
                replaceDocId.value = routeReplaceDocumentId.value;
                routeReplaceError.value = '目标文档不存在或已被删除，请返回文档管理重新选择';
            }
        }

        async function loadStats() {
            if (!slug.value) { stats.value = null; return; }
            try {
                stats.value = await api.libraryStats(slug.value);
            } catch (_) { stats.value = null; }
        }

        async function loadImportConfiguration() {
            importConfiguration.value = DEFAULT_IMPORT_CONFIGURATION;
            if (!slug.value) return;
            importConfigurationLoading.value = true;
            try {
                importConfiguration.value = await api.getImportConfiguration(slug.value);
            } catch (_) {
                ElMessage.warning('无法读取上传配置，已使用默认限制');
            } finally {
                importConfigurationLoading.value = false;
            }
        }

        function importProfileFor(config) {
            if (
                config.max_file_bytes === IMPORT_PROFILE_INITIAL.max_file_bytes
                && config.max_files_per_selection === IMPORT_PROFILE_INITIAL.max_files_per_selection
            ) return 'initial';
            if (
                config.max_file_bytes === IMPORT_PROFILE_DAILY.max_file_bytes
                && config.max_files_per_selection === IMPORT_PROFILE_DAILY.max_files_per_selection
            ) return 'daily';
            return 'custom';
        }

        function applyImportConfigurationProfile(profile) {
            importConfigurationProfile.value = profile;
            const selected = profile === 'initial'
                ? IMPORT_PROFILE_INITIAL
                : profile === 'daily' ? IMPORT_PROFILE_DAILY : null;
            if (!selected) return;
            importConfigurationForm.value = {
                max_file_mib: selected.max_file_bytes / (1024 * 1024),
                max_files_per_selection: selected.max_files_per_selection,
            };
        }

        function openImportConfigurationDialog() {
            if (!canConfigureImport.value || !slug.value) return;
            importConfigurationProfile.value = importProfileFor(importConfiguration.value);
            importConfigurationForm.value = {
                max_file_mib: importConfiguration.value.max_file_bytes / (1024 * 1024),
                max_files_per_selection: importConfiguration.value.max_files_per_selection,
            };
            importConfigurationDialogVisible.value = true;
        }

        async function saveImportConfiguration() {
            const maxFileMib = Number(importConfigurationForm.value.max_file_mib);
            const maxFiles = Number(importConfigurationForm.value.max_files_per_selection);
            const maxConfigurableMib = importConfiguration.value.max_configurable_file_bytes / (1024 * 1024);
            if (!Number.isInteger(maxFileMib) || maxFileMib < 1 || maxFileMib > maxConfigurableMib) {
                ElMessage.warning(`单文件上限必须是 1 到 ${maxConfigurableMib} MiB 的整数`);
                return;
            }
            if (!Number.isInteger(maxFiles) || maxFiles < 1
                || maxFiles > importConfiguration.value.max_configurable_files_per_selection) {
                ElMessage.warning(`文件数量必须是 1 到 ${importConfiguration.value.max_configurable_files_per_selection} 的整数`);
                return;
            }
            importConfigurationSaving.value = true;
            try {
                importConfiguration.value = await api.updateImportConfiguration(slug.value, {
                    max_file_bytes: maxFileMib * 1024 * 1024,
                    max_files_per_selection: maxFiles,
                });
                importConfigurationDialogVisible.value = false;
                ElMessage.success('上传限制已更新');
            } catch (error) {
                ElMessage.error(`上传限制更新失败：${humanizeError(error)}`);
            } finally {
                importConfigurationSaving.value = false;
            }
        }

        async function loadGraphExtractionConfiguration({ applyLibraryDefault = false } = {}) {
            const requestSeq = ++graphConfigRequestSeq;
            graphExtractionConfig.value = null;
            graphExtractionConfigError.value = '';
            graphExtractionSecurityLevel.value = '';
            graphExtractionConfigLoading.value = false;
            if (applyLibraryDefault) graphExtractionRequested.value = false;
            if (!slug.value) return;
            graphExtractionConfigLoading.value = true;
            try {
                const config = await api.getUploadGraphExtractionConfiguration(slug.value);
                if (requestSeq !== graphConfigRequestSeq) return;
                if (
                    !config ||
                    typeof config.default_requested !== 'boolean' ||
                    typeof config.exploration_available !== 'boolean' ||
                    typeof config.requires_active_schema !== 'boolean' ||
                    !['disabled', 'explore', 'governed'].includes(config.schema_mode) ||
                    !Object.hasOwn(BUILD_MODE_LABEL, config.default_build_mode) ||
                    !Array.isArray(config.allowed_security_levels) ||
                    !Array.isArray(config.reasons)
                ) {
                    graphExtractionConfigError.value = '图谱抽取配置响应无效';
                    return;
                }
                graphExtractionConfig.value = config;
                graphExtractionSecurityLevel.value = config.allowed_security_levels[0] || '';
                if (applyLibraryDefault) {
                    graphExtractionRequested.value = config.default_requested;
                }
            } catch (_) {
                if (requestSeq === graphConfigRequestSeq) {
                    graphExtractionConfigError.value = '无法确认图谱抽取配置，请稍后重试';
                }
            } finally {
                if (requestSeq === graphConfigRequestSeq) graphExtractionConfigLoading.value = false;
            }
        }

        function graphUploadOptions() {
            return graphJobRequested.value ? {
                graphExtractionRequested: true,
                securityLevel: graphExtractionSecurityLevel.value,
            } : {};
        }

        function graphConfirmationText() {
            if (!graphExtractionRequested.value) return '';
            if (graphExplorationMode.value) {
                return '；AI 自动发现（AI 自主抽取）将根据本次文件生成候选知识结构，并冻结到本次任务';
            }
            return formalGraphExtractionRequested.value
                ? `；安全级别为“${securityLevelLabel(graphExtractionSecurityLevel.value)}”，使用${buildModeLabel(graphExtractionConfig.value?.default_build_mode)}模式，向量化后自动抽取并发布合格事实`
                : '';
        }

        // ── File handling (add mode) ─────────────────────────
        function attachGraphTracking(item, response, requested = graphExtractionRequested.value) {
            if (!requested || !item || !response) return;
            const document = (response.documents || [])[0] || response;
            if (!document.document_id) return;
            item.graphTracking = {
                documentId: document.document_id,
                embeddingJobId: document.job_id || null,
                startedAt: Date.now(),
                phase: 'embedding',
                progress: 8,
                tone: 'primary',
                label: document.job_id ? '等待向量化' : '等待向量化任务',
                terminal: document.operation === 'unchanged',
            };
            if (document.operation === 'unchanged') {
                item.graphTracking.progress = 100;
                item.graphTracking.tone = 'success';
                item.graphTracking.label = '内容未变化，无需重新构建图谱';
            }
            startGraphProgressPolling();
        }

        function trackedGraphRows() {
            return [
                ...queue.value,
                ...batchReplaceItems.value,
                ...(importResult.value?.documents || []),
            ].filter((row) => row.graphTracking && !row.graphTracking.terminal);
        }

        function stopGraphProgressPolling() {
            graphProgressRequestSeq += 1;
            if (graphProgressTimer) clearTimeout(graphProgressTimer);
            graphProgressTimer = null;
        }

        function startGraphProgressPolling() {
            if (graphProgressTimer || !trackedGraphRows().length) return;
            graphProgressTimer = setTimeout(pollGraphProgress, 200);
        }

        async function pollGraphRow(row, activeSlug, requestSeq) {
            const tracking = row.graphTracking;
            if (!tracking || tracking.terminal) return;

            if (tracking.phase === 'embedding') {
                const jobs = await api.listDocumentJobs(activeSlug, tracking.documentId, true);
                if (requestSeq !== graphProgressRequestSeq || slug.value !== activeSlug) return;
                const job = (jobs || []).find((candidate) =>
                    tracking.embeddingJobId && String(candidate.id) === String(tracking.embeddingJobId)
                ) || (jobs || [])[0];
                if (!job) {
                    tracking.label = '等待向量化任务';
                    tracking.progress = 8;
                    return;
                }
                if (job.status === 'failed' || job.status === 'superseded') {
                    tracking.label = job.status === 'failed'
                        ? '向量化失败，未构建知识图谱'
                        : '向量化任务已被替代';
                    tracking.progress = 100;
                    tracking.tone = 'exception';
                    tracking.terminal = true;
                    return;
                }
                if (job.status !== 'done') {
                    tracking.label = job.status === 'processing' ? '正在向量化' : '等待向量化';
                    tracking.progress = job.status === 'processing' ? 22 : 12;
                    return;
                }
                tracking.phase = 'graph';
                tracking.label = '等待图谱抽取任务';
                tracking.progress = 30;
            }

            const response = await api.listGraphExtractions(activeSlug, {
                document_id: tracking.documentId,
                limit: 5,
            });
            if (requestSeq !== graphProgressRequestSeq || slug.value !== activeSlug) return;
            const earliest = tracking.startedAt - 10000;
            const job = (response?.items || []).find((candidate) => {
                const createdAt = Date.parse(candidate.created_at || '');
                return !Number.isFinite(createdAt) || createdAt >= earliest;
            });
            if (!job) {
                tracking.label = '等待图谱抽取任务';
                tracking.progress = 30;
                return;
            }
            Object.assign(tracking, graphJobProgress(job), {
                phase: 'graph',
                jobId: job.id,
            });
        }

        async function pollGraphProgress() {
            graphProgressTimer = null;
            const activeSlug = slug.value;
            const requestSeq = graphProgressRequestSeq;
            const rows = trackedGraphRows();
            if (!activeSlug || !rows.length) return;
            await Promise.all(rows.map(async (row) => {
                try {
                    await pollGraphRow(row, activeSlug, requestSeq);
                    row.graphTracking.pollError = '';
                } catch (_) {
                    if (requestSeq === graphProgressRequestSeq && slug.value === activeSlug) {
                        row.graphTracking.pollError = '进度暂时无法刷新，正在重试';
                    }
                }
            }));
            if (requestSeq === graphProgressRequestSeq && trackedGraphRows().length) {
                graphProgressTimer = setTimeout(pollGraphProgress, 2000);
            }
        }

        function triggerFileSelect() {
            if (uploading.value || addingFiles.value) return;
            if (fileInput.value) fileInput.value.click();
        }

        function triggerFolderSelect() {
            if (uploading.value || addingFiles.value) return;
            if (folderInput.value) folderInput.value.click();
        }

        async function addFiles(files) {
            if (uploading.value || addingFiles.value) return;
            if (!files || !files.length) return;
            addingFiles.value = true;
            const validationState = createBatchValidationState(
                queue.value.map((it) => it._key),
                importConfiguration.value,
            );
            const summary = { duplicates: 0, invalid: 0, ignored: [] };
            const total = Number(files.length) || 0;
            try {
                for (let start = 0; start < total; start += FILE_SELECTION_CHUNK_SIZE) {
                    const chunk = Array.prototype.slice.call(files, start, start + FILE_SELECTION_CHUNK_SIZE);
                    const { accepted, duplicates, invalid, ignored } = validateBatchChunk(chunk, validationState);
                    summary.duplicates += duplicates.length;
                    summary.invalid += invalid.length;
                    summary.ignored.push(...ignored);
                    for (const { file } of accepted) {
                        queue.value.push({
                            _key: fileKey(file),
                            file: Object.isExtensible(file) ? Object.freeze(file) : file,
                            name: file.name,
                            relativePath: file.webkitRelativePath || '',
                            size: file.size,
                            status: 'pending',
                            error: '',
                            progress: 0,
                            stageLabel: '',
                        });
                    }
                    for (const { file, reason, failType } of invalid) {
                        queue.value.push({
                            _key: fileKey(file),
                            file: null,
                            name: file.name,
                            relativePath: file.webkitRelativePath || '',
                            size: file.size,
                            status: 'invalid',
                            error: reason,
                            _failType: failType || 'format',
                        });
                    }
                    if (start + FILE_SELECTION_CHUNK_SIZE < total) await yieldToBrowser();
                }
            } finally {
                addingFiles.value = false;
            }

            const msgs = [];
            if (summary.duplicates) msgs.push(`${summary.duplicates} 个重复文件已跳过`);
            if (summary.invalid) msgs.push(`${summary.invalid} 个未通过校验`);
            if (summary.ignored.length) {
                const metadataCount = summary.ignored.filter(({ code }) => code === 'metadata_file').length;
                const officeLockCount = summary.ignored.filter(({ code }) => code === 'office_lock_file').length;
                const details = [
                    metadataCount ? `macOS 元数据 ${metadataCount} 个` : '',
                    officeLockCount ? `Office 临时锁文件 ${officeLockCount} 个` : '',
                ].filter(Boolean).join('，');
                msgs.push(`${summary.ignored.length} 个文件已忽略（${details}）`);
            }
            if (msgs.length) ElMessage.warning(msgs.join('；'));
        }

        function onFileChange(e) {
            addFiles(e.target.files);
            if (fileInput.value) fileInput.value.value = '';
        }

        function onFolderChange(e) {
            addFiles(e.target.files);
            if (folderInput.value) folderInput.value.value = '';
        }

        function onDragOver(e) {
            e.preventDefault();
            dragOver.value = true;
        }
        function onDragLeave() {
            dragOver.value = false;
        }
        function onDrop(e) {
            e.preventDefault();
            dragOver.value = false;
            addFiles(e.dataTransfer.files);
        }

        function removeItem(item) {
            if (uploading.value || addingFiles.value) return;
            const idx = queue.value.indexOf(item);
            if (idx >= 0) queue.value.splice(idx, 1);
        }

        function clearQueue() {
            if (uploading.value || addingFiles.value) return;
            queue.value = [];
            queuePage.value = 1;
        }

        // ── Replace mode file selection ──────────────────────
        function onReplaceFileChange(e) {
            const file = e.target.files?.[0];
            if (!file) { replaceFile.value = null; replaceFileError.value = ''; return; }
            const result = validateFile(file);
            if (result.valid) {
                replaceFile.value = file;
                replaceFileError.value = '';
            } else {
                replaceFile.value = file;
                replaceFileError.value = result.reason;
            }
            if (fileInput.value) fileInput.value.value = '';
        }

        function onReplaceDrop(e) {
            e.preventDefault();
            dragOver.value = false;
            const file = e.dataTransfer.files?.[0];
            if (!file) return;
            const result = validateFile(file);
            if (result.valid) {
                replaceFile.value = file;
                replaceFileError.value = '';
            } else {
                replaceFile.value = file;
                replaceFileError.value = result.reason;
            }
        }

        function triggerBatchReplaceFileSelect() {
            if (batchReplaceFileInput.value) batchReplaceFileInput.value.click();
        }

        function addBatchReplaceFiles(files) {
            if (!files || !files.length) return;
            const existingKeys = batchReplaceItems.value.map((it) => it._key);
            const items = createBatchReplaceItems(files, docs.value, existingKeys);
            batchReplaceItems.value.push(...items);
            if (items.length < files.length) ElMessage.warning('重复文件已跳过');
        }

        function onBatchReplaceFileChange(e) {
            addBatchReplaceFiles(e.target.files);
            if (batchReplaceFileInput.value) batchReplaceFileInput.value.value = '';
        }

        function onBatchReplaceDrop(e) {
            e.preventDefault();
            dragOver.value = false;
            addBatchReplaceFiles(e.dataTransfer.files);
        }

        function removeBatchReplaceItem(item) {
            const idx = batchReplaceItems.value.indexOf(item);
            if (idx >= 0) batchReplaceItems.value.splice(idx, 1);
        }

        function onBatchReplaceTargetChange(item, documentId) {
            setBatchReplaceTarget(item, documentId, docs.value);
        }

        function targetDocTitle(documentId) {
            const doc = docs.value.find((d) => String(d.id) === String(documentId));
            return doc?.title || documentId || '—';
        }

        function batchReplaceValidationText(item) {
            return item.validationStatus === 'valid' ? '通过' : (item.validationError || '未通过');
        }

        async function handleBatchReplace() {
            const ready = batchReplaceReadyItems.value;
            if (!slug.value || !ready.length) return;
            const graphRequested = graphJobRequested.value;
            try {
                await ElMessageBox.confirm(
                    `将覆盖 ${ready.length} 个已有文档；覆盖后会重新切分、重新向量化${graphConfirmationText()}；历史问答引用不会自动更新。`,
                    '确认批量替换', { type: 'warning' }
                );
            } catch (_) { return; }

            batchReplacing.value = true;
            try {
                const batchIds = createImportBatchIds(batchReplaceItems.value);
                const batchIdsByFile = new Map(
                    batchReplaceItems.value.map((item) => [item.file, batchIds.get(item)]),
                );
                const uploadOptions = graphUploadOptions();
                const resumeStatesByFile = new Map();
                if (graphRequested) {
                    for (const item of submittableBatchReplaceItems(batchReplaceItems.value)) {
                        const resumeState = {};
                        resumeStatesByFile.set(item.file, resumeState);
                        await createImportSessionForFile({
                            api,
                            slug: slug.value,
                            file: item.file,
                            batchId: batchIds.get(item),
                            options: {
                                ...uploadOptions,
                                replaceDocumentId: item.matchedDocId,
                            },
                            resumeState,
                        });
                    }
                }
                const result = await submitBatchReplaceItems(
                    batchReplaceItems.value,
                    slug.value,
                    (targetSlug, file, options) => uploadFileInChunks({
                        api,
                        slug: targetSlug,
                        file,
                        batchId: batchIdsByFile.get(file),
                        configuration: importConfiguration.value,
                        options,
                        resumeState: resumeStatesByFile.get(file),
                    }),
                    humanizeError,
                    uploadOptions,
                );
                for (const item of batchReplaceItems.value) {
                    attachGraphTracking(item, item.importResponse, graphRequested);
                }
                ElMessage.success(`批量替换完成：${result.submitted} 已提交，${result.skipped} 跳过${result.failed > 0 ? `，${result.failed} 失败` : ''}`);
                await loadDocs();
                await loadStats();
            } finally { batchReplacing.value = false; }
        }

        // ── Upload ───────────────────────────────────────────
        function stopImportJobsPolling() {
            if (importJobsTimer) clearTimeout(importJobsTimer);
            importJobsTimer = null;
            importJobsPollSequence += 1;
            importJobsPollCursor = 0;
            importJobsPollDelayIndex = 0;
            if (importJobsPollController) importJobsPollController.abort();
            importJobsPollController = null;
        }

        function scheduleImportJobsPolling(delay) {
            if (importJobsTimer || globalThis.document?.hidden) return;
            importJobsTimer = setTimeout(pollImportJobs, delay);
        }

        async function pollImportJobs() {
            importJobsTimer = null;
            if (importJobsPollPromise) return importJobsPollPromise;
            const targetSlug = slug.value;
            const batch = nextImportProgressBatch(queue.value, importJobsPollCursor);
            importJobsPollCursor = batch.nextCursor;
            if (!targetSlug || !batch.items.length) return;
            const requestSeq = importJobsPollSequence;
            const pollController = new AbortController();
            let changed = false;
            const pollPromise = (async () => {
            try {
                const jobs = await api.getImportJobProgress(
                    targetSlug,
                    batch.items.map((item) => item.importJobId),
                    { signal: pollController.signal },
                );
                if (requestSeq !== importJobsPollSequence || targetSlug !== slug.value) return;
                const byId = new Map((jobs || []).map((job) => [String(job.id), job]));
                for (const item of batch.items) {
                    if (!queue.value.includes(item) || String(item.importJobId) === '') continue;
                    const job = byId.get(String(item.importJobId));
                    if (!job) continue;
                    changed = changed
                        || item.status !== importDisplayStatus(job)
                        || item.stageLabel !== importStageLabel(job);
                    item.importJob = job;
                    item.stageLabel = importStageLabel(job);
                    item.progress = importStageProgress(job, 100);
                    if (job.status === 'succeeded') {
                        item.status = importDisplayStatus(job);
                        item.error = '';
                    } else if (['failed', 'cancelled', 'superseded'].includes(job.status)) {
                        item.status = importDisplayStatus(job);
                        item.error = job.last_error || '导入失败';
                    } else {
                        item.status = importDisplayStatus(job);
                    }
                }
            } catch (_) {
                // Keep the last known stage and retry polling.
            }
            })();
            importJobsPollController = pollController;
            importJobsPollPromise = pollPromise;
            try {
                await pollPromise;
            } finally {
                if (importJobsPollPromise === pollPromise) importJobsPollPromise = null;
                if (importJobsPollController === pollController) importJobsPollController = null;
            }
            if (requestSeq !== importJobsPollSequence || targetSlug !== slug.value) return;
            if (nextImportProgressBatch(queue.value, importJobsPollCursor).items.length) {
                importJobsPollDelayIndex = changed ? 0 : Math.min(
                    importJobsPollDelayIndex + 1,
                    IMPORT_PROGRESS_POLL_DELAYS.length - 1,
                );
                scheduleImportJobsPolling(IMPORT_PROGRESS_POLL_DELAYS[importJobsPollDelayIndex]);
            } else {
                loadStats();
            }
        }

        function startImportJobsPolling() {
            scheduleImportJobsPolling(300);
        }

        function onImportVisibilityChange() {
            if (globalThis.document?.hidden) {
                stopImportJobsPolling();
                return;
            }
            if (nextImportProgressBatch(queue.value, importJobsPollCursor).items.length) {
                startImportJobsPolling();
            }
        }

        async function retryGraphImport(importJob, targetSlug) {
            try {
                return await api.retryGraphExtraction(
                    targetSlug,
                    importJob.retry_target_id,
                );
            } catch (error) {
                if (error?.body?.detail !== 'no_retryable_units') throw error;
                return api.rerunGraphExtraction(targetSlug, importJob.retry_target_id, {
                    client_idempotency_key: `import-graph-rerun-${importJob.retry_target_id}-${Date.now()}`,
                });
            }
        }

        async function runQueue(onlyFailed) {
            if (!slug.value) return;
            if (uploading.value || addingFiles.value) return;
            if (multiBlockedByExtId.value) {
                ElMessage.warning('多文件模式下，外部文档编号只能用于单个文件');
                return;
            }
            const targetSlug = slug.value;
            uploading.value = true;
            const filterStatus = onlyFailed ? 'failed' : 'pending';
            const items = queue.value.filter((it) => it.status === filterStatus);
            if (graphExtractionRequested.value) {
                if (!graphExtractionReady.value) {
                    ElMessage.warning('请先完成图谱抽取配置确认');
                    uploading.value = false;
                    return;
                }
                try {
                    await ElMessageBox.confirm(
                        `将上传 ${items.length} 个文件${graphConfirmationText()}。使用中发现错误后，可在知识治理中修正并重新发布。`,
                        '确认上传并抽取图谱',
                        { type: 'warning' },
                    );
                } catch (_) {
                    uploading.value = false;
                    return;
                }
            }

            const uploadConfiguration = { ...importConfiguration.value };
            const uploadOptions = graphUploadOptions();
            const uploadExternalId = (items.length === 1 && extIdSet.value)
                ? externalId.value.trim()
                : null;
            const uploadController = new AbortController();
            activeUploadController = uploadController;
            let attemptedCount = 0;
            try {
                const batchIds = createImportBatchIds(items);
                if (graphJobRequested.value && !onlyFailed) {
                    await runConcurrent(
                        items,
                        uploadConfiguration,
                        async (it) => {
                            if (!it.file) return;
                            it._uploadResumeState = it._uploadResumeState || {};
                            await createImportSessionForFile({
                                api,
                                slug: targetSlug,
                                file: it.file,
                                batchId: batchIds.get(it),
                                options: {
                                    ...uploadOptions,
                                    externalId: uploadExternalId,
                                },
                                resumeState: it._uploadResumeState,
                                signal: uploadController.signal,
                            });
                        },
                        (it) => queue.value.includes(it),
                    );
                }
                attemptedCount = await runConcurrent(
                    items,
                    uploadConfiguration,
                    async (it) => {
                        if (!it.file) return;
                        it.status = 'uploading';
                        it.error = '';
                        try {
                            if (onlyFailed && it.importJobId && it.importJob?.status === 'failed') {
                                if (
                                    it.importJob.retry_target_type === 'graph'
                                    && it.importJob.retry_target_id
                                ) {
                                    await retryGraphImport(it.importJob, targetSlug);
                                    it.importJob = {
                                        ...it.importJob,
                                        status: 'processing',
                                        current_stage: 'graph',
                                        last_error: null,
                                        retry_target_type: null,
                                        retry_target_id: null,
                                    };
                                    it.status = importDisplayStatus(it.importJob);
                                    it.error = '';
                                    it.stageLabel = importStageLabel(it.importJob);
                                    it.progress = importStageProgress(it.importJob, 100);
                                    return;
                                }
                                if (it.importJob.retry_target_type !== 'import') {
                                    throw new Error('当前失败任务没有可用的重试方式，请刷新后查看最新状态');
                                }
                                const retried = await api.retryImportJob(targetSlug, it.importJobId);
                                it.importJob = retried;
                                it.status = importDisplayStatus(retried);
                                it.stageLabel = importStageLabel(retried);
                                return;
                            }
                            it._uploadResumeState = it._uploadResumeState || {};
                            const job = await uploadFileInChunks({
                                api,
                                slug: targetSlug,
                                file: it.file,
                                batchId: batchIds.get(it),
                                configuration: uploadConfiguration,
                                resumeState: it._uploadResumeState,
                                options: {
                                    ...uploadOptions,
                                    externalId: uploadExternalId,
                                },
                                onProgress(update) {
                                    it.importJobId = update.job?.id || it.importJobId;
                                    it.importJob = update.job || it.importJob;
                                    it.stageLabel = importStageLabel({
                                        ...update.job,
                                        current_stage: update.phase,
                                    });
                                    const ratio = update.total ? update.offset / update.total : 0;
                                    it.progress = importStageProgress(
                                        { current_stage: update.phase },
                                        ratio * 100,
                                    );
                                },
                                signal: uploadController.signal,
                            });
                            it.importJobId = job.id;
                            it.importJob = job;
                            it.status = importDisplayStatus(job);
                            it.stageLabel = importStageLabel(job);
                            it.progress = importStageProgress(job, 100);
                        } catch (e) {
                            if (e?.name === 'AbortError' || uploadController.signal.aborted) throw e;
                            it.status = 'failed';
                            it.error = humanizeError(e);
                        }
                    },
                    (it) => queue.value.includes(it),
                );
            } catch (e) {
                if (e?.name === 'AbortError' || uploadController.signal.aborted) return;
                ElMessage.error(`上传未能开始：${humanizeError(e)}`);
                return;
            } finally {
                if (activeUploadController === uploadController) {
                    activeUploadController = null;
                }
                uploading.value = false;
            }

            if (uploadController.signal.aborted) return;
            startImportJobsPolling();
            const fail = items.filter((item) => item.status === 'failed').length;
            const ok = Math.max(0, attemptedCount - fail);
            ElMessage.success(`上传完成：${ok} 成功${fail > 0 ? `，${fail} 失败` : ''}`);
        }

        // ── Replace mode ─────────────────────────────────────
        async function handleReplace() {
            if (!slug.value || !replaceDocId.value || !replaceFile.value) return;
            if (routeReplaceError.value) {
                ElMessage.warning(routeReplaceError.value);
                return;
            }
            if (replaceFileError.value) {
                ElMessage.warning('文件未通过校验：' + replaceFileError.value);
                return;
            }
            const file = replaceFile.value;
            const graphRequested = graphJobRequested.value;
            try {
                await ElMessageBox.confirm(
                    `确认用 "${file.name}" 替换当前文档？文档内容将完全覆盖，文档版本号递增，并重新向量化${graphConfirmationText()}。`,
                    '确认替换', { type: 'warning' }
                );
            } catch (_) { return; }
            loading.value = true;
            try {
                const resp = await api.importFile(slug.value, file, {
                    ...graphUploadOptions(),
                    replaceDocumentId: replaceDocId.value,
                });
                importResult.value = resp;
                for (const document of (resp?.documents || [])) {
                    attachGraphTracking(document, document, graphRequested);
                }
                const docs = (resp && resp.documents) ? resp.documents : [];
                const allUnchanged = docs.length > 0 && docs.every((d) => d.operation === 'unchanged');
                if (allUnchanged) {
                    ElMessage.warning('内容未变更，文档保持原样');
                } else {
                    ElMessage.success('替换成功');
                }
                replaceFile.value = null;
                replaceFileError.value = '';
                if (!routeReplaceActive.value) replaceDocId.value = null;
                await loadDocs();
                await loadStats();
            } catch (e) {
                ElMessage.error(humanizeError(e));
            } finally { loading.value = false; }
        }

        function clearReplace() {
            importResult.value = null;
            if (!routeReplaceActive.value) replaceDocId.value = null;
            replaceFile.value = null;
            replaceFileError.value = '';
        }

        function backToDocuments() {
            router.push({
                path: '/knowledge-assets/catalog',
                query: slug.value ? { library: slug.value } : {},
            });
        }

        // ── Watchers ─────────────────────────────────────────
        watch(slug, () => {
            stopGraphProgressPolling();
            stopImportJobsPolling();
            graphConfigRequestSeq += 1;
            graphExtractionConfig.value = null;
            graphExtractionConfigError.value = '';
            graphExtractionSecurityLevel.value = '';
            queue.value = [];
            batchReplaceItems.value = [];
            importResult.value = null;
            if (!routeReplaceActive.value) replaceDocId.value = null;
            replaceFile.value = null;
            replaceFileError.value = '';
            showExtId.value = false;
            externalId.value = '';
            loadDocs();
            loadStats();
            loadImportConfiguration();
            loadGraphExtractionConfiguration({ applyLibraryDefault: true });
        });
        watch(graphExtractionRequested, (requested) => {
            if (
                requested &&
                slug.value &&
                !graphExtractionConfig.value &&
                !graphExtractionConfigLoading.value
            ) {
                loadGraphExtractionConfiguration();
            }
        });
        watch(mode, () => {
            stopGraphProgressPolling();
            importResult.value = null;
            clearQueue();
            batchReplaceItems.value = [];
            replaceFile.value = null;
            replaceFileError.value = '';
            if (mode.value !== 'replace') {
                routeReplaceDocumentId.value = '';
                routeReplaceTitle.value = '';
                routeReplaceError.value = '';
            }
            if (mode.value === 'replace') {
                if (!routeReplaceActive.value) replaceDocId.value = null;
                loadDocs();
                loadStats();
            }
        });

        onMounted(() => {
            globalThis.document?.addEventListener('visibilitychange', onImportVisibilityChange);
            loadLibs();
        });
        onBeforeUnmount(() => {
            activeUploadController?.abort();
            activeUploadController = null;
            stopGraphProgressPolling();
            stopImportJobsPolling();
            globalThis.document?.removeEventListener('visibilitychange', onImportVisibilityChange);
        });

        return {
            libs, slug, mode, fileInput, folderInput, batchReplaceFileInput, externalId, showExtId, replaceDocId,
            replaceFile, replaceFileError, routeReplaceDocumentId, routeReplaceTitle,
            routeReplaceError, routeReplaceActive, replaceTargetTitle, selectedReplaceDoc,
            applyingRouteReplace, batchReplaceItems, batchReplacing, batchReplaceReadyItems,
            graphExtractionRequested, graphExtractionConfig, graphExtractionConfigLoading,
            graphExtractionConfigError, graphExtractionSecurityLevel,
            graphExtractionReady, graphExtractionStatus, buildModeLabel, schemaModeLabel,
            graphJobRequested,
            docs, docsLoading, loading, importResult, docQuery,
            queue, queuePage, queuePageSize, uploading, addingFiles, stats, dragOver, importConfiguration, importConfigurationLoading,
            importAccept, importFormatLabel,
            importConfigurationDialogVisible, importConfigurationSaving,
            importConfigurationProfile, importConfigurationForm, canConfigureImport,
            displayDocs, displayQueue, extIdSet, multiBlockedByExtId,
            pendingCount, submittedCount, skippedCount, failedCount, invalidCount,
            hasFailed, canStart, canReplace, canBatchReplace,
            graphProgressDetail,
            loadLibs, loadDocs, triggerFileSelect, triggerFolderSelect,
            openImportConfigurationDialog, applyImportConfigurationProfile, saveImportConfiguration,
            triggerBatchReplaceFileSelect, onFileChange, onFolderChange,
            onDragOver, onDragLeave, onDrop,
            addFiles, removeItem, clearQueue, runQueue,
            onReplaceFileChange, onReplaceDrop,
            onBatchReplaceFileChange, onBatchReplaceDrop, removeBatchReplaceItem,
            onBatchReplaceTargetChange, targetDocTitle, batchReplaceValidationText, handleBatchReplace,
            loadGraphExtractionConfiguration,
            handleReplace, clearReplace, backToDocuments,
            ST_LABEL, ST_TAG, OP_LABEL, OP_TAG,
            BATCH_REPLACE_MATCH_LABEL, BATCH_REPLACE_MATCH_TAG,
            BATCH_REPLACE_STATUS_LABEL, BATCH_REPLACE_STATUS_TAG,
            uploadEmpty,
            formatSize, fileTypeIcon, securityLevelLabel, MAX_BATCH_SIZE,
        };
    },
    template: `
    <div class="import-workspace">
        <el-alert v-if="routeReplaceActive && !routeReplaceError" type="warning" :closable="false" show-icon
                  class="import-replace-context"
                  :title="'正在替换《' + (replaceTargetTitle || routeReplaceDocumentId) + '》，新文件导入后会覆盖该文档并重新向量化。'" />
        <el-alert v-if="routeReplaceError" type="error" :closable="false" show-icon
                  class="import-replace-context" :title="routeReplaceError" />

        <!-- Config card -->
        <section class="import-config-card">
            <el-form :inline="true">
                <el-form-item label="目标库">
                    <el-select v-model="slug" placeholder="选择知识库" class="import-lib-select" :disabled="routeReplaceActive || batchReplacing || uploading || addingFiles">
                        <el-option v-for="l in libs" :key="l.slug"
                                   :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                    </el-select>
                </el-form-item>
                <el-form-item label="模式">
                    <el-radio-group v-model="mode" :disabled="routeReplaceActive || batchReplacing || uploading || addingFiles">
                        <el-radio value="add">新增数据</el-radio>
                        <el-radio value="replace">替换已有文档</el-radio>
                    </el-radio-group>
                </el-form-item>
                <el-form-item v-if="mode === 'replace'" label="目标文档">
                    <el-select v-model="replaceDocId" filterable
                               :filter-method="(v) => docQuery = v"
                               :loading="docsLoading || applyingRouteReplace" clearable
                               :disabled="routeReplaceActive || batchReplacing"
                               :placeholder="docsLoading || applyingRouteReplace ? '正在加载目标文档...' : '搜索并选择要替换的文档'"
                               class="import-replace-select"
                               popper-class="replace-doc-popper">
                        <el-option v-if="routeReplaceActive && replaceDocId && !selectedReplaceDoc"
                                   :label="replaceTargetTitle || replaceDocId" :value="replaceDocId" />
                        <el-option v-for="d in displayDocs" :key="d.id"
                                   :label="d.title || d.id.slice(0,8)" :value="d.id">
                            <div class="rdoc">
                                <div class="rdoc-l1">
                                    <span class="rdoc-name">{{ d.title || '(无标题)' }}</span>
                                    <span class="rdoc-meta">
                                        <el-tag size="small" :type="d.status === 'ready' ? 'success' : d.status === 'failed' ? 'danger' : 'info'">{{ d.status }}</el-tag>
                                    </span>
                                </div>
                                <div class="rdoc-l2">
                                    <span class="rdoc-ext">外部文档编号：{{ d.external_id || '—' }}</span>
                                    <span class="rdoc-time">{{ d.updated_at ? new Date(d.updated_at).toLocaleString('zh-CN') : '' }}</span>
                                </div>
                            </div>
                        </el-option>
                    </el-select>
                    <el-button v-if="routeReplaceActive" text @click="backToDocuments">返回文档管理</el-button>
                </el-form-item>
            </el-form>
            <div v-if="stats" class="import-stats-row">
                <span class="import-stat">文档总数 <b>{{ stats.document_count || 0 }}</b></span>
                <span class="import-stat">待处理任务 <b>{{ (stats.pending_jobs || 0) + (stats.processing_jobs || 0) }}</b></span>
            </div>
        </section>

        <!-- Two-column body: add mode -->
        <div v-if="mode === 'add'" class="import-body">
            <!-- Left: upload zone -->
            <section class="import-upload-card">
                <div class="import-dropzone"
                     :class="{ 'is-dragover': dragOver }"
                     @dragover="onDragOver"
                     @dragleave="onDragLeave"
                     @drop="onDrop">
                    <img :src="uploadEmpty" class="illustration-upload-empty" alt="" aria-hidden="true" />
                    <div class="import-dropzone-title">拖拽文件到此处</div>
                    <div class="import-dropzone-hint">支持 {{ importFormatLabel }}</div>
                    <div class="import-dropzone-hint">
                        单文件最大 {{ formatSize(importConfiguration.max_file_bytes) }}，
                        每次最多 {{ importConfiguration.max_files_per_selection }} 个
                        <el-tooltip v-if="canConfigureImport" content="上传限制设置" placement="top">
                            <el-button class="import-limit-settings" circle text
                                       :disabled="importConfigurationLoading || uploading || addingFiles"
                                       aria-label="上传限制设置"
                                       @click.stop="openImportConfigurationDialog">
                                <local-icon icon="mdi:cog-outline" />
                            </el-button>
                        </el-tooltip>
                    </div>
                    <div class="import-source-actions">
                        <el-button type="primary" :disabled="importConfigurationLoading || uploading || addingFiles"
                                   @click.stop="triggerFileSelect">
                            <local-icon icon="mdi:file-plus-outline" />选择文件
                        </el-button>
                        <el-button :disabled="importConfigurationLoading || uploading || addingFiles"
                                   @click.stop="triggerFolderSelect">
                            <local-icon icon="mdi:folder-upload-outline" />选择文件夹
                        </el-button>
                    </div>
                </div>
                <input ref="fileInput" type="file" multiple
                       :accept="importAccept"
                       :disabled="uploading || addingFiles"
                       class="import-file-input-hidden"
                       @change="onFileChange" />
                <input ref="folderInput" type="file" multiple webkitdirectory
                       :accept="importAccept"
                       :disabled="uploading || addingFiles"
                       class="import-file-input-hidden"
                       @change="onFolderChange" />
                <div class="import-graph-option" :class="{ 'is-enabled': graphExtractionRequested }">
                    <div class="import-graph-option-head">
                        <span>
                            <strong>抽取实体和关系</strong>
                            <small>文档向量化完成后执行</small>
                        </span>
                        <el-switch v-model="graphExtractionRequested"
                                   :disabled="graphExtractionConfigLoading || uploading || addingFiles"
                                   aria-label="完成上传后抽取实体和关系" />
                    </div>
                    <template v-if="graphExtractionRequested">
                        <el-select v-model="graphExtractionSecurityLevel"
                                   :disabled="uploading || graphExtractionConfigLoading || (!graphExtractionConfig?.available && !graphExtractionConfig?.exploration_available)"
                                   placeholder="选择安全级别">
                            <el-option v-for="level in graphExtractionConfig?.allowed_security_levels || []"
                                       :key="level" :label="securityLevelLabel(level)" :value="level" />
                        </el-select>
                        <p class="import-graph-mode">抽取策略：{{ schemaModeLabel(graphExtractionConfig) }} · 构建模式：{{ buildModeLabel(graphExtractionConfig?.default_build_mode) }}</p>
                        <p :class="graphExtractionReady ? 'is-ready' : 'is-blocked'">{{ graphExtractionStatus }}</p>
                    </template>
                </div>
                <el-collapse class="import-advanced-settings">
                    <el-collapse-item title="高级设置" name="advanced">
                        <el-button class="import-extid-toggle" text @click="showExtId = !showExtId">
                            {{ showExtId ? '收起外部文档编号' : '设置外部文档编号' }}
                        </el-button>
                        <div v-if="showExtId" class="import-extid-row">
                            <el-input v-model="externalId" placeholder="外部文档编号（可选）" clearable />
                            <p>用于与外部业务系统中的文档建立对应关系，普通上传无需填写。</p>
                        </div>
                    </el-collapse-item>
                </el-collapse>
            </section>

            <!-- Right: file list -->
            <section class="import-file-card">
                <div class="import-file-table-shell">
                    <el-table :data="displayQueue" empty-text="暂无文件，请从左侧添加">
                        <el-table-column label="文件" min-width="230">
                            <template #default="{row}">
                                <div class="import-file-name-cell">
                                    <img v-if="fileTypeIcon(row)" class="import-file-icon"
                                         :src="fileTypeIcon(row)" alt="" aria-hidden="true" />
                                    <local-icon v-else class="import-file-icon"
                                                icon="mdi:file-document-outline" />
                                    <span class="import-file-name" :title="row.relativePath || row.name">
                                        {{ row.relativePath || row.name }}
                                    </span>
                                </div>
                            </template>
                        </el-table-column>
                        <el-table-column label="大小" width="90" align="right">
                            <template #default="{row}">{{ formatSize(row.size) }}</template>
                        </el-table-column>
                        <el-table-column label="格式校验" width="80" align="center">
                            <template #default="{row}">
                                <span v-if="row._failType === 'format'" class="import-check-fail">✗</span>
                                <span v-else class="import-check-ok">✓</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="大小校验" width="80" align="center">
                            <template #default="{row}">
                                <span v-if="row._failType === 'size'" class="import-check-fail">✗</span>
                                <span v-else class="import-check-ok">✓</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="状态" width="90" align="center">
                            <template #default="{row}">
                                <el-tag :type="ST_TAG[row.status]" size="small">{{ ST_LABEL[row.status] }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="处理进度" min-width="220">
                            <template #default="{row}">
                                <div v-if="row.importJobId" class="import-graph-progress">
                                    <div class="import-graph-progress-head">
                                        <span>{{ row.stageLabel || '等待处理' }}</span>
                                        <b>{{ row.progress || 0 }}%</b>
                                    </div>
                                    <el-progress :percentage="row.progress || 0"
                                                 :status="row.status === 'failed' ? 'exception' : (row.status === 'submitted' || row.status === 'skipped') ? 'success' : undefined"
                                                 :stroke-width="6" :show-text="false" />
                                </div>
                                <div v-else-if="row.graphTracking" class="import-graph-progress">
                                    <div class="import-graph-progress-head">
                                        <span>{{ graphProgressDetail(row.graphTracking) }}</span>
                                        <b>{{ row.graphTracking.progress }}%</b>
                                    </div>
                                    <el-progress :percentage="row.graphTracking.progress"
                                                 :status="row.graphTracking.tone === 'primary' ? undefined : row.graphTracking.tone"
                                                 :stroke-width="6" :show-text="false" />
                                    <small v-if="row.graphTracking.pollError">{{ row.graphTracking.pollError }}</small>
                                </div>
                                <span v-else class="import-graph-progress-empty">上传后开始</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="错误原因" min-width="120">
                            <template #default="{row}">
                                <span class="import-error-text">{{ row.error || '—' }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="70" align="center">
                            <template #default="{row}">
                                <el-button v-if="row.status === 'pending' || row.status === 'invalid'"
                                           link type="danger" :disabled="uploading || addingFiles"
                                           @click="removeItem(row)">移除</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>
                <el-pagination v-if="queue.length > queuePageSize"
                               v-model:current-page="queuePage"
                               :page-size="queuePageSize"
                               layout="prev, pager, next, total"
                               :total="queue.length"
                               class="import-queue-pagination" />

                <!-- Summary bar -->
                <div class="import-summary-bar">
                    <div class="import-summary-stats">
                        <span>总数 <b>{{ queue.length }}</b></span>
                        <span class="import-stat-pending">待上传 <b>{{ pendingCount }}</b></span>
                        <span class="import-stat-ok">成功 <b>{{ submittedCount }}</b></span>
                        <span class="import-stat-skip">跳过 <b>{{ skippedCount }}</b></span>
                        <span class="import-stat-fail">失败 <b>{{ failedCount }}</b></span>
                        <span v-if="invalidCount" class="import-stat-invalid">无效 <b>{{ invalidCount }}</b></span>
                    </div>
                    <div class="import-summary-actions">
                        <el-button type="primary" :disabled="!canStart" :loading="uploading"
                                   @click="runQueue(false)">开始上传</el-button>
                        <el-button :disabled="!hasFailed || uploading"
                                   @click="runQueue(true)">仅重试失败</el-button>
                        <el-button :disabled="uploading" @click="clearQueue">清空列表</el-button>
                    </div>
                </div>
            </section>
        </div>

        <!-- Replace mode -->
        <div v-if="mode === 'replace'" class="import-body import-replace-body">
            <section class="import-upload-card">
                <div class="import-dropzone"
                     :class="{ 'is-dragover': dragOver }"
                     @dragover="onDragOver"
                     @dragleave="onDragLeave"
                     @drop="onReplaceDrop"
                     @click="triggerFileSelect">
                    <img :src="uploadEmpty" class="illustration-upload-empty" alt="" aria-hidden="true" />
                    <div class="import-dropzone-title">单文件替换</div>
                    <div class="import-dropzone-hint">选择 1 个文件替换上方目标文档</div>
                    <div class="import-dropzone-hint">支持 {{ importFormatLabel }}</div>
                </div>
                <input ref="fileInput" type="file"
                       :accept="importAccept"
                       class="import-file-input-hidden"
                       @change="onReplaceFileChange" />
                <div class="import-graph-option" :class="{ 'is-enabled': graphExtractionRequested }">
                    <div class="import-graph-option-head">
                        <span>
                            <strong>抽取实体和关系</strong>
                            <small>替换完成并向量化后执行</small>
                        </span>
                        <el-switch v-model="graphExtractionRequested"
                                   :disabled="graphExtractionConfigLoading"
                                   aria-label="替换后抽取实体和关系" />
                    </div>
                    <template v-if="graphExtractionRequested">
                        <el-select v-model="graphExtractionSecurityLevel"
                                   :disabled="graphExtractionConfigLoading || (!graphExtractionConfig?.available && !graphExtractionConfig?.exploration_available)"
                                   placeholder="选择安全级别">
                            <el-option v-for="level in graphExtractionConfig?.allowed_security_levels || []"
                                       :key="level" :label="securityLevelLabel(level)" :value="level" />
                        </el-select>
                        <p class="import-graph-mode">抽取策略：{{ schemaModeLabel(graphExtractionConfig) }} · 构建模式：{{ buildModeLabel(graphExtractionConfig?.default_build_mode) }}</p>
                        <p :class="graphExtractionReady ? 'is-ready' : 'is-blocked'">{{ graphExtractionStatus }}</p>
                    </template>
                </div>
                <div v-if="replaceFile" class="import-replace-file-info">
                    <div class="import-replace-file-name">
                        <img v-if="fileTypeIcon(replaceFile)" class="import-file-icon"
                             :src="fileTypeIcon(replaceFile)" alt="" aria-hidden="true" />
                        <local-icon v-else class="import-file-icon"
                                    icon="mdi:file-document-outline" />
                        <span>{{ replaceFile.name }}</span>
                        <span class="import-replace-file-size">({{ formatSize(replaceFile.size) }})</span>
                    </div>
                    <div v-if="replaceFileError" class="import-error-text">{{ replaceFileError }}</div>
                </div>
                <el-button type="primary" :loading="loading"
                           :disabled="!canReplace"
                           class="import-replace-btn"
                           @click="handleReplace">替换文档</el-button>
                <el-button v-if="importResult" class="import-replace-clear" @click="clearReplace">清除结果</el-button>
            </section>

            <section class="import-file-card import-batch-replace-card">
                <div class="import-batch-replace-head">
                    <div>
                        <h3>批量替换已有文档</h3>
                        <p>拖入多个文件，按文件名自动匹配目标文档；未匹配和多候选必须手动选择，不会静默新增。</p>
                    </div>
                    <el-button type="primary" :loading="batchReplacing" :disabled="!canBatchReplace"
                               @click="handleBatchReplace">批量提交 {{ batchReplaceReadyItems.length }} 个</el-button>
                </div>
                <div class="import-batch-replace-dropzone"
                     :class="{ 'is-dragover': dragOver }"
                     @dragover="onDragOver"
                     @dragleave="onDragLeave"
                     @drop="onBatchReplaceDrop"
                     @click="triggerBatchReplaceFileSelect">
                    <div class="import-dropzone-title">拖入多个文件批量替换</div>
                    <div class="import-dropzone-hint">只提交唯一匹配且文件校验通过的项；多候选不会自动选第一个</div>
                </div>
                <input ref="batchReplaceFileInput" type="file" multiple
                       :accept="importAccept"
                       class="import-file-input-hidden"
                       @change="onBatchReplaceFileChange" />
                <div class="import-file-table-shell import-batch-replace-table-shell">
                    <el-table :data="batchReplaceItems" empty-text="暂无批量替换文件">
                        <el-table-column label="新文件名" min-width="180">
                            <template #default="{row}">
                                <div class="import-file-name-cell">
                                    <img v-if="fileTypeIcon(row.file)" class="import-file-icon"
                                         :src="fileTypeIcon(row.file)" alt="" aria-hidden="true" />
                                    <span class="import-file-name" :title="row.name">{{ row.name }}</span>
                                </div>
                            </template>
                        </el-table-column>
                        <el-table-column label="目标文档" min-width="220">
                            <template #default="{row}">
                                <el-select :model-value="row.matchedDocId"
                                           filterable clearable
                                           :disabled="batchReplacing"
                                           placeholder="选择目标文档"
                                           class="import-batch-target-select"
                                           @change="(v) => onBatchReplaceTargetChange(row, v)">
                                    <el-option v-for="d in docs" :key="d.id"
                                               :label="d.title || d.id.slice(0,8)" :value="d.id" />
                                </el-select>
                                <div class="import-batch-target-title">{{ targetDocTitle(row.matchedDocId) }}</div>
                            </template>
                        </el-table-column>
                        <el-table-column label="匹配状态" width="110" align="center">
                            <template #default="{row}">
                                <el-tag :type="BATCH_REPLACE_MATCH_TAG[row.matchStatus]" size="small">{{ BATCH_REPLACE_MATCH_LABEL[row.matchStatus] }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="文件校验" width="120">
                            <template #default="{row}">
                                <span :class="row.validationStatus === 'valid' ? 'import-check-ok' : 'import-check-fail'">{{ batchReplaceValidationText(row) }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="提交状态" width="100" align="center">
                            <template #default="{row}">
                                <el-tag :type="BATCH_REPLACE_STATUS_TAG[row.status]" size="small">{{ BATCH_REPLACE_STATUS_LABEL[row.status] }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="知识图谱构建" min-width="220">
                            <template #default="{row}">
                                <div v-if="row.graphTracking" class="import-graph-progress">
                                    <div class="import-graph-progress-head">
                                        <span>{{ graphProgressDetail(row.graphTracking) }}</span>
                                        <b>{{ row.graphTracking.progress }}%</b>
                                    </div>
                                    <el-progress :percentage="row.graphTracking.progress"
                                                 :status="row.graphTracking.tone === 'primary' ? undefined : row.graphTracking.tone"
                                                 :stroke-width="6" :show-text="false" />
                                </div>
                                <span v-else class="import-graph-progress-empty">{{ graphExtractionRequested ? '上传后开始' : '未开启' }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="错误原因" min-width="140">
                            <template #default="{row}">
                                <span class="import-error-text">{{ row.error || '—' }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="70" align="center">
                            <template #default="{row}">
                                <el-button link type="danger" :disabled="batchReplacing" @click="removeBatchReplaceItem(row)">移除</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>
                <div class="import-summary-bar">
                    <div class="import-summary-stats">
                        <span>总数 <b>{{ batchReplaceItems.length }}</b></span>
                        <span class="import-stat-ok">可提交 <b>{{ batchReplaceReadyItems.length }}</b></span>
                        <span class="import-stat-skip">已跳过 <b>{{ batchReplaceItems.filter((it) => it.status === 'skipped').length }}</b></span>
                        <span class="import-stat-fail">失败 <b>{{ batchReplaceItems.filter((it) => it.status === 'failed').length }}</b></span>
                    </div>
                </div>
            </section>

            <section v-if="importResult" class="import-file-card">
                <div class="import-replace-result">
                    <span class="import-replace-count">导入 {{ importResult.imported_count || 0 }} 个文档</span>
                </div>
                <div class="import-file-table-shell">
                    <el-table :data="importResult.documents || []">
                        <el-table-column label="标题" min-width="150">
                            <template #default="{row}">{{ row.title || row.document_id?.slice(0,8) || '—' }}</template>
                        </el-table-column>
                        <el-table-column label="操作" width="100" align="center">
                            <template #default="{row}">
                                <el-tag :type="OP_TAG[row.operation]" size="small">{{ OP_LABEL[row.operation] || row.operation }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="分片数" width="80" align="center">
                            <template #default="{row}">{{ row.chunk_count ?? '—' }}</template>
                        </el-table-column>
                        <el-table-column label="知识图谱构建" min-width="220">
                            <template #default="{row}">
                                <div v-if="row.graphTracking" class="import-graph-progress">
                                    <div class="import-graph-progress-head">
                                        <span>{{ graphProgressDetail(row.graphTracking) }}</span>
                                        <b>{{ row.graphTracking.progress }}%</b>
                                    </div>
                                    <el-progress :percentage="row.graphTracking.progress"
                                                 :status="row.graphTracking.tone === 'primary' ? undefined : row.graphTracking.tone"
                                                 :stroke-width="6" :show-text="false" />
                                </div>
                                <span v-else class="import-graph-progress-empty">{{ graphExtractionRequested ? '上传后开始' : '未开启' }}</span>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>
            </section>
        </div>

        <el-dialog v-model="importConfigurationDialogVisible" title="上传限制设置"
                   width="520px" class="import-limit-dialog" destroy-on-close>
            <el-radio-group v-model="importConfigurationProfile" class="import-limit-profiles"
                            @change="applyImportConfigurationProfile">
                <el-radio-button value="initial">首次导入</el-radio-button>
                <el-radio-button value="daily">日常使用</el-radio-button>
                <el-radio-button value="custom">自定义</el-radio-button>
            </el-radio-group>
            <el-form label-position="top" class="import-limit-form">
                <el-form-item label="单文件上限 (MiB)">
                    <el-input-number v-model="importConfigurationForm.max_file_mib"
                                     :min="1" :max="importConfiguration.max_configurable_file_bytes / 1024 / 1024"
                                     :step="100" controls-position="right"
                                     @change="importConfigurationProfile = 'custom'" />
                </el-form-item>
                <el-form-item label="单次最多文件数">
                    <el-input-number v-model="importConfigurationForm.max_files_per_selection"
                                     :min="1" :max="importConfiguration.max_configurable_files_per_selection"
                                     :step="100" controls-position="right"
                                     @change="importConfigurationProfile = 'custom'" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="importConfigurationDialogVisible = false">取消</el-button>
                <el-button type="primary" :loading="importConfigurationSaving"
                           @click="saveImportConfiguration">保存</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
