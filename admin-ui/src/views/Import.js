import { onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';

export default {
    setup() {
        const libs = ref([]);
        const slug = ref(null);
        const fileInput = ref(null);
        const selectedFile = ref(null);
        const loading = ref(false);
        const importResult = ref(null);

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

        function triggerFileSelect() {
            if (fileInput.value) {
                fileInput.value.click();
            }
        }

        function onFileChange(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                selectedFile.value = files[0];
                importResult.value = null; // Clear previous result
            }
        }

        async function handleImport() {
            if (!slug.value) {
                ElMessage.warning('请选择要导入的目标库');
                return;
            }
            if (!selectedFile.value) {
                ElMessage.warning('请先选择要上传的文件');
                return;
            }
            loading.value = true;
            try {
                const resp = await api.importFile(slug.value, selectedFile.value);
                importResult.value = resp;
                ElMessage.success(`导入成功！共导入 ${resp.imported_count} 篇文档。`);
                // Clear selection
                selectedFile.value = null;
                if (fileInput.value) {
                    fileInput.value.value = '';
                }
            } catch (e) {
                ElMessage.error(e.message || '导入失败，请检查文件格式');
            } finally {
                loading.value = false;
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
            libs,
            slug,
            fileInput,
            selectedFile,
            loading,
            importResult,
            triggerFileSelect,
            onFileChange,
            handleImport,
            formatSize
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
                        <div class="card-header">
                            <span>文件导入</span>
                        </div>
                    </template>

                    <el-form label-position="top">
                        <el-form-item label="选择目标库" required>
                            <el-select v-model="slug" placeholder="请选择库" style="width: 100%">
                                <el-option v-for="l in libs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                            </el-select>
                        </el-form-item>

                        <el-form-item label="选择要导入的文件" required>
                            <input type="file" ref="fileInput" style="display:none" @change="onFileChange" accept=".txt,.md,.json,.csv" />
                            <div style="display: flex; flex-direction: column; gap: 8px;">
                                <div>
                                    <el-button type="primary" @click="triggerFileSelect">选择本地文件</el-button>
                                </div>
                                <div v-if="selectedFile" style="background:#f5f7fa; padding: 10px; border-radius:4px; font-size:13px; line-height: 1.5;">
                                    文件名: <b>{{ selectedFile.name }}</b> <br/>
                                    大小: {{ formatSize(selectedFile.size) }}
                                </div>
                                <div style="font-size: 12px; color: #909399; margin-top: 4px;">
                                    支持的文件类型: <b>.txt, .md, .json, .csv</b> <br/>
                                    - .txt/.md：作为单篇文档直接摄入并自动分片；<br/>
                                    - .json：支持文档对象或对象数组 (需包含 text 字段)；<br/>
                                    - .csv：将每行作为一篇文档导入 (首列或 text/content 列作为正文)。
                                </div>
                            </div>
                        </el-form-item>

                        <el-form-item style="margin-top: 30px;">
                            <el-button type="success" :disabled="!selectedFile" @click="handleImport" :loading="loading" style="width: 100%">
                                开始导入
                            </el-button>
                        </el-form-item>
                    </el-form>
                </el-card>
            </el-col>

            <el-col :span="14">
                <el-card v-if="importResult">
                    <template #header>
                        <div class="card-header">
                            <span>导入结果反馈</span>
                        </div>
                    </template>

                    <el-alert title="数据导入已完成！后台 Worker 正在异步生成向量，你可以稍后在「任务监控」页面查看生成状态。" type="success" :closable="false" style="margin-bottom:16px" />

                    <div style="margin-bottom: 12px; font-size: 14px;">
                        状态: <el-tag type="success">成功</el-tag> &nbsp;&nbsp;
                        成功摄入文档数: <b>{{ importResult.imported_count }}</b>
                    </div>

                    <el-table :data="importResult.documents" border size="small" style="width: 100%">
                        <el-table-column prop="title" label="文档标题" min-width="180" show-overflow-tooltip />
                        <el-table-column prop="chunk_count" label="分片数量" width="90" align="center" />
                        <el-table-column prop="status" label="摄入状态" width="100" align="center">
                            <template #default="{row}">
                                <el-tag size="small" type="info">{{ row.status }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column prop="document_id" label="文档 ID" width="100">
                            <template #default="{row}">
                                <span class="mono">{{ row.document_id.slice(0, 8) }}…</span>
                            </template>
                        </el-table-column>
                    </el-table>
                </el-card>

                <el-card v-else style="height: 100%; display: flex; align-items: center; justify-content: center; text-align: center; color: #909399; min-height: 300px;">
                    <div>
                        <p style="font-size: 16px;">暂无导入记录</p>
                        <p style="font-size: 13px;">请在左侧选择目标库并上传文件进行导入操作。</p>
                    </div>
                </el-card>
            </el-col>
        </el-row>
    </div>
    `
};
