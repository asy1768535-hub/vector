import { computed, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { dataEmpty } from '../illustrations.js';
import {
    computeLibraryStats, filterLibraries, librarySlugFromName, libraryStatus, paginateLibraries,
    srcSummary,
} from '../libraries_ui.js';
import { createRequestFence, readProjection } from '../read_state_ui.js';

export default {
    setup() {
        const libs = ref([]);
        const loading = ref(false);
        const loadStarted = ref(false);
        const librariesResolved = ref(false);
        const librariesError = ref('');
        const librariesRequestFence = createRequestFence();
        const showDeleted = ref(false);
        const keyword = ref('');
        const statusFilter = ref('');
        const retrievalFilter = ref('');
        const page = ref(1);
        const pageSize = ref(10);
        const detailOpen = ref(false);
        const selectedLibrary = ref(null);

        const create = reactive({
            open: false,
            submitting: false,
            slugEdited: false,
            form: { slug: '', name: '', description: '', embedding_model: '', embedding_dim: 1024,
                vector_distance: 'cosine', embedding_base_url: '', embed_batch_size: 32,
                rerank_enabled: null, ocr_enabled: true, docx_table_aware: true,
                retrieval_mode: 'dense', source_enrichment_enabled: true,
                graph_extraction_enabled: false, graph_extraction_build_mode: 'standard',
                schema_template: 'none',
                chunk_size: 1000, chunk_overlap: 120 },
        });
        const edit = reactive({
            open: false, slug: '', initial: null, graphSecurityLevels: [],
            form: { name: '', description: '', embedding_model: '', embedding_dim: null,
                vector_distance: 'cosine', embedding_base_url: '', embed_batch_size: null,
                rerank_enabled: null, ocr_enabled: null, docx_table_aware: null,
                retrieval_mode: 'dense', source_enrichment_enabled: true,
                graph_extraction_enabled: false, graph_extraction_build_mode: 'standard',
                chunk_size: 1000, chunk_overlap: 120 },
        });

        const stats = computed(() => computeLibraryStats(libs.value));
        const filteredLibraries = computed(() => filterLibraries(libs.value, {
            keyword: keyword.value, status: statusFilter.value, retrievalMode: retrievalFilter.value,
        }));
        const pagedLibraries = computed(() => paginateLibraries(filteredLibraries.value, page.value, pageSize.value));

        watch([keyword, retrievalFilter, showDeleted, pageSize], () => { page.value = 1; });
        watch(statusFilter, async (value) => {
            page.value = 1;
            if (value === 'deleted' && !showDeleted.value) { showDeleted.value = true; await load(true); }
        });

        async function resetFilters() {
            keyword.value = ''; statusFilter.value = ''; retrievalFilter.value = '';
            page.value = 1;
            if (showDeleted.value) { showDeleted.value = false; await load(true); }
        }
        function openDetail(row) { selectedLibrary.value = row; detailOpen.value = true; }
        function toggleLabel(val) { if (val === true) return '开启'; if (val === false) return '关闭'; return '继承'; }
        function embedDisplay(row) { return (row.embedding_model || '全局默认') + ' / ' + (row.embedding_dim ? row.embedding_dim + 'd' : '—'); }
        function sourceDisplay(config) { return config ? `开启（${srcSummary(config)}）` : '关闭'; }

        async function load(forceRefresh = false) {
            const requestToken = librariesRequestFence.begin();
            loadStarted.value = true;
            loading.value = true;
            librariesError.value = '';
            try {
                const params = showDeleted.value ? { include_deleted: 'true' } : {};
                const result = await api.listLibraries(params, forceRefresh);
                if (!librariesRequestFence.isCurrent(requestToken)) return;
                libs.value = result;
                librariesResolved.value = true;
                if (selectedLibrary.value) {
                    selectedLibrary.value = libs.value.find((row) => row.slug === selectedLibrary.value.slug) || null;
                    if (!selectedLibrary.value) detailOpen.value = false;
                }
            } catch (e) {
                if (!librariesRequestFence.isCurrent(requestToken)) return;
                librariesError.value = e.message || '知识库列表加载失败';
            } finally {
                if (librariesRequestFence.isCurrent(requestToken)) loading.value = false;
            }
        }

        const librariesReadState = computed(() => readProjection({
            started: loadStarted.value,
            loading: loading.value,
            hasResolved: librariesResolved.value,
            empty: libs.value.length === 0,
            error: librariesError.value,
        }));

        function openCreate() {
            create.slugEdited = false;
            create.form = { slug: '', name: '', description: '', embedding_model: '', embedding_dim: 1024,
                vector_distance: 'cosine', embedding_base_url: '', embed_batch_size: 32,
                rerank_enabled: null, ocr_enabled: true, docx_table_aware: true,
                retrieval_mode: 'dense', source_enrichment_enabled: true,
                graph_extraction_enabled: false, graph_extraction_build_mode: 'standard',
                schema_template: 'none',
                chunk_size: 1000, chunk_overlap: 120 };
            create.open = true;
        }

        function onCreateNameInput(name) {
            if (!create.slugEdited) create.form.slug = librarySlugFromName(name);
        }

        function onCreateSlugInput() {
            create.slugEdited = true;
        }

        function onCreateGraphToggle(enabled) {
            if (enabled && create.form.schema_template === 'none') {
                create.form.schema_template = 'enterprise';
            }
            if (!enabled) create.form.schema_template = 'none';
        }

        async function submitCreate() {
            if (create.submitting) return;
            const body = { ...create.form };
            for (const k of ['embedding_model', 'embedding_base_url']) if (!body[k]) body[k] = null;
            if (!body.embedding_dim) body.embedding_dim = null;
            if (!body.embed_batch_size) body.embed_batch_size = null;
            body.external_llm_enabled = body.graph_extraction_enabled;
            body.graph_extraction_allowed_security_levels = body.graph_extraction_enabled
                ? ['internal']
                : [];
            create.submitting = true;
            try {
                const created = await api.createLibrary(body);
                const generation = Number(created.slug.match(/__r(\d+)$/)?.[1] || 1);
                const message = generation > 1
                    ? '同名知识库已按第 ' + generation + ' 次创建，新 ID：' + created.slug
                    : '知识库已创建，ID：' + created.slug;
                ElMessage.success(message);
                create.open = false;
                await load(true);
            }
            catch (e) { ElMessage.error(e.message); }
            finally { create.submitting = false; }
        }

        function openEdit(row) {
            edit.slug = row.slug;
            const snapshot = { name: row.name, description: row.description || '', embedding_model: row.embedding_model,
                embedding_dim: row.embedding_dim, vector_distance: row.vector_distance,
                embedding_base_url: row.embedding_base_url || '', embed_batch_size: row.embed_batch_size ?? null,
                rerank_enabled: row.rerank_enabled ?? null, ocr_enabled: row.ocr_enabled ?? null,
                docx_table_aware: row.docx_table_aware ?? null, retrieval_mode: row.retrieval_mode || 'dense',
                chunk_size: row.chunk_size, chunk_overlap: row.chunk_overlap,
                source_enrichment_enabled: row.source_config != null,
                graph_extraction_enabled: row.graph_extraction_enabled === true,
                graph_extraction_build_mode: row.graph_extraction_build_mode || 'standard' };
            edit.graphSecurityLevels = Array.isArray(row.graph_extraction_allowed_security_levels)
                ? [...row.graph_extraction_allowed_security_levels]
                : [];
            edit.form = { ...snapshot }; edit.initial = snapshot; edit.open = true;
        }

        async function submitEdit() {
            const diff = {};
            for (const k of Object.keys(edit.form)) if (edit.form[k] !== edit.initial[k]) diff[k] = edit.form[k];
            if (Object.keys(diff).length === 0) { ElMessage.info('未做任何修改'); edit.open = false; return; }
            if (diff.graph_extraction_enabled === true) {
                diff.external_llm_enabled = true;
                diff.graph_extraction_allowed_security_levels = edit.graphSecurityLevels.length
                    ? edit.graphSecurityLevels
                    : ['internal'];
            }
            if (diff.embedding_dim !== undefined || diff.vector_distance !== undefined) {
                try { await ElMessageBox.confirm('修改向量维度或距离算法后，现有 Qdrant collection 结构将不再匹配。\n保存后请立即执行「重建 collection」，否则后续摄入或检索可能失败。\n\n继续保存吗？', '危险操作', { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '取消' }); }
                catch (_) { return; }
            }
            try { await api.updateLibrary(edit.slug, diff); ElMessage.success('已保存'); edit.open = false; await load(true); }
            catch (e) { ElMessage.error(e.message); }
        }

        async function rebuild(row) {
            try { await ElMessageBox.confirm(`确认重建 ${row.slug} 的 Qdrant collection？\n\n会删旧 collection、按当前 PG 参数建新 collection，\n所有该库文档与 embedding_jobs 重置为 pending，worker 会重新 embed 全部。\n\n过程可能耗时（取决于文档量与 embed 速度）。`, '重建 collection', { type: 'warning' }); }
            catch (_) { return; }
            try { await api.rebuildLibraryCollection(row.slug); ElMessage.success('已重建并重置 jobs，worker 会重新 embed'); await load(true); }
            catch (e) { ElMessage.error(e.message || String(e)); }
        }

        async function testEmbedding(row) {
            try {
                const r = await api.testLibraryEmbedding(row.slug);
                if (r.ok && r.message === 'ok') ElMessage.success(`「${row.slug}」embedding 正常：${r.embedding_model} / ${r.dim}维`);
                else if (r.ok) ElMessage.warning(`「${row.slug}」可达但有问题：${r.message}`);
                else ElMessageBox.alert(r.message, `「${row.slug}」embedding 测试失败`, { type: 'error' });
            } catch (e) { ElMessage.error(e.message || String(e)); }
        }

        async function del(row) {
            try { await ElMessageBox.confirm(`软删除库 ${row.slug}? Qdrant collection 也会异步清理。`, '确认', { type: 'warning' }); }
            catch (_) { return; }
            try { await api.deleteLibrary(row.slug); ElMessage.success('已删除'); await load(true); }
            catch (e) { ElMessage.error(e.message || String(e)); }
        }

        const faqMgr = reactive({ open: false, slug: '', loading: false, error: '', list: [], newQuestion: '', newSort: 0 });
        const faqRequestFence = createRequestFence();
        async function loadFaq() {
            const requestToken = faqRequestFence.begin();
            const requestedSlug = faqMgr.slug;
            faqMgr.loading = true;
            faqMgr.error = '';
            try {
                const result = await api.listLibraryFaqs(requestedSlug, { includeInactive: true });
                if (!faqRequestFence.isCurrent(requestToken)) return;
                faqMgr.list = result;
            } catch (e) {
                if (!faqRequestFence.isCurrent(requestToken)) return;
                faqMgr.error = e.message || '常用问题加载失败';
            } finally {
                if (faqRequestFence.isCurrent(requestToken)) faqMgr.loading = false;
            }
        }
        async function openFaq(row) { faqMgr.slug = row.slug; faqMgr.newQuestion = ''; faqMgr.newSort = 0; faqMgr.list = []; faqMgr.open = true; await loadFaq(); }
        async function addFaq() { const q = (faqMgr.newQuestion || '').trim(); if (!q) { ElMessage.warning('请输入问题'); return; } try { await api.createLibraryFaq(faqMgr.slug, { question: q, sort_order: faqMgr.newSort || 0, is_active: true }); faqMgr.newQuestion = ''; faqMgr.newSort = 0; ElMessage.success('已新增'); loadFaq(); } catch (e) { ElMessage.error(e.message); } }
        async function saveFaq(row) { const q = (row.question || '').trim(); if (!q) { ElMessage.warning('问题不能为空'); return; } try { await api.updateLibraryFaq(faqMgr.slug, row.id, { question: q, sort_order: row.sort_order, is_active: row.is_active }); ElMessage.success('已保存'); loadFaq(); } catch (e) { ElMessage.error(e.message); } }
        async function removeFaq(row) { try { await ElMessageBox.confirm(`删除常用问题：「${row.question}」？`, '确认', { type: 'warning' }); } catch (_) { return; } try { await api.deleteLibraryFaq(faqMgr.slug, row.id); ElMessage.success('已删除'); loadFaq(); } catch (e) { ElMessage.error(e.message || String(e)); } }

        onMounted(() => load(false));

        return { libs, loading, librariesResolved, librariesError, librariesReadState, showDeleted, create, edit, stats, pagedLibraries, detailOpen, selectedLibrary,
            keyword, statusFilter, retrievalFilter, page, pageSize, resetFilters, openDetail,
            load, openCreate, submitCreate, openEdit, submitEdit, rebuild, del, testEmbedding,
            onCreateNameInput, onCreateSlugInput, onCreateGraphToggle,
            dataEmpty, srcSummary, sourceDisplay, libraryStatus, toggleLabel, embedDisplay,
            faqMgr, loadFaq, openFaq, addFaq, saveFaq, removeFaq };
    },
    template: `
    <div class="libraries-workspace">
      <header class="libraries-header">
        <div><h2 class="libraries-title">知识库管理</h2><p class="libraries-desc">管理知识库配置、检索模式与处理能力</p></div>
        <div class="libraries-header-actions">
          <el-button @click="load(true)" :loading="loading">刷新</el-button>
          <el-button type="primary" @click="openCreate">新建知识库</el-button>
        </div>
      </header>

      <section v-if="librariesReadState === 'fatal'" class="app-read-state app-read-state--error" role="alert">
        <div><strong>知识库列表加载失败</strong><p>{{ librariesError }}</p></div>
        <el-button type="primary" :loading="loading" @click="load(true)">重试</el-button>
      </section>

      <section v-else-if="librariesReadState === 'idle' || librariesReadState === 'loading'"
               class="app-read-state" v-loading="true">
        <span>正在加载知识库</span>
      </section>

      <template v-else>
      <el-alert v-if="librariesReadState === 'refresh-error'"
                type="warning" :closable="false" show-icon
                title="知识库列表刷新失败，当前仍显示上次成功加载的数据"
                :description="librariesError" />

      <section class="libraries-toolbar">
        <el-input v-model="keyword" class="libraries-search" clearable placeholder="搜索知识库名称或唯一 ID" />
        <el-select v-model="statusFilter" class="libraries-filter" placeholder="全部状态" clearable>
          <el-option label="正常" value="active" /><el-option label="已删除" value="deleted" />
        </el-select>
        <el-select v-model="retrievalFilter" class="libraries-filter" placeholder="全部检索模式" clearable>
          <el-option label="向量检索" value="dense" /><el-option label="混合检索" value="hybrid" />
        </el-select>
        <el-checkbox v-model="showDeleted" @change="load(true)">包含已删除</el-checkbox>
        <el-button @click="resetFilters">重置</el-button>
      </section>

      <section class="libraries-stats">
        <div class="libraries-stat"><local-icon icon="overview:kb-count" class="libraries-stat-icon" /><b>{{ stats.total }}</b><span>知识库总数</span></div>
        <div class="libraries-stat libraries-stat--active"><local-icon icon="overview:online-services" class="libraries-stat-icon" /><b>{{ stats.active }}</b><span>正常</span></div>
        <div class="libraries-stat libraries-stat--ocr"><span class="libraries-stat-text-icon">OCR</span><b>{{ stats.ocr }}</b><span>OCR 开启</span></div>
        <div class="libraries-stat libraries-stat--rerank"><local-icon icon="overview:service-status" class="libraries-stat-icon" /><b>{{ stats.rerank }}</b><span>Rerank 配置</span></div>
      </section>

      <section class="libraries-table-card">
        <div class="libraries-table-shell">
          <el-table :data="pagedLibraries.rows" v-loading="loading" row-key="slug" @row-click="openDetail">
            <template #empty>
              <div v-if="librariesReadState === 'empty'" class="illustration-empty-wrapper">
                <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                <p>暂无知识库</p>
              </div>
            </template>
            <el-table-column label="名称" min-width="180">
              <template #default="{row}"><div class="libraries-library-cell"><div class="libraries-library-name">{{ row.name }}</div><div class="libraries-library-slug">{{ row.slug }}</div></div></template>
            </el-table-column>
            <el-table-column label="状态" width="80" align="center">
              <template #default="{row}"><el-tag :type="libraryStatus(row).type" size="small">{{ libraryStatus(row).label }}</el-tag></template>
            </el-table-column>
            <el-table-column label="Embedding" min-width="140">
              <template #default="{row}"><div class="libraries-library-slug">{{ embedDisplay(row) }}</div></template>
            </el-table-column>
            <el-table-column label="处理能力" width="160">
              <template #default="{row}"><el-tag size="small" :type="row.ocr_enabled === true ? 'success' : row.ocr_enabled === false ? 'info' : ''">OCR {{ toggleLabel(row.ocr_enabled) }}</el-tag><el-tag size="small" :type="row.rerank_enabled === true ? 'success' : row.rerank_enabled === false ? 'info' : ''" class="libraries-tag-second">Rerank {{ toggleLabel(row.rerank_enabled) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="检索" width="90" align="center">
              <template #default="{row}">{{ row.retrieval_mode === 'hybrid' ? '混合检索' : '向量检索' }}</template>
            </el-table-column>
            <el-table-column label="切分" width="100"><template #default="{row}">{{ row.chunk_size }} / {{ row.chunk_overlap }}</template></el-table-column>
            <el-table-column label="操作" width="220" fixed="right">
              <template #default="{row}">
                <div class="libraries-actions">
                  <el-button link size="small" @click.stop="openDetail(row)">详情</el-button>
                  <el-button link size="small" :disabled="!!row.deleted_at" @click.stop="openEdit(row)">编辑</el-button>
                  <el-button link size="small" :disabled="!!row.deleted_at" @click.stop="openFaq(row)">常用问题</el-button>
                  <el-dropdown trigger="click" :disabled="!!row.deleted_at" @command="(cmd) => { if (cmd==='test') testEmbedding(row); if (cmd==='rebuild') rebuild(row); if (cmd==='delete') del(row); }">
                    <el-button link size="small" @click.stop>更多<el-icon><local-icon icon="mdi:chevron-down"></local-icon></el-icon></el-button>
                    <template #dropdown><el-dropdown-menu><el-dropdown-item command="test">测试 Embedding</el-dropdown-item><el-dropdown-item command="rebuild">重建 Collection</el-dropdown-item><el-dropdown-item command="delete">软删除</el-dropdown-item></el-dropdown-menu></template>
                  </el-dropdown>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div class="libraries-pagination"><span>共 {{ pagedLibraries.total }} 条</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10,20,50]" layout="sizes, prev, pager, next" :total="pagedLibraries.total" /></div>
      </section>
      </template>

      <!-- Detail drawer -->
      <el-drawer v-model="detailOpen" class="libraries-detail-drawer" :with-header="false" size="480px">
        <template v-if="selectedLibrary">
          <div class="libraries-detail-header">
            <div class="libraries-detail-title-row">
              <div class="libraries-detail-identity">
                <h3 class="libraries-detail-title">{{ selectedLibrary.name }}</h3>
                <span class="libraries-detail-slug mono">{{ selectedLibrary.slug }}</span>
              </div>
              <el-tag :type="libraryStatus(selectedLibrary).type" size="small">{{ libraryStatus(selectedLibrary).label }}</el-tag>
            </div>
            <div class="libraries-detail-summary">
              <span class="libraries-detail-pill">{{ selectedLibrary.retrieval_mode === 'hybrid' ? '混合检索' : '向量检索' }}</span>
              <span class="libraries-detail-pill">{{ selectedLibrary.vector_distance || '—' }}</span>
              <span class="libraries-detail-pill">{{ selectedLibrary.chunk_size }} / {{ selectedLibrary.chunk_overlap }}</span>
            </div>
          </div>

          <div class="libraries-detail-sections">
            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">基础信息</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">描述</span>
                <span class="libraries-detail-value">{{ selectedLibrary.description || '—' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">唯一 ID</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.slug }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">状态</span>
                <span class="libraries-detail-value"><el-tag :type="libraryStatus(selectedLibrary).type" size="small">{{ libraryStatus(selectedLibrary).label }}</el-tag></span>
              </div>
            </section>

            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">向量与检索</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">Embedding</span>
                <span class="libraries-detail-value">{{ embedDisplay(selectedLibrary) }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">向量距离</span>
                <span class="libraries-detail-value">{{ selectedLibrary.vector_distance || '—' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">检索模式</span>
                <span class="libraries-detail-value">{{ selectedLibrary.retrieval_mode === 'hybrid' ? '混合检索' : '向量检索' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">知识图谱</span>
                <span class="libraries-detail-value"><el-tag :type="selectedLibrary.graph_extraction_enabled ? 'success' : 'info'" size="small">{{ selectedLibrary.graph_extraction_enabled ? '开启' : '关闭' }}</el-tag></span>
              </div>
              <div v-if="selectedLibrary.graph_extraction_enabled" class="libraries-detail-field">
                <span class="libraries-detail-label">构建模式</span>
                <span class="libraries-detail-value">{{ { fast: '快速', standard: '标准', deep: '深度' }[selectedLibrary.graph_extraction_build_mode] || '标准' }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">切分</span>
                <span class="libraries-detail-value">{{ selectedLibrary.chunk_size }} / {{ selectedLibrary.chunk_overlap }}</span>
              </div>
            </section>

            <section class="libraries-detail-section">
              <h4 class="libraries-detail-section-title">存储与来源</h4>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">Qdrant</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.qdrant_collection }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">全文源</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ sourceDisplay(selectedLibrary.source_config) }}</span>
              </div>
              <div class="libraries-detail-field">
                <span class="libraries-detail-label">接口地址</span>
                <span class="libraries-detail-value libraries-detail-value--mono">{{ selectedLibrary.embedding_base_url || '—' }}</span>
              </div>
            </section>
          </div>

          <div class="libraries-detail-actions">
            <el-button :disabled="!!selectedLibrary.deleted_at" @click="detailOpen=false;openEdit(selectedLibrary)">编辑配置</el-button>
            <el-button type="primary" plain :disabled="!!selectedLibrary.deleted_at" @click="detailOpen=false;openFaq(selectedLibrary)">管理常用问题</el-button>
          </div>
        </template>
      </el-drawer>

      <!-- Create dialog -->
      <el-dialog v-model="create.open" title="新建知识库" width="780px" top="6vh" class="libraries-dialog">
        <el-form label-width="92px">
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">基本信息</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="名称" required><el-input v-model="create.form.name" placeholder="医学知识库" class="libraries-form-control" @input="onCreateNameInput" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="库唯一ID" required><el-input v-model="create.form.slug" readonly placeholder="根据名称自动生成" class="libraries-form-control" /><div class="libraries-form-hint">同名历史库删除后可重新创建；系统会追加 __r2、__r3 表示创建代次</div></el-form-item></el-col>
              <el-col :span="24"><el-form-item label="描述"><el-input v-model="create.form.description" type="textarea" :rows="2" class="libraries-form-control" /></el-form-item></el-col>
            </el-row>
          </div>
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">向量与切分配置</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="向量维度"><el-input-number v-model="create.form.embedding_dim" :min="64" :max="8192" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="单批大小"><el-input-number v-model="create.form.embed_batch_size" :min="1" :max="256" class="libraries-form-control" /><div class="libraries-form-hint">每次同时计算的切片数；本地 bge-m3 默认 32</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="分片大小"><el-input-number v-model="create.form.chunk_size" :min="200" :max="8000" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="分片重叠"><el-input-number v-model="create.form.chunk_overlap" :min="0" :max="2000" class="libraries-form-control" /></el-form-item></el-col>
            </el-row>
          </div>
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">检索与处理选项</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="检索模式"><el-select v-model="create.form.retrieval_mode" class="libraries-form-control"><el-option value="dense" label="向量检索 dense" /><el-option value="hybrid" label="混合检索 hybrid" /></el-select><div class="libraries-form-hint">hybrid=向量+关键词(pg_trgm) RRF</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="Rerank"><el-select v-model="create.form.rerank_enabled" class="libraries-form-control"><el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" /></el-select><div class="libraries-form-hint">需先在 .env 配 RERANK_* 才生效</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="知识图谱"><el-switch v-model="create.form.graph_extraction_enabled" active-text="上传时默认建立知识图谱" @change="onCreateGraphToggle" /></el-form-item></el-col>
              <el-col v-if="create.form.graph_extraction_enabled" :span="12"><el-form-item label="构建模式"><el-radio-group v-model="create.form.graph_extraction_build_mode"><el-radio-button value="fast">快速</el-radio-button><el-radio-button value="standard">标准</el-radio-button><el-radio-button value="deep">深度</el-radio-button></el-radio-group><div class="libraries-form-hint">标准模式兼顾完整度与速度</div></el-form-item></el-col>
              <el-col v-if="create.form.graph_extraction_enabled" :span="12"><el-form-item label="Schema"><el-select v-model="create.form.schema_template" class="libraries-form-control"><el-option value="enterprise" label="基础企业 Schema（推荐）" /><el-option value="none" label="暂不创建 Schema" /></el-select><div class="libraries-form-hint">基础模板会随知识库创建并立即激活，之后可在 Schema 管理中扩展</div></el-form-item></el-col>
            </el-row>
            <el-alert v-if="create.form.graph_extraction_enabled && create.form.schema_template === 'enterprise'" type="info" :closable="false" class="libraries-form-alert">将创建并激活基础企业 Schema，包含人员、部门、岗位、制度、流程、项目、产品、客户、文档和术语等实体类型及基础关系。</el-alert>
            <el-alert v-else-if="create.form.graph_extraction_enabled" type="warning" :closable="false" class="libraries-form-alert">知识图谱已开启，但没有选择 Schema。创建后必须先在 Schema 管理中建立并激活 Schema，才能进行带图谱的文件上传。</el-alert>
          </div>
        </el-form>
        <template #footer><el-button @click="create.open = false" :disabled="create.submitting">取消</el-button><el-button type="primary" :loading="create.submitting" @click="submitCreate">创建</el-button></template>
      </el-dialog>

      <!-- Edit dialog -->
      <el-dialog v-model="edit.open" :title="'编辑库 / ' + edit.slug" width="780px" top="6vh" class="libraries-dialog">
        <el-alert type="warning" :closable="false" class="libraries-form-alert">修改<b>向量维度</b>或<b>距离算法</b>后必须重建 collection。修改<b>向量模型</b>或<b>接口地址</b>只影响之后上传的文档，已有切片不会自动重新计算向量。</el-alert>
        <el-form label-width="92px">
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">基本信息</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="名称"><el-input v-model="edit.form.name" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="向量模型"><el-input v-model="edit.form.embedding_model" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="24"><el-form-item label="描述"><el-input v-model="edit.form.description" type="textarea" :rows="2" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="24"><el-form-item label="接口地址"><el-input v-model="edit.form.embedding_base_url" placeholder="留空继承全局 Embedding 服务" class="libraries-form-control" /></el-form-item></el-col>
            </el-row>
          </div>
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">Embedding 与切分</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="向量维度"><el-input-number v-model="edit.form.embedding_dim" :min="64" :max="8192" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="距离算法"><el-select v-model="edit.form.vector_distance" class="libraries-form-control"><el-option value="cosine" label="余弦距离 cosine" /><el-option value="euclid" label="欧氏距离 euclid" /><el-option value="dot" label="点积 dot" /></el-select></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="单批大小"><el-input-number v-model="edit.form.embed_batch_size" :min="1" :max="256" placeholder="继承全局" class="libraries-form-control" /><div class="libraries-form-hint">每次同时计算的切片数；留空时继承全局配置</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="分片大小"><el-input-number v-model="edit.form.chunk_size" :min="200" :max="8000" class="libraries-form-control" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="分片重叠"><el-input-number v-model="edit.form.chunk_overlap" :min="0" :max="2000" class="libraries-form-control" /></el-form-item></el-col>
            </el-row>
          </div>
          <div class="libraries-form-section"><h4 class="libraries-form-section-title">检索与处理选项</h4>
            <el-row :gutter="16">
              <el-col :span="12"><el-form-item label="检索模式"><el-select v-model="edit.form.retrieval_mode" class="libraries-form-control"><el-option value="dense" label="向量检索 dense" /><el-option value="hybrid" label="混合检索 hybrid" /></el-select><div class="libraries-form-hint">hybrid=向量+关键词 RRF；切换即时生效、无需重建</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="Rerank"><el-select v-model="edit.form.rerank_enabled" class="libraries-form-control"><el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" /></el-select><div class="libraries-form-hint">需先在 .env 配 RERANK_* 才生效</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="图片 OCR"><el-select v-model="edit.form.ocr_enabled" class="libraries-form-control"><el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" /></el-select><div class="libraries-form-hint">改开关只影响之后新上传/重灌的文档</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="DOCX 表格"><el-select v-model="edit.form.docx_table_aware" class="libraries-form-control"><el-option :value="null" label="继承全局" /><el-option :value="true" label="开启" /><el-option :value="false" label="关闭" /></el-select><div class="libraries-form-hint">改开关只影响之后新上传/重灌的文档</div></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="PGSQL 全文源"><el-switch v-model="edit.form.source_enrichment_enabled" active-text="启用正文回查" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="知识图谱"><el-switch v-model="edit.form.graph_extraction_enabled" active-text="上传时默认建立知识图谱" /></el-form-item></el-col>
              <el-col v-if="edit.form.graph_extraction_enabled" :span="12"><el-form-item label="构建模式"><el-radio-group v-model="edit.form.graph_extraction_build_mode"><el-radio-button value="fast">快速</el-radio-button><el-radio-button value="standard">标准</el-radio-button><el-radio-button value="deep">深度</el-radio-button></el-radio-group></el-form-item></el-col>
              <el-col v-if="edit.form.graph_extraction_enabled" :span="12"><el-form-item label="Schema"><el-button type="primary" plain @click="edit.open=false;$router.push({ path: '/knowledge-governance/schema', query: { library: edit.slug, tab: 'overview' } })">打开 Schema 管理</el-button></el-form-item></el-col>
            </el-row>
            <el-alert v-if="edit.form.graph_extraction_enabled" type="info" :closable="false" class="libraries-form-alert">开启后，新上传和替换文件会默认进入图谱抽取流程；上传页仍可针对单次任务关闭。系统会自动验证并发布合格事实；使用中发现错误后可在知识治理中修正。</el-alert>
          </div>
          <el-alert v-if="edit.form.source_enrichment_enabled" type="info" :closable="false" class="libraries-form-alert">PGSQL 全文源按<b>约定</b>自动配置：源表 = <code>{{ edit.slug || '<库唯一ID>' }}</code>（本库唯一ID）· 开启时已有高级配置会保持不变；从关闭改为开启会按约定重新生成。</el-alert>
          <el-alert v-else type="warning" :closable="false" class="libraries-form-alert">已关闭全文源补全：检索结果只使用向量库已有文本，不自动回查 PGSQL 全文源。</el-alert>
        </el-form>
        <template #footer><el-button @click="edit.open = false">取消</el-button><el-button type="primary" @click="submitEdit">保存</el-button></template>
      </el-dialog>

      <!-- FAQ dialog -->
      <el-dialog v-model="faqMgr.open" :title="'常用问题 / ' + faqMgr.slug" width="680px" class="libraries-dialog">
        <el-alert type="info" :closable="false" class="libraries-faq-alert">管理员在此维护高频问题；普通用户在「数据检索」页选到该库后会看到这些问题，点一下即可发起检索。停用的不会展示给用户。</el-alert>
        <el-alert v-if="faqMgr.error" type="warning" :closable="false" show-icon
                  :title="'常用问题加载失败：' + faqMgr.error" class="libraries-faq-alert">
          <template #default><el-button link type="primary" :loading="faqMgr.loading" @click="loadFaq">重试</el-button></template>
        </el-alert>
        <div class="libraries-faq-create"><el-input v-model="faqMgr.newQuestion" placeholder="新增常用问题，如：八大员包括哪些岗位" @keyup.enter="addFaq" /><el-input-number v-model="faqMgr.newSort" :min="0" :max="9999" controls-position="right" /><el-button type="primary" @click="addFaq">新增</el-button></div>
        <el-table :data="faqMgr.list" v-loading="faqMgr.loading" border size="small">
          <el-table-column label="排序" width="110"><template #default="{row}"><el-input-number v-model="row.sort_order" :min="0" :max="9999" size="small" controls-position="right" class="libraries-form-control" /></template></el-table-column>
          <el-table-column label="问题"><template #default="{row}"><el-input v-model="row.question" size="small" /></template></el-table-column>
          <el-table-column label="启用" width="80" align="center"><template #default="{row}"><el-switch v-model="row.is_active" /></template></el-table-column>
          <el-table-column label="操作" width="160"><template #default="{row}"><el-button size="small" type="primary" plain @click="saveFaq(row)">保存</el-button><el-button size="small" type="danger" plain @click="removeFaq(row)">删除</el-button></template></el-table-column>
        </el-table>
        <el-empty v-if="!faqMgr.loading && !faqMgr.list.length" description="暂无常用问题" :image-size="60" />
        <template #footer><el-button @click="faqMgr.open = false">关闭</el-button></template>
      </el-dialog>
    </div>
    `,
};
