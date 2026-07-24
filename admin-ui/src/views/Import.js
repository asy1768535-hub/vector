import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';
import { resolveImportEntry } from '../import_query.js';
import { humanizeError } from './import_errors.js';
import {
    ST_LABEL, ST_TAG, OP_LABEL, OP_TAG,
    validateBatch, fileKey, formatSize, fileTypeIcon,
    MAX_BATCH_SIZE, validateFile,
} from '../import_ui.js';
import {
    BATCH_REPLACE_MATCH_LABEL, BATCH_REPLACE_MATCH_TAG,
    BATCH_REPLACE_STATUS_LABEL, BATCH_REPLACE_STATUS_TAG,
    createBatchReplaceItems, setBatchReplaceTarget,
    submittableBatchReplaceItems, submitBatchReplaceItems,
} from '../batch_replace.js';
import { uploadEmpty } from '../illustrations.js';

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libs = ref([]);
        const slug = ref(null);
        const mode = ref('add');
        const fileInput = ref(null);
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
        const uploading = ref(false);
        const stats = ref(null);
        const dragOver = ref(false);
        const showExtId = ref(false);
        const routeReplaceDocumentId = ref('');
        const routeReplaceTitle = ref('');
        const routeReplaceError = ref('');
        const applyingRouteReplace = ref(false);
        const batchReplaceItems = ref([]);
        const batchReplacing = ref(false);

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

        const pendingCount = computed(() =>
            queue.value.filter((it) => it.status === 'pending').length
        );
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

        const canStart = computed(() =>
            mode.value === 'add' && slug.value && pendingCount.value > 0 &&
            !uploading.value && !multiBlockedByExtId.value
        );

        const canReplace = computed(() =>
            mode.value === 'replace' && slug.value && replaceDocId.value &&
            replaceFile.value && !replaceFileError.value && !loading.value &&
            !docsLoading.value && !routeReplaceError.value && !batchReplacing.value
        );

        const batchReplaceReadyItems = computed(() =>
            submittableBatchReplaceItems(batchReplaceItems.value)
        );
        const canBatchReplace = computed(() =>
            mode.value === 'replace' && slug.value && batchReplaceReadyItems.value.length > 0 &&
            !batchReplacing.value && !loading.value && !docsLoading.value
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

        // ── File handling (add mode) ─────────────────────────
        function triggerFileSelect() {
            if (fileInput.value) fileInput.value.click();
        }

        function addFiles(files) {
            if (!files || !files.length) return;
            const existingKeys = queue.value.map((it) => it._key);
            const { accepted, duplicates, invalid } = validateBatch(Array.from(files), existingKeys);

            for (const { file } of accepted) {
                queue.value.push({
                    _key: fileKey(file),
                    file,
                    name: file.name,
                    size: file.size,
                    status: 'pending',
                    error: '',
                });
            }
            for (const { file, reason, failType } of invalid) {
                queue.value.push({
                    _key: fileKey(file),
                    file: null,
                    name: file.name,
                    size: file.size,
                    status: 'invalid',
                    error: reason,
                    _failType: failType || 'format',
                });
            }

            const msgs = [];
            if (duplicates.length) msgs.push(`${duplicates.length} 个重复文件已跳过`);
            if (invalid.length) msgs.push(`${invalid.length} 个未通过校验`);
            if (msgs.length) ElMessage.warning(msgs.join('；'));
        }

        function onFileChange(e) {
            addFiles(e.target.files);
            if (fileInput.value) fileInput.value.value = '';
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
            const idx = queue.value.indexOf(item);
            if (idx >= 0) queue.value.splice(idx, 1);
        }

        function clearQueue() {
            queue.value = [];
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
            try {
                await ElMessageBox.confirm(
                    `将覆盖 ${ready.length} 个已有文档；覆盖后会重新切分、重新向量化；历史问答引用不会自动更新。`,
                    '确认批量替换', { type: 'warning' }
                );
            } catch (_) { return; }

            batchReplacing.value = true;
            try {
                const result = await submitBatchReplaceItems(
                    batchReplaceItems.value,
                    slug.value,
                    api.importFile,
                    humanizeError
                );
                ElMessage.success(`批量替换完成：${result.submitted} 已提交，${result.skipped} 跳过${result.failed > 0 ? `，${result.failed} 失败` : ''}`);
                await loadDocs();
                await loadStats();
            } finally { batchReplacing.value = false; }
        }

        // ── Upload ───────────────────────────────────────────
        async function runQueue(onlyFailed) {
            if (!slug.value) return;
            if (multiBlockedByExtId.value) {
                ElMessage.warning('多文件模式下 external_id 只能用于单个文件');
                return;
            }
            uploading.value = true;
            const filterStatus = onlyFailed ? 'failed' : 'pending';
            const items = queue.value.filter((it) => it.status === filterStatus);

            for (const it of items) {
                if (!it.file) continue;
                it.status = 'uploading';
                try {
                    const extId = (items.length === 1 && extIdSet.value) ? externalId.value.trim() : null;
                    const resp = await api.importFile(slug.value, it.file, { externalId: extId });
                    const docs = (resp && resp.documents) ? resp.documents : [];
                    const allUnchanged = docs.length > 0 && docs.every((d) => d.operation === 'unchanged');
                    it.status = allUnchanged ? 'skipped' : 'submitted';
                    it.error = '';
                } catch (e) {
                    it.status = 'failed';
                    it.error = humanizeError(e);
                }
            }

            uploading.value = false;
            loadStats();
            const ok = submittedCount.value + skippedCount.value;
            const fail = failedCount.value;
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
            try {
                await ElMessageBox.confirm(
                    `确认用 "${file.name}" 替换当前文档？文档内容将完全覆盖，revision 递增，并重新向量化。`,
                    '确认替换', { type: 'warning' }
                );
            } catch (_) { return; }
            loading.value = true;
            try {
                const resp = await api.importFile(slug.value, file, { replaceDocumentId: replaceDocId.value });
                importResult.value = resp;
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
                path: '/knowledge-assets/documents',
                query: slug.value ? { slug: slug.value } : {},
            });
        }

        // ── Watchers ─────────────────────────────────────────
        watch(slug, () => {
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
        });
        watch(mode, () => {
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

        onMounted(loadLibs);

        return {
            libs, slug, mode, fileInput, batchReplaceFileInput, externalId, showExtId, replaceDocId,
            replaceFile, replaceFileError, routeReplaceDocumentId, routeReplaceTitle,
            routeReplaceError, routeReplaceActive, replaceTargetTitle, selectedReplaceDoc,
            applyingRouteReplace, batchReplaceItems, batchReplacing, batchReplaceReadyItems,
            docs, docsLoading, loading, importResult, docQuery,
            queue, uploading, stats, dragOver,
            displayDocs, extIdSet, multiBlockedByExtId,
            pendingCount, submittedCount, skippedCount, failedCount, invalidCount,
            hasFailed, canStart, canReplace, canBatchReplace,
            loadLibs, loadDocs, triggerFileSelect, triggerBatchReplaceFileSelect, onFileChange,
            onDragOver, onDragLeave, onDrop,
            addFiles, removeItem, clearQueue, runQueue,
            onReplaceFileChange, onReplaceDrop,
            onBatchReplaceFileChange, onBatchReplaceDrop, removeBatchReplaceItem,
            onBatchReplaceTargetChange, targetDocTitle, batchReplaceValidationText, handleBatchReplace,
            handleReplace, clearReplace, backToDocuments,
            ST_LABEL, ST_TAG, OP_LABEL, OP_TAG,
            BATCH_REPLACE_MATCH_LABEL, BATCH_REPLACE_MATCH_TAG,
            BATCH_REPLACE_STATUS_LABEL, BATCH_REPLACE_STATUS_TAG,
            uploadEmpty,
            formatSize, fileTypeIcon, MAX_BATCH_SIZE,
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
                    <el-select v-model="slug" placeholder="选择知识库" class="import-lib-select" :disabled="routeReplaceActive || batchReplacing">
                        <el-option v-for="l in libs" :key="l.slug"
                                   :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                    </el-select>
                </el-form-item>
                <el-form-item label="模式">
                    <el-radio-group v-model="mode" :disabled="routeReplaceActive || batchReplacing">
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
                                    <span class="rdoc-ext">{{ d.external_id || '—' }}</span>
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
                     @drop="onDrop"
                     @click="triggerFileSelect">
                    <img :src="uploadEmpty" class="illustration-upload-empty" alt="" aria-hidden="true" />
                    <div class="import-dropzone-title">点击选择文件或拖拽文件到此处</div>
                    <div class="import-dropzone-hint">支持 txt、md、markdown、json、csv、docx、xlsx、pdf</div>
                    <div class="import-dropzone-hint">单文件最大 50 MB，每批最多 {{ MAX_BATCH_SIZE }} 个</div>
                </div>
                <input ref="fileInput" type="file" multiple
                       accept=".txt,.md,.markdown,.json,.csv,.docx,.xlsx,.pdf"
                       class="import-file-input-hidden"
                       @change="onFileChange" />
                <div v-if="showExtId" class="import-extid-row">
                    <el-input v-model="externalId" placeholder="external_id" clearable />
                </div>
                <el-button class="import-extid-toggle" text @click="showExtId = !showExtId; if (!showExtId) externalId = ''">
                    {{ showExtId ? '移除 external_id' : '设置 external_id' }}
                </el-button>
            </section>

            <!-- Right: file list -->
            <section class="import-file-card">
                <div class="import-file-table-shell">
                    <el-table :data="queue" empty-text="暂无文件，请从左侧添加">
                        <el-table-column label="文件" min-width="180">
                            <template #default="{row}">
                                <div class="import-file-name-cell">
                                    <img v-if="fileTypeIcon(row)" class="import-file-icon"
                                         :src="fileTypeIcon(row)" alt="" aria-hidden="true" />
                                    <local-icon v-else class="import-file-icon"
                                                icon="mdi:file-document-outline" />
                                    <span class="import-file-name" :title="row.name">{{ row.name }}</span>
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
                        <el-table-column label="错误原因" min-width="120">
                            <template #default="{row}">
                                <span class="import-error-text">{{ row.error || '—' }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="70" align="center">
                            <template #default="{row}">
                                <el-button v-if="row.status === 'pending' || row.status === 'invalid'"
                                           link type="danger" @click="removeItem(row)">移除</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>

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
                    <div class="import-dropzone-hint">支持 txt、md、markdown、json、csv、docx、xlsx、pdf</div>
                </div>
                <input ref="fileInput" type="file"
                       accept=".txt,.md,.markdown,.json,.csv,.docx,.xlsx,.pdf"
                       class="import-file-input-hidden"
                       @change="onReplaceFileChange" />
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
                       accept=".txt,.md,.markdown,.json,.csv,.docx,.xlsx,.pdf"
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
                    </el-table>
                </div>
            </section>
        </div>
    </div>
    `,
};
