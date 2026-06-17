import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

// PGSQL 全文源摘要（用于列表展示已存储的 source_config）
function srcSummary(cfg) {
    if (!cfg) return '(无)';
    const tbl = cfg.db_name ? `${cfg.db_name}.${cfg.table}` : cfg.table;
    return `${tbl} · ${cfg.key_field}→${cfg.text_column}`;
}

export default {
    setup() {
        const libs = ref([]);
        const loading = ref(false);
        const showDeleted = ref(false);
        const create = reactive({
            open: false,
            form: {
                slug: '', name: '', description: '',
                embedding_model: '', embedding_dim: null, vector_distance: 'cosine',
                embedding_base_url: '',
                chunk_size: 1000, chunk_overlap: 120,
            },
        });
        const edit = reactive({
            open: false, slug: '', initial: null,
            form: {
                name: '', description: '',
                embedding_model: '', embedding_dim: null, vector_distance: 'cosine',
                embedding_base_url: '',
                chunk_size: 1000, chunk_overlap: 120,
            },
        });

        async function load() {
            loading.value = true;
            try {
                const params = showDeleted.value ? { include_deleted: 'true' } : {};
                libs.value = await api.listLibraries(params);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function openCreate() {
            create.form = {
                slug: '', name: '', description: '',
                embedding_model: '', embedding_dim: null, vector_distance: 'cosine',
                embedding_base_url: '',
                chunk_size: 1000, chunk_overlap: 120,
            };
            create.open = true;
        }

        async function submitCreate() {
            // 把空字符串改成 null，让后端用全局默认
            const body = { ...create.form };
            for (const k of ['embedding_model', 'embedding_base_url']) {
                if (!body[k]) body[k] = null;
            }
            if (!body.embedding_dim) body.embedding_dim = null;
            // 全文源由后端按约定自动生成，无需前端传
            try {
                await api.createLibrary(body);
                ElMessage.success('库已创建并已建 Qdrant collection');
                create.open = false;
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        function openEdit(row) {
            edit.slug = row.slug;
            const snapshot = {
                name: row.name,
                description: row.description || '',
                embedding_model: row.embedding_model,
                embedding_dim: row.embedding_dim,
                vector_distance: row.vector_distance,
                embedding_base_url: row.embedding_base_url || '',
                chunk_size: row.chunk_size,
                chunk_overlap: row.chunk_overlap,
            };
            edit.form = { ...snapshot };
            edit.initial = snapshot;
            edit.open = true;
        }

        async function submitEdit() {
            const diff = {};
            for (const k of Object.keys(edit.form)) {
                if (edit.form[k] !== edit.initial[k]) diff[k] = edit.form[k];
            }
            if (Object.keys(diff).length === 0) {
                ElMessage.info('未做任何修改');
                edit.open = false;
                return;
            }
            // 改动了 dim / distance ? 强提示要重建
            const structural = diff.embedding_dim !== undefined;
            if (structural) {
                try {
                    await ElMessageBox.confirm(
                        '改了 embedding_dim ⚠️\n'
                        + 'Qdrant collection 结构已固定，保存后之后的新 embed 会维度不匹配。\n'
                        + '保存后请立刻点「重建 collection」按钮，否则后续摄入会失败。\n\n'
                        + '继续保存吗？',
                        '危险操作', { type: 'warning', confirmButtonText: '我知道风险，保存', cancelButtonText: '取消' }
                    );
                } catch (_) { return; }
            }
            try {
                await api.updateLibrary(edit.slug, diff);
                ElMessage.success('已保存');
                edit.open = false;
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function rebuild(row) {
            try {
                await ElMessageBox.confirm(
                    `确认重建 ${row.slug} 的 Qdrant collection？\n\n`
                    + '会删旧 collection、按当前 PG 参数建新 collection，\n'
                    + '所有该库文档与 embedding_jobs 重置为 pending，worker 会重新 embed 全部。\n\n'
                    + '过程可能耗时（取决于文档量与 embed 速度）。',
                    '重建 collection', { type: 'warning' }
                );
                await api.rebuildLibraryCollection(row.slug);
                ElMessage.success('已重建并重置 jobs，worker 会重新 embed');
                load();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        async function del(row) {
            try {
                await ElMessageBox.confirm(
                    `软删除库 ${row.slug}? Qdrant collection 也会异步清理。`,
                    '确认', { type: 'warning' }
                );
                await api.deleteLibrary(row.slug);
                ElMessage.success('已删除');
                load();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        onMounted(load);
        return { libs, loading, showDeleted, create, edit, openCreate, submitCreate, openEdit, submitEdit,
                 rebuild, del, load, srcSummary };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>库管理</h2>
            <div>
                <el-checkbox v-model="showDeleted" @change="load" style="margin-right:12px">显示已删除</el-checkbox>
                <el-button @click="load" :loading="loading">刷新</el-button>
                <el-button type="primary" @click="openCreate">新建库</el-button>
            </div>
        </div>
        <el-table :data="libs" v-loading="loading" border>
            <el-table-column prop="slug" label="库唯一ID" width="180" />
            <el-table-column prop="name" label="名称" width="180" />
            <el-table-column prop="description" label="描述" show-overflow-tooltip />
            <el-table-column label="向量参数" width="220">
                <template #default="{row}">
                    <span class="mono">{{ row.embedding_model }} / {{ row.embedding_dim }}d / {{ row.vector_distance }}</span>
                </template>
            </el-table-column>
            <el-table-column label="模型接口地址" width="240" show-overflow-tooltip>
                <template #default="{row}">
                    <span class="mono">{{ row.embedding_base_url || '(全局默认)' }}</span>
                </template>
            </el-table-column>
            <el-table-column label="切分" width="120">
                <template #default="{row}">{{ row.chunk_size }} / {{ row.chunk_overlap }}</template>
            </el-table-column>
            <el-table-column label="Qdrant Collection" width="200">
                <template #default="{row}"><span class="mono">{{ row.qdrant_collection }}</span></template>
            </el-table-column>
            <el-table-column label="全文源 (PGSQL)" width="240" show-overflow-tooltip>
                <template #default="{row}">
                    <el-tag v-if="row.source_config" type="success" size="small" effect="plain" class="mono">
                        {{ srcSummary(row.source_config) }}
                    </el-tag>
                    <span v-else style="color:#909399">向量自带正文</span>
                </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
                <template #default="{row}">
                    <el-tag :type="row.deleted_at ? 'info' : 'success'" size="small">
                        {{ row.deleted_at ? '已删' : '正常' }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column label="操作" width="260" fixed="right">
                <template #default="{row}">
                    <el-button size="small" :disabled="!!row.deleted_at" @click="openEdit(row)">编辑</el-button>
                    <el-button size="small" type="warning" :disabled="!!row.deleted_at" @click="rebuild(row)">重建</el-button>
                    <el-button size="small" type="danger" :disabled="!!row.deleted_at" @click="del(row)">删除</el-button>
                </template>
            </el-table-column>
        </el-table>

        <!-- 新建对话框 -->
        <el-dialog v-model="create.open" title="新建库" width="560px">
            <el-form label-width="140px">
                <el-form-item label="库唯一ID" required>
                    <el-input v-model="create.form.slug" placeholder="只能大小写字母和下划线，如 medical / legal_cn / CaseLib" />
                </el-form-item>
                <el-form-item label="名称" required>
                    <el-input v-model="create.form.name" placeholder="医学知识库" />
                </el-form-item>
                <el-form-item label="描述">
                    <el-input v-model="create.form.description" type="textarea" :rows="2" />
                </el-form-item>
                <el-form-item label="向量模型">
                    <el-input v-model="create.form.embedding_model" placeholder="默认：bge-m3" />
                </el-form-item>
                <el-form-item label="向量维度">
                    <el-input-number v-model="create.form.embedding_dim" :min="64" :max="8192" placeholder="默认：1024" />
                </el-form-item>
                <el-form-item label="模型接口地址">
                    <el-input v-model="create.form.embedding_base_url"
                              placeholder="默认：http://10.0.10.2:8111/v1/embeddings" />
                </el-form-item>
                <el-form-item label="分片大小">
                    <el-input-number v-model="create.form.chunk_size" :min="200" :max="8000" />
                </el-form-item>
                <el-form-item label="分片重叠">
                    <el-input-number v-model="create.form.chunk_overlap" :min="0" :max="2000" />
                </el-form-item>

                <el-alert type="info" :closable="false" style="margin-top:4px">
                    PGSQL 全文源按<b>约定</b>自动配置：源表 =
                    <code>{{ create.form.slug || '<库唯一ID>' }}</code>（本库唯一ID）·
                    外键 <code>text_id</code> → 正文列 <code>content</code> · <code>bigint</code> ·
                    源库取 <code>.env</code> 配置。
                </el-alert>
            </el-form>
            <template #footer>
                <el-button @click="create.open = false">取消</el-button>
                <el-button type="primary" @click="submitCreate">创建</el-button>
            </template>
        </el-dialog>

        <!-- 编辑对话框 -->
        <el-dialog v-model="edit.open" :title="'编辑库 / ' + edit.slug" width="560px">
            <el-alert type="warning" :closable="false" style="margin-bottom:12px">
                改 <b>向量维度 (embedding_dim)</b> 会让 Qdrant 现有 collection 维度对不上，
                保存后请立刻点表格里「重建」按钮重置整个库。
                改 <b>向量模型 (embedding_model)</b> / <b>模型接口地址 (embedding_base_url)</b> 只影响之后新摄入的文档；
                已存在 chunk 不会自动重新 embed。
            </el-alert>
            <el-form label-width="140px">
                <el-form-item label="名称">
                    <el-input v-model="edit.form.name" />
                </el-form-item>
                <el-form-item label="描述">
                    <el-input v-model="edit.form.description" type="textarea" :rows="2" />
                </el-form-item>
                <el-form-item label="向量模型">
                    <el-input v-model="edit.form.embedding_model" />
                </el-form-item>
                <el-form-item label="向量维度">
                    <el-input-number v-model="edit.form.embedding_dim" :min="64" :max="8192" />
                </el-form-item>
                <el-form-item label="模型接口地址">
                    <el-input v-model="edit.form.embedding_base_url"
                              placeholder="默认：http://10.0.10.2:8111/v1/embeddings" />
                </el-form-item>
                <el-form-item label="分片大小">
                    <el-input-number v-model="edit.form.chunk_size" :min="200" :max="8000" />
                </el-form-item>
                <el-form-item label="分片重叠">
                    <el-input-number v-model="edit.form.chunk_overlap" :min="0" :max="2000" />
                </el-form-item>

            </el-form>
            <template #footer>
                <el-button @click="edit.open = false">取消</el-button>
                <el-button type="primary" @click="submitEdit">保存</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
