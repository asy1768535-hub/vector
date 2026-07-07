import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store, hasPermission } from '../store.js';
import { dataEmpty } from '../illustrations.js';
import { readableLibraries, resolveSelectedSlug } from '../menu_access.js';
import { copyTextToClipboard } from '../copy_text.js';
import {
    documentDisplayName,
    documentStatusLabel,
    documentStatusTag,
    documentTypeIcon,
    documentTypeLabel,
    filterDocuments,
    formatDocumentTime,
    latestDocumentJob,
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
        const sourceReader = reactive({
            open: false,
            loading: false,
            row: null,
            data: null,
            error: '',
            keyword: '',
        });
        const dialog = reactive({
            open: false,
            mode: 'create',   // 'create' | 'edit'
            docId: null,
            row: null,
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
        const detailLatestJob = computed(() => latestDocumentJob(detail.jobs));
        const sourceText = computed(() => String(sourceReader.data?.normalized_text || ''));
        const sourceMatchCount = computed(() => {
            const keyword = sourceReader.keyword.trim().toLowerCase();
            if (!keyword) return 0;
            const text = sourceText.value.toLowerCase();
            let count = 0;
            let pos = 0;
            while (pos < text.length) {
                const idx = text.indexOf(keyword, pos);
                if (idx < 0) break;
                count += 1;
                pos = idx + keyword.length;
            }
            return count;
        });
        const highlightedSourceParts = computed(() => {
            const text = sourceText.value;
            const keyword = sourceReader.keyword.trim();
            if (!keyword) return [{ text, match: false }];
            const lowerText = text.toLowerCase();
            const lowerKeyword = keyword.toLowerCase();
            const parts = [];
            let pos = 0;
            while (pos < text.length) {
                const idx = lowerText.indexOf(lowerKeyword, pos);
                if (idx < 0) break;
                if (idx > pos) parts.push({ text: text.slice(pos, idx), match: false });
                parts.push({ text: text.slice(idx, idx + keyword.length), match: true });
                pos = idx + keyword.length;
            }
            if (pos < text.length) parts.push({ text: text.slice(pos), match: false });
            return parts.length ? parts : [{ text, match: false }];
        });

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
            dialog.row = null;
            dialog.form = { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' };
            dialog.open = true;
        }

        function openEdit(row) {
            if (!slug.value || !canInsert.value) return;
            dialog.mode = 'edit';
            dialog.docId = row.id;
            dialog.row = row;
            dialog.form = {
                title: row.title || '',
                external_id: row.external_id || '',
                text: '',
                splitter: 'text',
                metadata_json: row.metadata ? JSON.stringify(row.metadata, null, 2) : '',
            };
            dialog.open = true;
        }

        function openReplaceImport(row) {
            if (!slug.value || !canInsert.value) return;
            router.push({
                path: '/import',
                query: {
                    library: slug.value,
                    mode: 'replace',
                    replaceDocumentId: row.id,
                    replaceTitle: documentDisplayName(row),
                },
            });
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
                    ? `已覆盖正文并重新入队 ${resp.chunk_count} 个分片`
                    : `已入队 ${resp.chunk_count} 个分片 (doc_id=${resp.document_id.slice(0, 8)}…)`);
                dialog.open = false;
                loadDocs();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function del(row) {
            if (!slug.value) return;
            if (!canDelete.value) {
                ElMessage.warning('没有删除权限');
                return;
            }
            try {
                await ElMessageBox.confirm(
                    `确认删除文档 "${documentDisplayName(row)}"？\n\n删除后文档将不可用于后续检索；向量清理为异步执行；历史问答引用不会自动恢复。`,
                    '删除文档',
                    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
                );
                await api.deleteDocument(slug.value, row.id);
                ElMessage.success('已删除，向量清理将异步完成');
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

        function shortText(value, size = 18) {
            const text = String(value || '');
            if (!text) return '—';
            return text.length > size ? `${text.slice(0, size)}…` : text;
        }

        async function copyDocValue(value, label) {
            try {
                await copyTextToClipboard(value);
                ElMessage.success(`已复制${label}`);
            } catch (e) {
                ElMessage.error(e.message || '复制失败');
            }
        }

        async function openFullSource(row) {
            if (!slug.value || !row) return;
            sourceReader.open = true;
            sourceReader.loading = true;
            sourceReader.row = row;
            sourceReader.data = null;
            sourceReader.error = '';
            sourceReader.keyword = '';
            try {
                sourceReader.data = await api.getDocumentFullSource(slug.value, row.id);
            } catch (e) {
                sourceReader.error = e.status === 404
                    ? '该文档缺少原文快照，请重新导入后再阅读原文。'
                    : (e.message || '加载原文失败');
            } finally {
                sourceReader.loading = false;
            }
        }

        async function downloadOriginalFile(row) {
            if (!slug.value || !row) return;
            try {
                const { blob, filename } = await api.downloadDocumentFile(slug.value, row.id);
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename || documentDisplayName(row);
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
            } catch (e) {
                const msg = e.status === 404
                    ? '该文档缺少原始文件，请重新导入后再下载'
                    : (e.message || '下载原文件失败');
                ElMessage.error(msg);
            }
        }

        function closeFullSource() {
            sourceReader.open = false;
            sourceReader.loading = false;
            sourceReader.data = null;
            sourceReader.error = '';
            sourceReader.keyword = '';
        }

        function deleteTitle() {
            if (!slug.value) return '请先选择知识库';
            return canDelete.value ? '删除文档' : '没有删除权限';
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
            detail, detailLatestJob, sourceReader, sourceText, sourceMatchCount, highlightedSourceParts,
            dialog, loadDocs, resetFilters, openFileImport, openIngest, openEdit,
            openReplaceImport, openDetail, openFullSource, downloadOriginalFile, closeFullSource, retryJob, retryDocument, submitIngest, del, metadataText,
            copyDocValue, deleteTitle, shortText,
            documentDisplayName, documentStatusLabel, documentStatusTag, documentTypeIcon, documentTypeLabel,
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
            <el-table-column label="操作" width="250" class-name="documents-op-column">
              <template #default="{row}">
                <div class="documents-op-group">
                  <el-button link class="doc-link-btn" @click="openDetail(row)">详情</el-button>
                  <el-button link class="doc-link-btn" :disabled="!canInsert" :title="canInsert ? '编辑文档' : '没有写入权限'" @click="openEdit(row)">编辑</el-button>
                  <el-button v-if="isSuperuser && row.status === 'failed'" link type="warning"
                             @click="retryDocument(row)">重试</el-button>
                  <el-button link type="danger" class="doc-delete-btn" :disabled="!canDelete" :title="deleteTitle()" @click="del(row)">删除</el-button>
                </div>
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

      <el-drawer v-model="detail.open" class="documents-detail" title="文档详情" size="780px">
        <template v-if="detail.row">
          <section class="documents-detail-card">
            <div class="documents-detail-card-head">
              <h3>基础信息</h3>
              <el-tag :type="documentStatusTag(detail.row.status)">{{ documentStatusLabel(detail.row.status) }}</el-tag>
            </div>
            <div class="documents-detail-grid">
              <span>文件名</span><b>{{ documentDisplayName(detail.row) }}</b>
              <span>文件类型</span><span>{{ documentTypeLabel(detail.row) }}</span>
              <span>状态</span><span>{{ documentStatusLabel(detail.row.status) }}</span>
              <span>版本</span><span>v{{ detail.row.current_revision || 0 }}</span>
              <span>文档 ID</span><span class="documents-copy-line"><code>{{ detail.row.id }}</code><el-button text class="documents-copy-btn" title="复制文档 ID" @click="copyDocValue(detail.row.id, '文档 ID')"><local-icon icon="mdi:content-copy"></local-icon></el-button></span>
              <span>external_id</span><span>{{ detail.row.external_id || '—' }}</span>
              <span>更新时间</span><span>{{ formatDocumentTime(detail.row.updated_at) }}</span>
              <span>content_hash</span><span class="documents-copy-line"><code :title="detail.row.content_hash || ''">{{ shortText(detail.row.content_hash, 28) }}</code><el-button v-if="detail.row.content_hash" text class="documents-copy-btn" title="复制 content_hash" @click="copyDocValue(detail.row.content_hash, 'content_hash')"><local-icon icon="mdi:content-copy"></local-icon></el-button></span>
            </div>
          </section>

          <section class="documents-detail-card">
            <div class="documents-detail-card-head"><h3>处理信息</h3></div>
            <div v-loading="detail.loading">
              <div class="documents-detail-grid">
                <span>最近摄入任务</span><span>{{ detailLatestJob?.id || '—' }}</span>
                <span>任务状态</span><span><el-tag v-if="detailLatestJob" :type="documentStatusTag(detailLatestJob.status)" size="small">{{ documentStatusLabel(detailLatestJob.status) }}</el-tag><template v-else>—</template></span>
                <span>失败原因</span><span class="documents-error">{{ detailLatestJob?.last_error || detail.row.last_error || '—' }}</span>
              </div>
              <el-empty v-if="!detail.loading && !detail.jobs.length" description="暂无摄入任务" />
              <article v-for="job in detail.jobs" :key="job.id" class="documents-job">
                <div class="documents-job-head">
                  <el-tag :type="documentStatusTag(job.status)">{{ documentStatusLabel(job.status) }}</el-tag>
                  <el-button v-if="isSuperuser && job.status === 'failed'" link type="warning"
                             @click="retryJob(job)">重试失败任务</el-button>
                </div>
                <div>任务 ID：{{ job.id }}</div>
                <div>创建：{{ formatDocumentTime(job.created_at) }}</div>
                <div>开始：{{ formatDocumentTime(job.claimed_at) }}</div>
                <div>结束：{{ formatDocumentTime(job.finished_at) }}</div>
                <div>尝试次数：{{ job.attempt_count ?? '—' }}</div>
                <div v-if="job.last_error" class="documents-error">{{ job.last_error }}</div>
              </article>
            </div>
          </section>

          <section class="documents-detail-card">
            <div class="documents-detail-card-head">
              <h3>来源信息</h3>
              <div class="documents-source-actions">
                <el-button type="primary" plain @click="openFullSource(detail.row)">阅读原文</el-button>
                <el-button plain @click="downloadOriginalFile(detail.row)">下载原文件</el-button>
              </div>
            </div>
            <p class="documents-source-note">阅读原文使用 normalized_text 快照；下载原文件只在后端已持久化原始上传文件时可用。</p>
          </section>

          <section class="documents-detail-card documents-metadata-card">
            <div class="documents-detail-card-head"><h3>Metadata</h3></div>
            <pre class="documents-detail-json">{{ metadataText(detail.row) }}</pre>
          </section>
        </template>
      </el-drawer>

      <el-dialog v-model="sourceReader.open" title="阅读原文" width="860px" class="documents-source-dialog" @closed="closeFullSource">
        <div v-if="sourceReader.loading" class="documents-source-loading">正在加载原文...</div>
        <template v-else>
          <el-alert v-if="sourceReader.error" type="warning" :closable="false" show-icon :title="sourceReader.error" />
          <template v-else-if="sourceReader.data">
            <div class="documents-source-meta">
              <span>{{ sourceReader.data.file_name || sourceReader.data.document_title || documentDisplayName(sourceReader.row) }}</span>
              <span v-if="sourceReader.data.file_type">{{ sourceReader.data.file_type }}</span>
              <span>v{{ sourceReader.data.revision || sourceReader.row?.current_revision || 0 }}</span>
              <span>{{ sourceReader.data.text_length || sourceText.length }} 字</span>
            </div>
            <div class="documents-source-toolbar">
              <el-input v-model="sourceReader.keyword" clearable placeholder="搜索原文关键词" />
              <span class="documents-source-match-count">{{ sourceReader.keyword ? ('匹配 ' + sourceMatchCount + ' 处') : '输入关键词后高亮匹配' }}</span>
            </div>
            <el-empty v-if="!sourceText" description="原文快照为空" />
            <pre v-else class="documents-source-text"><template v-for="(part, pi) in highlightedSourceParts" :key="pi"><mark v-if="part.match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></pre>
          </template>
          <el-empty v-else description="暂无原文内容" />
        </template>
      </el-dialog>

      <el-dialog v-model="dialog.open" :title="dialog.mode === 'edit' ? '编辑文档' : (slug ? ('向 ' + slug + ' 提交文档') : '提交文档')" width="720px" class="documents-edit-dialog">
        <template v-if="dialog.mode === 'edit'">
          <el-alert type="warning" :closable="false" class="documents-edit-alert"
                    title="当前保存是文本覆盖：会替换正文、重新切分、重新向量化，旧问答引用不会自动更新。" />
        </template>
        <el-form label-width="110px">
          <section class="documents-edit-section">
            <h3>{{ dialog.mode === 'edit' ? '基础信息编辑' : '基础信息' }}</h3>
            <p v-if="dialog.mode === 'edit'" class="documents-edit-hint">标题和 metadata 会随下方文本覆盖一起提交；external_id 当前版本不可修改。</p>
            <el-form-item label="标题"><el-input v-model="dialog.form.title" /></el-form-item>
            <el-form-item label="external_id">
              <el-input v-model="dialog.form.external_id" :disabled="dialog.mode === 'edit'" placeholder="可选；用于幂等/upsert" />
              <div v-if="dialog.mode === 'edit'" class="documents-form-help">后端当前不支持在文档页单独修改 external_id。</div>
            </el-form-item>
            <el-form-item label="metadata">
              <el-input v-model="dialog.form.metadata_json" type="textarea" :rows="3"
                        placeholder='可选 JSON，例如 {"author":"...","year":2024}' />
            </el-form-item>
          </section>

          <section class="documents-edit-section documents-content-overwrite">
            <h3>{{ dialog.mode === 'edit' ? '覆盖文档内容（文本覆盖）' : '正文' }}</h3>
            <p v-if="dialog.mode === 'edit'" class="documents-edit-hint">此操作会用下面的完整正文替换旧正文，重新切分并重新向量化；旧问答引用不会自动更新。</p>
            <el-form-item label="切分方式">
              <el-radio-group v-model="dialog.form.splitter">
                <el-radio value="text">text</el-radio>
                <el-radio value="markdown">markdown</el-radio>
                <el-radio value="none">none (单一分片)</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="正文" required>
              <el-input v-model="dialog.form.text" type="textarea" :rows="10"
                        placeholder="粘贴完整文本 / Markdown / JSON 字符串" />
            </el-form-item>
          </section>

          <section v-if="dialog.mode === 'edit'" class="documents-edit-section documents-reimport-zone">
            <h3>覆盖导入新文档</h3>
            <p>如果需要用 PDF、Word、Excel 等文件覆盖，请使用导入页的替换模式。新文件导入后会覆盖此文档并重新向量化。</p>
            <el-button type="warning" plain @click="openReplaceImport(dialog.row)">重新导入并覆盖此文档</el-button>
          </section>
        </el-form>
        <template #footer>
          <el-button @click="dialog.open = false">取消</el-button>
          <el-button type="primary" @click="submitIngest">{{ dialog.mode === 'edit' ? '保存文本覆盖并重新向量化' : '提交（异步 embed）' }}</el-button>
        </template>
      </el-dialog>
    </div>
    `,
};
