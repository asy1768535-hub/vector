import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store, hasPermission } from '../store.js';
import { dataEmpty } from '../illustrations.js';
import { readableLibraries, resolveSelectedSlug } from '../menu_access.js';
import {
    documentDisplayName,
    documentStatusLabel,
    documentStatusTag,
    documentTypeIcon,
    filterDocuments,
    formatDocumentTime,
    paginateDocuments,
} from '../documents_ui.js';

export default {
    setup() {
        const router = useRouter();
        const route = useRoute();
        const libs = ref([]);
        const slug = ref(null);
        const docs = ref([]);
        const stats = ref(null);
        const loading = ref(false);
        const filters = reactive({ keyword: '', status: '', type: '', dateRange: [] });
        const page = ref(1);
        const pageSize = ref(5);
        const detail = reactive({ open: false, row: null, jobs: [], loading: false });
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
            Boolean(slug.value) && (store.user?.is_superuser || hasPermission(slug.value, 'insert'))
        );
        const canDelete = computed(() =>
            Boolean(slug.value) && (store.user?.is_superuser || hasPermission(slug.value, 'delete'))
        );
        const isSuperuser = computed(() => Boolean(store.user?.is_superuser));

        const processingCount = computed(() =>
            Number(stats.value?.pending_jobs || 0) + Number(stats.value?.processing_jobs || 0)
        );
        const filteredDocs = computed(() => filterDocuments(docs.value, filters));
        const pagination = computed(() => paginateDocuments(filteredDocs.value, page.value, pageSize.value));
        const visibleDocs = computed(() => pagination.value.items);
        const partialList = computed(() =>
            Number(stats.value?.document_count || 0) > docs.value.length
        );

        async function loadLibs() {
            try {
                // 超管 → /admin/libraries；非超管 → 仅「有 read 权限」的库（文档页是读类页面）。
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = readableLibraries(store.permissions);
                }
                // 选中库：优先 URL 参数 ?slug=，其次当前值，最后第一个可读库
                const urlSlug = route.query.slug;
                const candidate = (urlSlug && libs.value.some((l) => l.slug === urlSlug)) ? urlSlug : slug.value;
                const nextSlug = resolveSelectedSlug(candidate, libs.value);
                slug.value = nextSlug;
                await loadDocs();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function loadDocs(forceRefresh = false) {
            if (!slug.value) { docs.value = []; stats.value = null; return; }
            loading.value = true;
            try {
                const [d, s] = await Promise.all([
                    api.listDocuments(slug.value, { limit: 500 }, forceRefresh),
                    api.libraryStats(slug.value, forceRefresh),
                ]);
                docs.value = d;
                stats.value = s;
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function resetFilters() {
            filters.keyword = '';
            filters.status = '';
            filters.type = '';
            filters.dateRange = [];
            page.value = 1;
            pageSize.value = 5;
        }

        function openFileImport() {
            if (!slug.value || !canInsert.value) return;
            router.push({ path: '/import', query: { library: slug.value, mode: 'add' } });
        }

        function openIngest() {
            if (!slug.value || !canInsert.value) return;
            dialog.mode = 'create';
            dialog.docId = null;
            dialog.form = { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' };
            dialog.open = true;
        }

        function openEdit(row) {
            if (!slug.value || !canInsert.value) return;
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
            if (!slug.value) {
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
            if (!slug.value) return;
            try {
                await ElMessageBox.confirm(`删除文档 "${row.title || row.id.slice(0, 8)}"?`, '确认', { type: 'warning' });
                await api.deleteDocument(slug.value, row.id);
                ElMessage.success('已删除 (Qdrant 异步清理)');
                loadDocs();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        async function openDetail(row) {
            detail.open = true;
            detail.row = row;
            detail.jobs = [];
            await loadDetailJobs(row);
        }

        async function loadDetailJobs(row) {
            if (!row || !slug.value) return;
            detail.loading = true;
            try {
                detail.jobs = await api.listDocumentJobs(slug.value, row.id);
            } catch (e) {
                ElMessage.error(`加载文档任务失败：${e.message || String(e)}`);
            } finally {
                detail.loading = false;
            }
        }

        async function retryJob(job) {
            if (!isSuperuser.value || job.status !== 'failed') return;
            try {
                await ElMessageBox.confirm('确认重新提交这个失败任务？', '重试任务', { type: 'warning' });
                await api.retryJob(job.id);
                ElMessage.success('任务已重新提交');
                await Promise.all([loadDetailJobs(detail.row), loadDocs()]);
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        async function retryDocument(row) {
            if (!slug.value || !isSuperuser.value || row.status !== 'failed') return;
            detail.row = row;
            try {
                const jobs = await api.listDocumentJobs(slug.value, row.id);
                const failedJob = jobs.find((job) => job.status === 'failed');
                if (!failedJob) {
                    ElMessage.warning('未找到可重试的失败任务');
                    return;
                }
                detail.jobs = jobs;
                await retryJob(failedJob);
            } catch (e) {
                ElMessage.error(`加载文档任务失败：${e.message || String(e)}`);
            }
        }

        function metadataText(row) {
            if (!row?.metadata) return '—';
            try { return JSON.stringify(row.metadata, null, 2); }
            catch (_) { return '—'; }
        }

        watch(
            () => [filters.keyword, filters.status, filters.type, ...(filters.dateRange || [])],
            () => { page.value = 1; },
        );
        watch(pageSize, () => { page.value = 1; });
        watch(slug, async () => {
            detail.open = false;
            detail.row = null;
            resetFilters();
            await loadDocs();
        });
        watch(() => pagination.value.page, (validPage) => {
            if (page.value !== validPage) page.value = validPage;
        });

        onMounted(async () => {
            await loadLibs();
            // Auto-open document detail if linked from chat page (?open=<document_id>)
            const openId = route.query.open;
            if (openId && slug.value) {
                const row = docs.value.find((d) => String(d.id) === String(openId));
                if (row) await openDetail(row);
            }
        });

        return {
            myLibs, slug, docs, stats, loading, canInsert, canDelete, isSuperuser,
            processingCount, filters, page, pageSize, pagination, visibleDocs, partialList,
            detail, dialog, loadDocs, resetFilters, openFileImport, openIngest, openEdit,
            openDetail, retryJob, retryDocument, submitIngest, del, metadataText,
            documentDisplayName, documentStatusLabel, documentStatusTag, documentTypeIcon,
            formatDocumentTime, dataEmpty,
        };
    },
    template: `
    <div class="documents-workspace">
      <section class="documents-overview">
        <div class="documents-heading">
          <h2>文档管理</h2>
          <el-select v-model="slug" placeholder="选择知识库" style="width: 100%">
            <el-option v-for="l in myLibs" :key="l.slug"
                       :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
          </el-select>
        </div>
        <div class="documents-stats">
          <div class="documents-stat"><span>文档总数</span><b class="documents-stat-value">{{ stats?.document_count || 0 }}</b></div>
          <div class="documents-stat"><span>处理中</span><b class="documents-stat-value">{{ processingCount }}</b></div>
          <div class="documents-stat"><span>已完成</span><b class="documents-stat-value">{{ stats?.done_jobs || 0 }}</b></div>
          <div class="documents-stat"><span>失败</span><b class="documents-stat-value documents-stat-danger">{{ stats?.failed_jobs || 0 }}</b></div>
        </div>
        <div class="documents-actions">
          <el-button :disabled="!canInsert" :title="!slug ? '请先选择知识库' : (canInsert ? '' : '没有写入权限')" @click="openFileImport">文件导入</el-button>
          <el-button type="primary" :disabled="!canInsert" :title="!slug ? '请先选择知识库' : (canInsert ? '' : '没有写入权限')" @click="openIngest">提交文本</el-button>
        </div>
      </section>

      <el-alert v-if="!myLibs.length" title="当前账号没有可读取的知识库"
                type="info" :closable="false" show-icon />

      <section class="documents-filters">
        <el-input v-model="filters.keyword" clearable placeholder="搜索文件名、external_id 或文档 ID" />
        <el-select v-model="filters.status" clearable placeholder="全部状态">
          <el-option label="等待中" value="pending" />
          <el-option label="处理中" value="processing" />
          <el-option label="完成" value="ready" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-select v-model="filters.type" clearable placeholder="全部类型">
          <el-option label="PDF" value="pdf" />
          <el-option label="Word" value="word" />
          <el-option label="Excel" value="excel" />
          <el-option label="Markdown" value="markdown" />
          <el-option label="JSON" value="json" />
          <el-option label="CSV" value="csv" />
          <el-option label="文本" value="text" />
          <el-option label="其他" value="other" />
        </el-select>
        <el-date-picker v-model="filters.dateRange" type="daterange" value-format="YYYY-MM-DD"
                        start-placeholder="开始日期" end-placeholder="结束日期" style="width: 100%" />
        <div class="documents-filter-actions">
          <el-button @click="resetFilters">重置</el-button>
          <el-button :loading="loading" @click="loadDocs(true)">刷新</el-button>
        </div>
      </section>

      <section class="documents-table-panel">
        <div v-if="partialList" class="documents-load-note">
          当前加载 {{ docs.length }} / 总计 {{ stats.document_count }}，筛选与分页仅作用于已加载文档
        </div>
        <div class="documents-table-shell">
          <el-table :data="visibleDocs" v-loading="loading">
            <template #empty><div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>当前条件下暂无文档</p></div></template>
            <el-table-column label="文件名" min-width="250">
              <template #default="{row}">
                <div class="documents-file">
                  <img v-if="documentTypeIcon(row)" class="documents-file-icon"
                       :src="documentTypeIcon(row)" alt="" aria-hidden="true" />
                  <local-icon v-else class="documents-file-icon" icon="mdi:file-document-outline"></local-icon>
                  <span class="documents-file-name" :title="documentDisplayName(row)">{{ documentDisplayName(row) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{row}">
                <el-tag :type="documentStatusTag(row.status)" size="small">{{ documentStatusLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="版本" width="80">
              <template #default="{row}">v{{ row.current_revision || 0 }}</template>
            </el-table-column>
            <el-table-column label="external_id" min-width="120" show-overflow-tooltip>
              <template #default="{row}">{{ row.external_id || '—' }}</template>
            </el-table-column>
            <el-table-column label="更新时间" width="155">
              <template #default="{row}">{{ formatDocumentTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="220">
              <template #default="{row}">
                <el-button link class="doc-link-btn" @click="openDetail(row)">详情</el-button>
                <el-button link class="doc-link-btn" :disabled="!canInsert" @click="openEdit(row)">编辑</el-button>
                <el-button v-if="isSuperuser && row.status === 'failed'" link type="warning"
                           @click="retryDocument(row)">重试</el-button>
                <el-button link type="danger" :disabled="!canDelete" @click="del(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div class="documents-pagination">
          <el-pagination v-model:current-page="page" v-model:page-size="pageSize"
                         :page-sizes="[5, 20, 50]" :total="pagination.total"
                         layout="total, sizes, prev, pager, next" />
        </div>
      </section>

      <el-drawer v-model="detail.open" class="documents-detail" title="文档详情" size="520px">
        <template v-if="detail.row">
          <div class="documents-detail-meta">
            <span>标题</span><b>{{ documentDisplayName(detail.row) }}</b>
            <span>文档 ID</span><span class="mono">{{ detail.row.id }}</span>
            <span>external_id</span><span>{{ detail.row.external_id || '—' }}</span>
            <span>状态</span><el-tag :type="documentStatusTag(detail.row.status)">{{ documentStatusLabel(detail.row.status) }}</el-tag>
            <span>版本</span><span>v{{ detail.row.current_revision || 0 }}</span>
            <span>content hash</span><span class="mono">{{ detail.row.content_hash || '—' }}</span>
            <span>创建时间</span><span>{{ formatDocumentTime(detail.row.created_at) }}</span>
            <span>更新时间</span><span>{{ formatDocumentTime(detail.row.updated_at) }}</span>
            <span>最后错误</span><span class="documents-error">{{ detail.row.last_error || '—' }}</span>
          </div>
          <h3>Metadata</h3>
          <pre class="documents-detail-json">{{ metadataText(detail.row) }}</pre>
          <h3>摄入任务</h3>
          <div v-loading="detail.loading">
            <el-empty v-if="!detail.loading && !detail.jobs.length" description="暂无摄入任务" />
            <article v-for="job in detail.jobs" :key="job.id" class="documents-job">
              <div class="documents-job-head">
                <el-tag :type="documentStatusTag(job.status)">{{ documentStatusLabel(job.status) }}</el-tag>
                <el-button v-if="isSuperuser && job.status === 'failed'" link type="warning"
                           @click="retryJob(job)">重试</el-button>
              </div>
              <div>创建：{{ formatDocumentTime(job.created_at) }}</div>
              <div>开始：{{ formatDocumentTime(job.claimed_at) }}</div>
              <div>结束：{{ formatDocumentTime(job.finished_at) }}</div>
              <div>尝试次数：{{ job.attempt_count ?? '—' }}</div>
              <div v-if="job.last_error" class="documents-error">{{ job.last_error }}</div>
            </article>
          </div>
        </template>
      </el-drawer>

      <el-dialog v-model="dialog.open" :title="dialog.mode === 'edit' ? '编辑文档（整篇替换并重 embed）' : (slug ? ('向 ' + slug + ' 提交文档') : '提交文档')" width="640px">
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
