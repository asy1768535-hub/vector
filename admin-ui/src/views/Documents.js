import { computed, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store, hasPermission } from '../store.js';
import { readableLibraries, resolveSelectedSlug } from '../menu_access.js';

export default {
    setup() {
        const libs = ref([]);
        const slug = ref(null);
        const docs = ref([]);
        const stats = ref(null);
        const loading = ref(false);
        const dialog = reactive({
            open: false,
            mode: 'create',   // 'create' | 'edit'
            docId: null,
            form: { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' },
        });

        const myLibs = computed(() =>
            store.user?.is_superuser
                ? libs.value
                : libs.value.filter((l) => hasPermission(l.slug, 'read'))
        );

        const canInsert = computed(() =>
            store.user?.is_superuser || hasPermission(slug.value, 'insert')
        );
        const canDelete = computed(() =>
            store.user?.is_superuser || hasPermission(slug.value, 'delete')
        );

        async function loadLibs() {
            try {
                // 超管 → /admin/libraries；非超管 → 仅「有 read 权限」的库（文档页是读类页面）。
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = readableLibraries(store.permissions);
                }
                // 选中库必须是当前可读库之一：当前 slug 失效 → 切到第一个；无可读库 → null（不发文档请求）。
                slug.value = resolveSelectedSlug(slug.value, libs.value);
            } catch (e) { ElMessage.error(e.message); }
        }

        async function loadDocs() {
            if (!slug.value) return;
            loading.value = true;
            try {
                const [d, s] = await Promise.all([
                    api.listDocuments(slug.value, { limit: 200 }),
                    api.libraryStats(slug.value),
                ]);
                docs.value = d;
                stats.value = s;
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        watch(slug, loadDocs);

        function openIngest() {
            dialog.mode = 'create';
            dialog.docId = null;
            dialog.form = { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' };
            dialog.open = true;
        }

        function openEdit(row) {
            dialog.mode = 'edit';
            dialog.docId = row.id;
            dialog.form = {
                title: row.title || '',
                external_id: row.external_id || '',
                text: '',
                splitter: 'text',
                metadata_json: row.metadata ? JSON.stringify(row.metadata) : '',
            };
            dialog.open = true;
        }

        async function submitIngest() {
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
                    external_id: dialog.form.external_id || null,
                    text: dialog.form.text,
                    splitter: dialog.form.splitter,
                    metadata,
                };
                const resp = dialog.mode === 'edit'
                    ? await api.updateDocument(slug.value, dialog.docId, body)
                    : await api.ingestDocument(slug.value, body);
                ElMessage.success(dialog.mode === 'edit'
                    ? `已更新并重新入队 ${resp.chunk_count} 个分片`
                    : `已入队 ${resp.chunk_count} 个分片 (doc_id=${resp.document_id.slice(0, 8)}…)`);
                dialog.open = false;
                loadDocs();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function del(row) {
            try {
                await ElMessageBox.confirm(`删除文档 "${row.title || row.id.slice(0, 8)}"?`, '确认', { type: 'warning' });
                await api.deleteDocument(slug.value, row.id);
                ElMessage.success('已删除 (Qdrant 异步清理)');
                loadDocs();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        onMounted(async () => {
            await loadLibs();
            await loadDocs();
        });

        return { myLibs, slug, docs, stats, loading, canInsert, canDelete, dialog,
                 loadDocs, openIngest, openEdit, submitIngest, del };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>文档</h2>
            <div>
                <el-select v-model="slug" placeholder="选择库" style="width:240px">
                    <el-option v-for="l in myLibs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                </el-select>
                <el-button @click="loadDocs" :loading="loading">刷新</el-button>
                <el-button type="primary" :disabled="!canInsert" @click="openIngest">提交文档</el-button>
            </div>
        </div>

        <el-row v-if="stats" :gutter="12" style="margin-bottom:16px">
            <el-col :span="4"><el-card><div>文档数</div><b style="font-size:24px">{{ stats.document_count }}</b></el-card></el-col>
            <el-col :span="4"><el-card><div>分片数</div><b style="font-size:24px">{{ stats.chunk_count }}</b></el-card></el-col>
            <el-col :span="4"><el-card><div>排队中</div><b style="font-size:24px">{{ stats.pending_jobs }}</b></el-card></el-col>
            <el-col :span="4"><el-card><div>处理中</div><b style="font-size:24px">{{ stats.processing_jobs }}</b></el-card></el-col>
            <el-col :span="4"><el-card><div>已完成</div><b style="font-size:24px;color:#67c23a">{{ stats.done_jobs }}</b></el-card></el-col>
            <el-col :span="4"><el-card><div>失败</div><b style="font-size:24px;color:#f56c6c">{{ stats.failed_jobs }}</b></el-card></el-col>
        </el-row>

        <el-table :data="docs" border v-loading="loading">
            <el-table-column label="ID" width="100">
                <template #default="{row}"><span class="mono">{{ row.id.slice(0, 8) }}…</span></template>
            </el-table-column>
            <el-table-column prop="title" label="标题" min-width="200" />
            <el-table-column prop="external_id" label="external_id" width="160" />
            <el-table-column label="状态" width="110">
                <template #default="{row}">
                    <el-tag :type="row.status === 'ready' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'" size="small">
                        {{ row.status }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column prop="content_hash" label="content_hash" width="140">
                <template #default="{row}"><span class="mono">{{ row.content_hash.slice(0, 12) }}…</span></template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="180" />
            <el-table-column label="操作" width="180" fixed="right">
                <template #default="{row}">
                    <el-button size="small" :disabled="!canInsert" @click="openEdit(row)">编辑</el-button>
                    <el-button size="small" type="danger" :disabled="!canDelete" @click="del(row)">删除</el-button>
                </template>
            </el-table-column>
        </el-table>

        <el-dialog v-model="dialog.open" :title="dialog.mode === 'edit' ? '编辑文档（整篇替换并重 embed）' : ('向 ' + slug + ' 提交文档')" width="640px">
            <el-alert v-if="dialog.mode === 'edit'" type="warning" :closable="false" style="margin-bottom:12px"
                      title="更新会用下面的正文整篇替换旧内容：删除旧分片与旧向量，重新切分并重新 embed。请粘贴完整的新正文。" />
            <el-form label-width="100px">
                <el-form-item label="标题"><el-input v-model="dialog.form.title" /></el-form-item>
                <el-form-item label="external_id">
                    <el-input v-model="dialog.form.external_id" :disabled="dialog.mode === 'edit'" placeholder="可选；用于幂等/upsert" />
                </el-form-item>
                <el-form-item label="切分方式">
                    <el-radio-group v-model="dialog.form.splitter">
                        <el-radio value="text">text</el-radio>
                        <el-radio value="markdown">markdown</el-radio>
                        <el-radio value="none">none (单一分片)</el-radio>
                    </el-radio-group>
                </el-form-item>
                <el-form-item label="metadata">
                    <el-input v-model="dialog.form.metadata_json" type="textarea" :rows="2"
                              placeholder='可选 JSON，例如 {"author":"...","year":2024}' />
                </el-form-item>
                <el-form-item label="正文" required>
                    <el-input v-model="dialog.form.text" type="textarea" :rows="10"
                              placeholder="粘贴文本 / Markdown / JSON 字符串" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" @click="submitIngest">{{ dialog.mode === 'edit' ? '保存并重新 embed' : '提交（异步 embed）' }}</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
