import { computed, onMounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';

const OP_LABEL = { created: '新建', updated: '已更新', unchanged: '未变更' };
const OP_TAG = { created: 'success', updated: 'warning', unchanged: 'info' };

// 多文件队列项状态
const ST_LABEL = { pending: '等待中', uploading: '上传中', submitted: '已提交', skipped: '跳过', failed: '失败' };
const ST_TAG = { pending: 'info', uploading: 'warning', submitted: 'success', skipped: 'info', failed: 'danger' };

// 旧二进制 Office 格式：无纯 Python 解析库，后端会 400 拒绝；前端在选择时即提示另存为新格式，
// 避免进入队列后只看到笼统的 400 文案。
const LEGACY_SAVE_AS = { '.doc': '请另存为 .docx 后再上传', '.xls': '请另存为 .xlsx 后再上传' };
function fileExt(name) {
    const i = name.lastIndexOf('.');
    return i >= 0 ? name.slice(i).toLowerCase() : '';
}

// 后端错误 → 人话（按 HTTP 状态；其它显示后端 detail）
function humanizeError(e) {
    const detail = e && e.message ? e.message : '上传失败';
    switch (e && e.status) {
        case 413: return '文件超过上传大小限制';
        case 415: return '暂不支持该文件类型';
        case 400: return '文件内容无法解析或不符合格式';
        case 409: return '当前知识库状态不允许上传';
        default:  return detail;
    }
}

export default {
    setup() {
        const libs = ref([]);
        const slug = ref(null);
        const mode = ref('add');           // 'add'（默认，多文件队列）| 'replace'（单文件）
        const fileInput = ref(null);
        const externalId = ref('');

        // 替换模式（保持单文件）
        const selectedFile = ref(null);
        const replaceDocId = ref(null);
        const docs = ref([]);
        const docsLoading = ref(false);
        const loading = ref(false);        // 替换模式上传中
        const importResult = ref(null);    // 替换模式结果
        const docQuery = ref('');

        // 新增模式（多文件队列）
        const queue = ref([]);             // [{ key, file, name, size, status, error }]
        const uploading = ref(false);      // 队列上传中
        const ranOnce = ref(false);        // 是否已点过「开始上传」（控制汇总/提示显示）

        async function loadLibs() {
            try {
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = store.permissions
                        .filter((p) => p.actions.includes('insert'))
                        .map((p) => ({ slug: p.library_slug, name: p.library_slug }));
                }
                if (!slug.value && libs.value.length) slug.value = libs.value[0].slug;
            } catch (e) {
                ElMessage.error(e.message);
            }
        }

        async function loadDocs() {
            if (!slug.value) { docs.value = []; return; }
            docsLoading.value = true;
            try {
                docs.value = await api.listDocuments(slug.value, { limit: 500 });
            } catch (e) {
                ElMessage.error('加载文档列表失败：' + (e.message || e));
                docs.value = [];
            } finally {
                docsLoading.value = false;
            }
        }

        // 切库：清空队列与结果（不同库不混批）；进入替换模式刷新可替换文档
        watch(slug, () => { clearQueue(); selectedFile.value = null; importResult.value = null; });
        watch(mode, () => {
            importResult.value = null;
            if (mode.value === 'replace') { replaceDocId.value = null; loadDocs(); }
        });

        function triggerFileSelect() {
            if (fileInput.value) fileInput.value.click();
        }

        function onFileChange(event) {
            const files = Array.from(event.target.files || []);
            if (!files.length) return;
            if (mode.value === 'replace') {
                const ext = fileExt(files[0].name);
                if (LEGACY_SAVE_AS[ext]) { ElMessage.warning(`${files[0].name}：${LEGACY_SAVE_AS[ext]}`); }
                else { selectedFile.value = files[0]; importResult.value = null; }
            } else {
                // 新增模式：追加到队列（按 名+大小+修改时间 去重），可分次累加
                const seen = new Set(queue.value.map((it) => it.key));
                for (const f of files) {
                    const ext = fileExt(f.name);
                    if (LEGACY_SAVE_AS[ext]) {            // 旧格式不入队，直接提示另存为
                        ElMessage.warning(`${f.name}：${LEGACY_SAVE_AS[ext]}`);
                        continue;
                    }
                    const key = `${f.name}__${f.size}__${f.lastModified}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    queue.value.push({ key, file: f, name: f.name, size: f.size, status: 'pending', error: '' });
                }
                ranOnce.value = false;
            }
            // 允许再次选择相同文件触发 change
            if (fileInput.value) fileInput.value.value = '';
        }

        function clearQueue() {
            queue.value = [];
            ranOnce.value = false;
        }

        function fmtTime(t) {
            if (!t) return '—';
            try { return new Date(t).toLocaleString(); } catch (e) { return t; }
        }

        function filterDocs(q) {
            docQuery.value = (q || '').trim().toLowerCase();
        }
        const displayDocs = computed(() => {
            const q = docQuery.value;
            if (!q) return docs.value;
            return docs.value.filter((d) =>
                (d.title || '').toLowerCase().includes(q) ||
                (d.external_id || '').toLowerCase().includes(q) ||
                (d.id || '').toLowerCase().includes(q),
            );
        });
        function onReplaceVisible(v) { if (v) docQuery.value = ''; }
        function selectedDoc() {
            return docs.value.find((d) => d.id === replaceDocId.value) || null;
        }

        function formatSize(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        // ── 新增模式：external_id 守卫与汇总 ────────────────────────────
        // external_id 仅适用于单文件 upsert：队列 >1 且填了 external_id 时禁止上传。
        const extIdSet = computed(() => externalId.value.trim() !== '');
        const multiBlockedByExtId = computed(() => mode.value === 'add' && queue.value.length > 1 && extIdSet.value);
        const hasFailed = computed(() => queue.value.some((it) => it.status === 'failed'));
        const pendingCount = computed(() => queue.value.filter((it) => it.status === 'pending').length);
        const summary = computed(() => {
            const c = { submitted: 0, skipped: 0, failed: 0 };
            for (const it of queue.value) if (c[it.status] !== undefined) c[it.status]++;
            return c;
        });
        const canStart = computed(() =>
            mode.value === 'add' && !!slug.value && pendingCount.value > 0 && !uploading.value && !multiBlockedByExtId.value,
        );

        // ── 新增模式：顺序逐个上传（失败不中断）────────────────────────
        async function runQueue(onlyFailed) {
            if (!slug.value) { ElMessage.warning('请选择目标库'); return; }
            if (multiBlockedByExtId.value) {
                ElMessage.warning('external_id 只适用于单文件 upsert；多文件上传请清空 external_id。');
                return;
            }
            const targets = queue.value.filter((it) =>
                onlyFailed ? it.status === 'failed' : it.status === 'pending',
            );
            if (!targets.length) {
                ElMessage.info(onlyFailed ? '没有失败的文件可重试' : '没有待上传的文件');
                return;
            }
            // external_id 仅在「队列恰好 1 个文件」时透传（单文件 upsert）；否则一律不带。
            const extId = (queue.value.length === 1 && extIdSet.value) ? externalId.value.trim() : null;

            uploading.value = true;
            ranOnce.value = true;
            try {
                for (const it of targets) {
                    it.status = 'uploading';
                    it.error = '';
                    try {
                        const resp = await api.importFile(slug.value, it.file, { externalId: extId });
                        const ops = (resp.documents || []).map((d) => d.operation).filter(Boolean);
                        // 全部 unchanged → 跳过（相同内容已存在）；否则视为已提交
                        it.status = (ops.length && ops.every((o) => o === 'unchanged')) ? 'skipped' : 'submitted';
                    } catch (e) {
                        it.status = 'failed';
                        it.error = humanizeError(e);
                    }
                }
            } finally {
                uploading.value = false;
            }
            const s = summary.value;
            ElMessage.success(`上传结束：成功 ${s.submitted}，跳过 ${s.skipped}，失败 ${s.failed}`);
        }

        // ── 替换模式：单文件（保持原行为）──────────────────────────────
        function reportTip(resp) {
            const ops = (resp.documents || []).map((d) => d.operation).filter(Boolean);
            if (ops.length && ops.every((o) => o === 'unchanged')) {
                ElMessage.warning('相同内容已存在，未重复新增');
            } else {
                ElMessage.success('替换成功：revision 已增加，旧内容已停止检索');
            }
        }

        async function handleReplace() {
            if (!slug.value) { ElMessage.warning('请选择目标库'); return; }
            if (!replaceDocId.value) { ElMessage.warning('请先选择要替换的已有文档'); return; }
            if (!selectedFile.value) { ElMessage.warning('请先选择用于替换的新文件'); return; }
            const target = selectedDoc();
            const name = (target && (target.title || target.external_id)) || replaceDocId.value.slice(0, 8);
            try {
                await ElMessageBox.confirm(
                    `将用新文件替换《${name}》，revision 将增加，旧内容会停止检索。`,
                    '确认替换',
                    { confirmButtonText: '确认替换', cancelButtonText: '取消', type: 'warning' },
                );
            } catch (e) { return; }
            loading.value = true;
            try {
                const resp = await api.importFile(slug.value, selectedFile.value, { replaceDocumentId: replaceDocId.value });
                importResult.value = resp;
                reportTip(resp);
                selectedFile.value = null;
                if (fileInput.value) fileInput.value.value = '';
                replaceDocId.value = null;
                loadDocs();
            } catch (e) {
                ElMessage.error(e.message || '操作失败，请检查文件格式');
            } finally {
                loading.value = false;
            }
        }

        onMounted(loadLibs);

        return {
            libs, slug, mode, fileInput, externalId,
            selectedFile, replaceDocId, docs, docsLoading, loading, importResult,
            queue, uploading, ranOnce, extIdSet, multiBlockedByExtId, hasFailed,
            pendingCount, summary, canStart,
            triggerFileSelect, onFileChange, clearQueue, runQueue, handleReplace,
            formatSize, fmtTime, displayDocs, filterDocs, onReplaceVisible,
            OP_LABEL, OP_TAG, ST_LABEL, ST_TAG,
        };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>导入数据</h2>
        </div>

        <el-row :gutter="20">
            <el-col :span="13">
                <el-card>
                    <template #header>
                        <div class="card-header"><span>文件导入</span></div>
                    </template>

                    <el-form label-position="top">
                        <el-form-item label="选择目标库" required>
                            <el-select v-model="slug" placeholder="请选择库" style="width: 100%">
                                <el-option v-for="l in libs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                            </el-select>
                        </el-form-item>

                        <el-form-item label="上传方式" required>
                            <el-radio-group v-model="mode" :disabled="uploading || loading">
                                <el-radio-button label="add">新增文档</el-radio-button>
                                <el-radio-button label="replace">替换已有文档</el-radio-button>
                            </el-radio-group>
                            <div style="font-size: 12px; color: #909399; margin-top: 6px;">
                                <span v-if="mode === 'add'">新增：可一次选择多个文件，按顺序逐个上传；按内容/external_id 去重。</span>
                                <span v-else>替换：选中一篇已有文档，用<b>单个</b>新文件覆盖它（revision +1，旧内容立即停止检索）。</span>
                            </div>
                        </el-form-item>

                        <el-form-item v-if="mode === 'replace'" label="选择要替换的文档" required>
                            <el-select v-model="replaceDocId" filterable :loading="docsLoading"
                                       :filter-method="filterDocs" @visible-change="onReplaceVisible"
                                       popper-class="replace-doc-popper" :fit-input-width="true"
                                       no-data-text="当前知识库暂无可替换文档"
                                       placeholder="按文件名 / external_id / 文档ID 搜索选择" style="width: 100%">
                                <el-option v-for="d in displayDocs" :key="d.id"
                                           :label="d.title || '(无标题)'" :value="d.id">
                                    <div class="rdoc">
                                        <div class="rdoc-l1">
                                            <span class="rdoc-name">{{ d.title || '(无标题)' }}</span>
                                            <span class="rdoc-meta">
                                                <el-tag size="small" type="info">{{ d.status }}</el-tag>
                                                <el-tag size="small">rev {{ d.current_revision }}</el-tag>
                                            </span>
                                        </div>
                                        <div class="rdoc-l2">
                                            <span class="rdoc-ext">external_id: {{ d.external_id || '（无）' }}</span>
                                            <span class="rdoc-time">更新于 {{ fmtTime(d.updated_at) }}</span>
                                        </div>
                                    </div>
                                </el-option>
                            </el-select>
                            <div style="font-size: 12px; color: #909399; margin-top: 4px;">
                                没有 external_id 的文档也能选择替换（按 document ID 定位）。
                            </div>
                        </el-form-item>

                        <el-form-item :label="mode === 'replace' ? '选择用于替换的新文件' : '选择要导入的文件（可多选）'" required>
                            <input type="file" ref="fileInput" style="display:none" :multiple="mode === 'add'" @change="onFileChange" accept=".txt,.md,.json,.csv,.doc,.docx,.xlsx,.pdf" />
                            <div style="display: flex; flex-direction: column; gap: 8px;">
                                <div>
                                    <el-button type="primary" @click="triggerFileSelect" :disabled="uploading || loading">
                                        {{ mode === 'add' ? '选择本地文件（可多选）' : '选择本地文件' }}
                                    </el-button>
                                </div>
                                <div v-if="mode === 'replace' && selectedFile" style="background:#f5f7fa; padding: 10px; border-radius:4px; font-size:13px; line-height: 1.5;">
                                    文件名: <b>{{ selectedFile.name }}</b> <br/>
                                    大小: {{ formatSize(selectedFile.size) }}
                                </div>
                                <div style="font-size: 12px; color: #909399; margin-top: 4px;">
                                    支持: <b>.txt, .md, .json, .csv, .docx, .xlsx, .pdf</b><br/>
                                    - .txt/.md：单篇文档，自动分片；<br/>
                                    - .json：文档对象或对象数组 (需含 text 字段)；<br/>
                                    - .csv：每行一篇文档 (首列或 text/content 列作正文)；<br/>
                                    - .docx：Word 段落 + 表格 (开启「图片 OCR」时内嵌图片也识别)；<br/>
                                    - .xlsx：每工作表表格切分 (旧版 .xls 请另存为 .xlsx)；<br/>
                                    - .pdf：提取文字层；该库开启「图片 OCR」后，扫描页/图片页逐页渲染并 OCR (带【第 N 页】标记)。<br/>
                                    <span style="color:#e6a23c;">.doc / .xls 旧格式暂不支持，请在 Word / Excel 中『另存为』.docx / .xlsx 后再上传。</span><br/>
                                    <span v-if="mode === 'replace'" style="color:#e6a23c;">替换仅支持解析为单篇文档的文件 (JSON 数组 / CSV 多行不可用于替换)。</span>
                                </div>
                            </div>
                        </el-form-item>

                        <el-collapse v-if="mode === 'add'" style="margin-bottom: 16px;">
                            <el-collapse-item title="高级设置">
                                <el-form-item label="外部 ID (external_id)">
                                    <el-input v-model="externalId" placeholder="留空即可；主要供外部系统同步使用" clearable :disabled="uploading" />
                                    <div style="font-size: 12px; color: #909399; margin-top: 4px;">
                                        <b>仅适用于单文件 upsert</b>：重复上传同一 external_id 会覆盖更新对应文档。
                                        多文件上传时请留空——填了 external_id 将禁止多文件上传。
                                    </div>
                                    <div v-if="multiBlockedByExtId" style="font-size: 12px; color: #f56c6c; margin-top: 4px;">
                                        external_id 只适用于单文件 upsert，已禁止多文件上传。请清空 external_id 或仅保留 1 个文件。
                                    </div>
                                </el-form-item>
                            </el-collapse-item>
                        </el-collapse>

                        <!-- 新增模式：多文件队列操作 -->
                        <template v-if="mode === 'add'">
                            <el-form-item style="margin-top: 10px;">
                                <div style="display:flex; gap:8px; width:100%;">
                                    <el-button type="success" :disabled="!canStart" :loading="uploading" @click="runQueue(false)" style="flex:1;">
                                        开始上传<span v-if="pendingCount"> ({{ pendingCount }})</span>
                                    </el-button>
                                    <el-button :disabled="!hasFailed || uploading" @click="runQueue(true)">仅重试失败</el-button>
                                    <el-button :disabled="!queue.length || uploading" @click="clearQueue">清空列表</el-button>
                                </div>
                            </el-form-item>
                        </template>

                        <!-- 替换模式：单文件按钮 -->
                        <el-form-item v-else style="margin-top: 10px;">
                            <el-button type="warning"
                                       :disabled="!selectedFile || !replaceDocId"
                                       @click="handleReplace" :loading="loading" style="width: 100%">
                                替换该文档
                            </el-button>
                        </el-form-item>
                    </el-form>
                </el-card>
            </el-col>

            <el-col :span="11">
                <!-- 新增模式：上传队列 -->
                <el-card v-if="mode === 'add'">
                    <template #header>
                        <div class="card-header">
                            <span>上传队列</span>
                            <span style="font-size:12px; color:#909399;">共 {{ queue.length }} 个文件</span>
                        </div>
                    </template>

                    <el-alert v-if="ranOnce && !uploading" type="success" :closable="false" style="margin-bottom:12px"
                              :title="'上传结束：成功 ' + summary.submitted + '，跳过 ' + summary.skipped + '，失败 ' + summary.failed + '。可到「文档」或「任务监控」查看向量化状态。'" />

                    <el-table v-if="queue.length" :data="queue" border size="small" style="width: 100%">
                        <el-table-column type="index" label="#" width="44" align="center" />
                        <el-table-column prop="name" label="文件名" min-width="180" show-overflow-tooltip />
                        <el-table-column label="大小" width="90" align="right">
                            <template #default="{row}">{{ formatSize(row.size) }}</template>
                        </el-table-column>
                        <el-table-column label="状态" width="92" align="center">
                            <template #default="{row}">
                                <el-tag size="small" :type="ST_TAG[row.status]">{{ ST_LABEL[row.status] }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column prop="error" label="错误原因" min-width="160" show-overflow-tooltip>
                            <template #default="{row}"><span style="color:#f56c6c;">{{ row.error }}</span></template>
                        </el-table-column>
                    </el-table>

                    <div v-else style="text-align:center; color:#909399; padding: 40px 0;">
                        <p style="font-size: 15px;">队列为空</p>
                        <p style="font-size: 13px;">点击左侧「选择本地文件（可多选）」添加文件，再点「开始上传」。</p>
                    </div>
                </el-card>

                <!-- 替换模式：单文件结果 -->
                <el-card v-else-if="importResult">
                    <template #header>
                        <div class="card-header"><span>导入结果反馈</span></div>
                    </template>

                    <el-alert title="已完成！后台 Worker 正在异步生成向量，可在「任务监控」页查看进度。" type="success" :closable="false" style="margin-bottom:16px" />

                    <div style="margin-bottom: 12px; font-size: 14px;">
                        成功处理文档数: <b>{{ importResult.imported_count }}</b>
                    </div>

                    <el-table :data="importResult.documents" border size="small" style="width: 100%">
                        <el-table-column prop="title" label="文档标题" min-width="160" show-overflow-tooltip />
                        <el-table-column label="操作" width="90" align="center">
                            <template #default="{row}">
                                <el-tag size="small" :type="OP_TAG[row.operation] || 'info'">
                                    {{ OP_LABEL[row.operation] || row.operation || '—' }}
                                </el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column prop="chunk_count" label="分片数" width="80" align="center" />
                        <el-table-column prop="status" label="状态" width="90" align="center">
                            <template #default="{row}"><el-tag size="small" type="info">{{ row.status }}</el-tag></template>
                        </el-table-column>
                        <el-table-column prop="document_id" label="文档 ID" width="110">
                            <template #default="{row}"><span class="mono">{{ row.document_id.slice(0, 8) }}…</span></template>
                        </el-table-column>
                    </el-table>
                </el-card>

                <el-card v-else style="height: 100%; display: flex; align-items: center; justify-content: center; text-align: center; color: #909399; min-height: 300px;">
                    <div>
                        <p style="font-size: 16px;">暂无导入记录</p>
                        <p style="font-size: 13px;">选择目标库与上传方式，然后上传文件。</p>
                    </div>
                </el-card>
            </el-col>
        </el-row>
    </div>
    `
};
