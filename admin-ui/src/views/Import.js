import { computed, onMounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';

const OP_LABEL = { created: '新建', updated: '已更新', unchanged: '未变更' };
const OP_TAG = { created: 'success', updated: 'warning', unchanged: 'info' };

export default {
    setup() {
        const libs = ref([]);
        const slug = ref(null);
        const mode = ref('add');           // 'add'（默认）| 'replace'
        const fileInput = ref(null);
        const selectedFile = ref(null);
        const externalId = ref('');
        const replaceDocId = ref(null);    // 替换模式：目标 document ID
        const docs = ref([]);              // 替换模式：当前库的活动文档
        const docsLoading = ref(false);
        const loading = ref(false);
        const importResult = ref(null);
        const docQuery = ref('');          // 替换下拉的搜索串（自定义 filter-method）

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
                // 列出当前库活动文档（默认不含已删）；上限放宽，普通库足够
                docs.value = await api.listDocuments(slug.value, { limit: 500 });
            } catch (e) {
                ElMessage.error('加载文档列表失败：' + (e.message || e));
                docs.value = [];
            } finally {
                docsLoading.value = false;
            }
        }

        // 切库 / 进入替换模式时，刷新可替换文档并清空已选目标
        watch([slug, mode], () => {
            importResult.value = null;
            if (mode.value === 'replace') {
                replaceDocId.value = null;
                loadDocs();
            }
        });

        function triggerFileSelect() {
            if (fileInput.value) fileInput.value.click();
        }

        function onFileChange(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                selectedFile.value = files[0];
                importResult.value = null;
            }
        }

        function fmtTime(t) {
            if (!t) return '—';
            try { return new Date(t).toLocaleString(); } catch (e) { return t; }
        }

        // 自定义过滤：按文件名 / external_id / 短 document ID 搜索（不依赖把搜索串塞进 label）
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
        // 每次打开下拉重置搜索串，保证显示全部可选文档
        function onReplaceVisible(v) {
            if (v) docQuery.value = '';
        }

        function selectedDoc() {
            return docs.value.find((d) => d.id === replaceDocId.value) || null;
        }

        function reportTip(resp) {
            const ops = (resp.documents || []).map((d) => d.operation).filter(Boolean);
            if (mode.value === 'add' && ops.length && ops.every((o) => o === 'unchanged')) {
                ElMessage.warning('相同内容已存在，未重复新增');
            } else if (mode.value === 'replace') {
                ElMessage.success('替换成功：revision 已增加，旧内容已停止检索');
            } else {
                ElMessage.success(`导入成功！共处理 ${resp.imported_count} 篇文档。`);
            }
        }

        async function doUpload(opts) {
            loading.value = true;
            try {
                const resp = await api.importFile(slug.value, selectedFile.value, opts);
                importResult.value = resp;
                reportTip(resp);
                // 清理选择
                selectedFile.value = null;
                if (fileInput.value) fileInput.value.value = '';
                if (mode.value === 'add') externalId.value = '';
                if (mode.value === 'replace') { replaceDocId.value = null; loadDocs(); }
            } catch (e) {
                ElMessage.error(e.message || '操作失败，请检查文件格式');
            } finally {
                loading.value = false;
            }
        }

        async function handleImport() {
            if (!slug.value) { ElMessage.warning('请选择目标库'); return; }
            if (!selectedFile.value) { ElMessage.warning('请先选择要上传的文件'); return; }

            if (mode.value === 'replace') {
                if (!replaceDocId.value) {
                    ElMessage.warning('请先选择要替换的已有文档');
                    return;
                }
                const target = selectedDoc();
                const name = (target && (target.title || target.external_id)) || replaceDocId.value.slice(0, 8);
                try {
                    await ElMessageBox.confirm(
                        `将用新文件替换《${name}》，revision 将增加，旧内容会停止检索。`,
                        '确认替换',
                        { confirmButtonText: '确认替换', cancelButtonText: '取消', type: 'warning' },
                    );
                } catch (e) {
                    return; // 用户取消
                }
                await doUpload({ replaceDocumentId: replaceDocId.value });
            } else {
                await doUpload({ externalId: externalId.value.trim() || null });
            }
        }

        function formatSize(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        onMounted(loadLibs);

        return {
            libs, slug, mode, fileInput, selectedFile, externalId,
            replaceDocId, docs, docsLoading, loading, importResult,
            triggerFileSelect, onFileChange, handleImport, formatSize,
            fmtTime, displayDocs, filterDocs, onReplaceVisible, OP_LABEL, OP_TAG,
        };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>导入数据</h2>
        </div>

        <el-row :gutter="20">
            <el-col :span="10">
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
                            <el-radio-group v-model="mode">
                                <el-radio-button label="add">新增文档</el-radio-button>
                                <el-radio-button label="replace">替换已有文档</el-radio-button>
                            </el-radio-group>
                            <div style="font-size: 12px; color: #909399; margin-top: 6px;">
                                <span v-if="mode === 'add'">新增：按内容/external_id 去重，已存在的相同内容不会重复入库。</span>
                                <span v-else>替换：选中一篇已有文档，用新文件覆盖它（revision +1，旧内容立即停止检索）。</span>
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

                        <el-form-item :label="mode === 'replace' ? '选择用于替换的新文件' : '选择要导入的文件'" required>
                            <input type="file" ref="fileInput" style="display:none" @change="onFileChange" accept=".txt,.md,.json,.csv,.docx,.xlsx,.pdf" />
                            <div style="display: flex; flex-direction: column; gap: 8px;">
                                <div><el-button type="primary" @click="triggerFileSelect">选择本地文件</el-button></div>
                                <div v-if="selectedFile" style="background:#f5f7fa; padding: 10px; border-radius:4px; font-size:13px; line-height: 1.5;">
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
                                    <span v-if="mode === 'replace'" style="color:#e6a23c;">替换仅支持解析为单篇文档的文件 (JSON 数组 / CSV 多行不可用于替换)。</span>
                                </div>
                            </div>
                        </el-form-item>

                        <el-collapse v-if="mode === 'add'" style="margin-bottom: 16px;">
                            <el-collapse-item title="高级设置">
                                <el-form-item label="外部 ID (external_id)">
                                    <el-input v-model="externalId" placeholder="留空即可；主要供外部系统同步使用" clearable />
                                    <div style="font-size: 12px; color: #909399; margin-top: 4px;">
                                        主要供<b>外部系统幂等同步</b>：重复上传同一 external_id 会覆盖更新对应文档。
                                        普通用户新增文档无需填写；想替换已有文档请改用上方「替换已有文档」。
                                    </div>
                                </el-form-item>
                            </el-collapse-item>
                        </el-collapse>

                        <el-form-item style="margin-top: 10px;">
                            <el-button :type="mode === 'replace' ? 'warning' : 'success'"
                                       :disabled="!selectedFile || (mode === 'replace' && !replaceDocId)"
                                       @click="handleImport" :loading="loading" style="width: 100%">
                                {{ mode === 'replace' ? '替换该文档' : '开始导入' }}
                            </el-button>
                        </el-form-item>
                    </el-form>
                </el-card>
            </el-col>

            <el-col :span="14">
                <el-card v-if="importResult">
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
