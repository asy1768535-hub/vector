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
                embedding_base_url: '', embed_batch_size: null, rerank_enabled: null, ocr_enabled: null,
                docx_table_aware: null, retrieval_mode: 'dense',
                chunk_size: 1000, chunk_overlap: 120,
            },
        });
        const edit = reactive({
            open: false, slug: '', initial: null,
            form: {
                name: '', description: '',
                embedding_model: '', embedding_dim: null, vector_distance: 'cosine',
                embedding_base_url: '', embed_batch_size: null, rerank_enabled: null, ocr_enabled: null,
                docx_table_aware: null, retrieval_mode: 'dense',
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
                embedding_base_url: '', embed_batch_size: null, rerank_enabled: null, ocr_enabled: null,
                docx_table_aware: null, retrieval_mode: 'dense',
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
            if (!body.embed_batch_size) body.embed_batch_size = null;
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
                embed_batch_size: row.embed_batch_size ?? null,
                rerank_enabled: row.rerank_enabled ?? null,
                ocr_enabled: row.ocr_enabled ?? null,
                docx_table_aware: row.docx_table_aware ?? null,
                retrieval_mode: row.retrieval_mode || 'dense',
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

        async function testEmbedding(row) {
            try {
                const r = await api.testLibraryEmbedding(row.slug);
                if (r.ok && r.message === 'ok') {
                    ElMessage.success(`「${row.slug}」embedding 正常：${r.embedding_model} / ${r.dim}维`);
                } else if (r.ok) {
                    ElMessage.warning(`「${row.slug}」可达但有问题：${r.message}`);
                } else {
                    ElMessageBox.alert(r.message, `「${row.slug}」embedding 测试失败`, { type: 'error' });
                }
            } catch (e) { ElMessage.error(e.message || String(e)); }
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

        // ── 常用问题（FAQ）管理 ──────────────────────────────────
        const faqMgr = reactive({
            open: false, slug: '', loading: false, list: [],
            newQuestion: '', newSort: 0,
        });

        async function loadFaq() {
            faqMgr.loading = true;
            try {
                // 管理端拉全部（含停用），便于管理员维护
                faqMgr.list = await api.listLibraryFaqs(faqMgr.slug, { includeInactive: true });
            } catch (e) { ElMessage.error(e.message); }
            finally { faqMgr.loading = false; }
        }

        async function openFaq(row) {
            faqMgr.slug = row.slug;
            faqMgr.newQuestion = '';
            faqMgr.newSort = 0;
            faqMgr.list = [];
            faqMgr.open = true;
            await loadFaq();
        }

        async function addFaq() {
            const q = (faqMgr.newQuestion || '').trim();
            if (!q) { ElMessage.warning('请输入问题'); return; }
            try {
                await api.createLibraryFaq(faqMgr.slug, { question: q, sort_order: faqMgr.newSort || 0, is_active: true });
                faqMgr.newQuestion = '';
                faqMgr.newSort = 0;
                ElMessage.success('已新增');
                loadFaq();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function saveFaq(row) {
            const q = (row.question || '').trim();
            if (!q) { ElMessage.warning('问题不能为空'); return; }
            try {
                await api.updateLibraryFaq(faqMgr.slug, row.id, {
                    question: q, sort_order: row.sort_order, is_active: row.is_active,
                });
                ElMessage.success('已保存');
                loadFaq();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function removeFaq(row) {
            try {
                await ElMessageBox.confirm(`删除常用问题：「${row.question}」？`, '确认', { type: 'warning' });
                await api.deleteLibraryFaq(faqMgr.slug, row.id);
                ElMessage.success('已删除');
                loadFaq();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        onMounted(load);
        return { libs, loading, showDeleted, create, edit, openCreate, submitCreate, openEdit, submitEdit,
                 rebuild, del, testEmbedding, load, srcSummary,
                 faqMgr, openFaq, addFaq, saveFaq, removeFaq };
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
            <el-table-column label="检索模式" width="100">
                <template #default="{row}">
                    <el-tag :type="row.retrieval_mode === 'hybrid' ? 'warning' : 'info'" size="small" effect="plain">
                        {{ row.retrieval_mode === 'hybrid' ? '混合' : '向量' }}
                    </el-tag>
                </template>
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
            <el-table-column label="操作" width="420" fixed="right">
                <template #default="{row}">
                    <el-button size="small" type="success" plain :disabled="!!row.deleted_at" @click="testEmbedding(row)">测试</el-button>
                    <el-button size="small" :disabled="!!row.deleted_at" @click="openEdit(row)">编辑</el-button>
                    <el-button size="small" type="primary" plain :disabled="!!row.deleted_at" @click="openFaq(row)">常用问题</el-button>
                    <el-button size="small" type="warning" :disabled="!!row.deleted_at" @click="rebuild(row)">重建</el-button>
                    <el-button size="small" type="danger" :disabled="!!row.deleted_at" @click="del(row)">删除</el-button>
                </template>
            </el-table-column>
        </el-table>

        <!-- 新建对话框 -->
        <el-dialog v-model="create.open" title="新建库" width="780px" top="6vh">
            <el-form label-width="92px">
                <el-row :gutter="16">
                    <!-- 填写项 -->
                    <el-col :span="12">
                        <el-form-item label="库唯一ID" required>
                            <el-input v-model="create.form.slug" placeholder="小写字母/下划线，如 medical" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="名称" required>
                            <el-input v-model="create.form.name" placeholder="医学知识库" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="24">
                        <el-form-item label="描述">
                            <el-input v-model="create.form.description" type="textarea" :rows="2" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="向量模型">
                            <el-input v-model="create.form.embedding_model" placeholder="默认：bge-m3" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="接口地址">
                            <el-input v-model="create.form.embedding_base_url" placeholder="默认 .../v1/embeddings" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="向量维度">
                            <el-input-number v-model="create.form.embedding_dim" :min="64" :max="8192" style="width:100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="单批大小">
                            <el-input-number v-model="create.form.embed_batch_size" :min="1" :max="256" style="width:100%" />
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">留空=全局；阿里云填 10，本地 bge-m3 可填 32</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="分片大小">
                            <el-input-number v-model="create.form.chunk_size" :min="200" :max="8000" style="width:100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="分片重叠">
                            <el-input-number v-model="create.form.chunk_overlap" :min="0" :max="2000" style="width:100%" />
                        </el-form-item>
                    </el-col>

                    <!-- 选择项（下拉）-->
                    <el-col :span="24">
                        <el-divider content-position="left" style="margin:2px 0 14px;color:#909399;font-size:13px">检索与处理选项</el-divider>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="检索模式">
                            <el-select v-model="create.form.retrieval_mode" style="width:100%">
                                <el-option value="dense" label="向量检索 dense" />
                                <el-option value="hybrid" label="混合检索 hybrid" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">hybrid=向量+关键词(pg_trgm) RRF，对条款号/编号/专名更稳；无需重建</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="Rerank">
                            <el-select v-model="create.form.rerank_enabled" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">需先在 .env 配 RERANK_* 才生效</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="图片 OCR">
                            <el-select v-model="create.form.ocr_enabled" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">识别 DOCX 内嵌图片/PDF 扫描页（较慢）</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="docx表格">
                            <el-select v-model="create.form.docx_table_aware" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">每表单独成块带表头；表格重的库建议开</div>
                        </el-form-item>
                    </el-col>
                </el-row>
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
        <el-dialog v-model="edit.open" :title="'编辑库 / ' + edit.slug" width="780px" top="6vh">
            <el-alert type="warning" :closable="false" style="margin-bottom:12px">
                改 <b>向量维度</b> 会让 Qdrant 现有 collection 维度对不上，保存后请立刻点表格里「重建」。
                改 <b>向量模型</b> / <b>接口地址</b> 只影响之后新摄入的文档；已存在 chunk 不会自动重 embed。
            </el-alert>
            <el-form label-width="92px">
                <el-row :gutter="16">
                    <!-- 填写项 -->
                    <el-col :span="12">
                        <el-form-item label="名称">
                            <el-input v-model="edit.form.name" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="向量模型">
                            <el-input v-model="edit.form.embedding_model" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="24">
                        <el-form-item label="描述">
                            <el-input v-model="edit.form.description" type="textarea" :rows="2" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="24">
                        <el-form-item label="接口地址">
                            <el-input v-model="edit.form.embedding_base_url" placeholder="默认：http://10.0.10.2:8111/v1/embeddings" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="向量维度">
                            <el-input-number v-model="edit.form.embedding_dim" :min="64" :max="8192" style="width:100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="单批大小">
                            <el-input-number v-model="edit.form.embed_batch_size" :min="1" :max="256" style="width:100%" />
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">留空=全局；阿里云填 10，本地 bge-m3 可填 32</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="分片大小">
                            <el-input-number v-model="edit.form.chunk_size" :min="200" :max="8000" style="width:100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="分片重叠">
                            <el-input-number v-model="edit.form.chunk_overlap" :min="0" :max="2000" style="width:100%" />
                        </el-form-item>
                    </el-col>

                    <!-- 选择项（下拉）-->
                    <el-col :span="24">
                        <el-divider content-position="left" style="margin:2px 0 14px;color:#909399;font-size:13px">检索与处理选项</el-divider>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="检索模式">
                            <el-select v-model="edit.form.retrieval_mode" style="width:100%">
                                <el-option value="dense" label="向量检索 dense" />
                                <el-option value="hybrid" label="混合检索 hybrid" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">hybrid=向量+关键词 RRF；切换即时生效、无需重建</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="Rerank">
                            <el-select v-model="edit.form.rerank_enabled" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">需先在 .env 配 RERANK_* 才生效</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="图片 OCR">
                            <el-select v-model="edit.form.ocr_enabled" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">改开关只影响之后新上传/重灌的文档</div>
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="docx表格">
                            <el-select v-model="edit.form.docx_table_aware" style="width:100%">
                                <el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" />
                            </el-select>
                            <div style="font-size:12px;color:#909399;line-height:1.35;margin-top:2px">改开关只影响之后新上传/重灌的文档</div>
                        </el-form-item>
                    </el-col>
                </el-row>
            </el-form>
            <template #footer>
                <el-button @click="edit.open = false">取消</el-button>
                <el-button type="primary" @click="submitEdit">保存</el-button>
            </template>
        </el-dialog>

        <!-- 常用问题管理对话框 -->
        <el-dialog v-model="faqMgr.open" :title="'常用问题 / ' + faqMgr.slug" width="680px">
            <el-alert type="info" :closable="false" style="margin-bottom:12px">
                管理员在此维护高频问题；普通用户在「数据检索」页选到该库后会看到这些问题，点一下即可发起检索。停用的不会展示给用户。
            </el-alert>
            <div style="display:flex;gap:8px;margin-bottom:12px">
                <el-input v-model="faqMgr.newQuestion" placeholder="新增常用问题，如：八大员包括哪些岗位" @keyup.enter="addFaq" />
                <el-input-number v-model="faqMgr.newSort" :min="0" :max="9999" controls-position="right" style="width:120px" />
                <el-button type="primary" @click="addFaq">新增</el-button>
            </div>
            <el-table :data="faqMgr.list" v-loading="faqMgr.loading" border size="small">
                <el-table-column label="排序" width="110">
                    <template #default="{row}">
                        <el-input-number v-model="row.sort_order" :min="0" :max="9999" size="small" controls-position="right" style="width:92px" />
                    </template>
                </el-table-column>
                <el-table-column label="问题">
                    <template #default="{row}"><el-input v-model="row.question" size="small" /></template>
                </el-table-column>
                <el-table-column label="启用" width="80" align="center">
                    <template #default="{row}"><el-switch v-model="row.is_active" /></template>
                </el-table-column>
                <el-table-column label="操作" width="160">
                    <template #default="{row}">
                        <el-button size="small" type="primary" plain @click="saveFaq(row)">保存</el-button>
                        <el-button size="small" type="danger" plain @click="removeFaq(row)">删除</el-button>
                    </template>
                </el-table-column>
            </el-table>
            <el-empty v-if="!faqMgr.loading && !faqMgr.list.length" description="暂无常用问题" :image-size="60" />
            <template #footer>
                <el-button @click="faqMgr.open = false">关闭</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
