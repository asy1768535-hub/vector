import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { taskEmpty } from '../illustrations.js';
import { STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL, formatJobTime, jobDuration, shortId, filterJobs, paginateJobs, libraryName } from '../jobs_ui.js';

const BUILD_MODE_LABEL = { fast: '快速', standard: '标准', deep: '深度' };
const PUBLICATION_STATUS_LABEL = {
    pending: '等待抽取完成',
    publishing: '自动发布中',
    available: '已发布可用',
    retryable: '自动发布失败，可重试',
    not_required: '无合格事实，无需发布',
};

function buildModeLabel(value) {
    return BUILD_MODE_LABEL[value] || BUILD_MODE_LABEL.standard;
}

function publicationStatusLabel(value) {
    return PUBLICATION_STATUS_LABEL[value] || '等待抽取完成';
}

function formatEta(seconds) {
    if (!Number.isFinite(seconds)) return '—';
    if (seconds < 60) return `${Math.max(0, Math.round(seconds))} 秒`;
    return `约 ${Math.ceil(seconds / 60)} 分钟`;
}

export default {
    setup() {
        const jobs = ref([]);
        const libs = ref([]);
        const loading = ref(false);
        const resetting = ref(false);
        const retryingId = ref(null);
        const emptyStats = () => ({
            pending: 0, processing: 0, done: 0, failed: 0,
            cancelled: 0, superseded: 0, retryable_failed: 0, total: 0,
        });
        const stats = ref(emptyStats());
        const statsFailed = ref(false);
        const filters = reactive({ task_type: '', status: '', library_id: '', worker_id: '', document_id: '', dateFrom: '', dateTo: '' });
        const page = ref(1);
        const pageSize = ref(10);
        const selectedJob = ref(null);
        const detailCardRef = ref(null);
        const truncated = ref(false);
        const libsFailed = ref(false);

        let refreshTimer = null;

        async function load(forceRefresh = false, silent = false) {
            if (!silent) loading.value = true;
            const [jr, sr] = await Promise.allSettled([
                api.listMonitoredTasks({ limit: 1500 }, forceRefresh),
                api.monitoredTaskStats(forceRefresh),
            ]);
            if (jr.status === 'fulfilled') {
                jobs.value = jr.value || [];
                truncated.value = (jr.value || []).length >= 500;
                if (selectedJob.value) {
                    const fresh = (jr.value || []).find((j) => j.id === selectedJob.value.id);
                    if (fresh) selectedJob.value = fresh;
                    else closeDetail();
                }
            } else if (!silent) {
                ElMessage.error(jr.reason?.message || '任务列表加载失败');
            }
            if (sr.status === 'fulfilled') {
                stats.value = sr.value || emptyStats();
                statsFailed.value = false;
            } else {
                statsFailed.value = true;
                stats.value = emptyStats();
            }
            if (!silent) loading.value = false;
        }

        async function loadLibs() {
            libsFailed.value = false;
            try {
                const l = await api.listLibraries({ limit: 500 }, false);
                libs.value = (l || []).filter((x) => !x.deleted_at);
            } catch (_) { libsFailed.value = true; }
        }

        async function refreshAll() {
            await Promise.all([load(true), loadLibs()]);
        }

        onMounted(async () => {
            await Promise.all([load(false), loadLibs()]);
            refreshTimer = window.setInterval(() => load(true, true), 10000);
        });
        onUnmounted(() => {
            if (refreshTimer !== null) window.clearInterval(refreshTimer);
        });

        const filtered = computed(() => filterJobs(jobs.value, filters));
        const paged = computed(() => paginateJobs(filtered.value, page.value, pageSize.value));

        function resetFilters() {
            Object.assign(filters, { task_type: '', status: '', library_id: '', worker_id: '', document_id: '', dateFrom: '', dateTo: '' });
            page.value = 1;
        }

        function isSelected(row) {
            return selectedJob.value && selectedJob.value.id === row.id;
        }

        async function openDetail(row) {
            if (isSelected(row)) {
                // 收起：不滚动
                selectedJob.value = null;
            } else {
                // 切换或首次打开
                selectedJob.value = row;
                await nextTick();
                detailCardRef.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        function closeDetail() { selectedJob.value = null; }

        async function retry(row) {
            if (retryingId.value) return;
            retryingId.value = row.id;
            try { await api.retryJob(row.id); ElMessage.success('已重置为 pending'); await refreshAll(); }
            catch (e) { ElMessage.error(e.message); }
            finally { retryingId.value = null; }
        }

        async function resetFailed() {
            if (filters.library_id) {
                try {
                    await ElMessageBox.confirm(
                        '将重置该知识库中的所有失败向量任务，worker 会自动重跑。确定？',
                        '重置失败向量任务', { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' }
                    );
                } catch (_) { return; }
            } else {
                if (statsFailed.value) {
                    try {
                        await ElMessageBox.confirm(
                            '将重置全部失败向量任务为待处理，worker 会自动重跑。确定？',
                            '重置失败向量任务', { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' }
                        );
                    } catch (_) { return; }
                } else {
                    if (!stats.value.retryable_failed) { ElMessage.info('当前没有可重试的失败向量任务'); return; }
                    try {
                        await ElMessageBox.confirm(
                            `将把全部 ${stats.value.retryable_failed} 条失败向量任务重置为待处理，worker 会自动重跑。确定？`,
                            '重置失败向量任务', { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' }
                        );
                    } catch (_) { return; }
                }
            }
            resetting.value = true;
            try {
                const r = await api.resetFailedJobs(filters.library_id || null);
                ElMessage.success(`已重置 ${r.reset_count} 条失败任务`);
                await refreshAll();
            } catch (e) { ElMessage.error(e.message); }
            finally { resetting.value = false; }
        }

        return {
            jobs, libs, loading, resetting, retryingId, stats, statsFailed,
            filters, page, pageSize, selectedJob, detailCardRef, truncated, libsFailed,
            filtered, paged, load, refreshAll, resetFilters, openDetail, closeDetail, retry, resetFailed,
            isSelected,
            STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL,
            formatJobTime, jobDuration, shortId, libraryName, taskEmpty,
            buildModeLabel, publicationStatusLabel, formatEta,
        };
    },
    template: `
    <div class="jobs-workspace">
      <header class="jobs-header">
        <div>
          <h2 class="jobs-title">任务监控</h2>
          <p class="jobs-desc">持续监控文件导入、向量化、知识图谱抽取与自动发布</p>
        </div>
        <div class="jobs-header-actions">
          <el-button @click="refreshAll" :loading="loading">刷新</el-button>
        </div>
      </header>

      <section class="jobs-stats">
        <div class="jobs-stat jobs-stat--pending">
          <local-icon icon="status:pending" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.pending }}</b><span>待处理</span></div>
        </div>
        <div class="jobs-stat jobs-stat--processing">
          <local-icon icon="status:processing" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.processing }}</b><span>处理中</span></div>
        </div>
        <div class="jobs-stat jobs-stat--done">
          <local-icon icon="status:success" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.done }}</b><span>已完成</span></div>
        </div>
        <div class="jobs-stat jobs-stat--failed">
          <local-icon icon="status:failed" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.failed }}</b><span>失败</span></div>
        </div>
      </section>

      <section class="jobs-toolbar">
        <div class="jobs-toolbar-row">
          <span class="jobs-toolbar-label">任务类型</span>
          <el-select v-model="filters.task_type" class="jobs-filter-status" placeholder="全部" clearable @change="page=1">
            <el-option label="文件导入" value="import" />
            <el-option label="向量化" value="embedding" />
            <el-option label="知识图谱" value="graph" />
          </el-select>
          <span class="jobs-toolbar-label">知识库</span>
          <el-select v-model="filters.library_id" class="jobs-filter" placeholder="全部" clearable @change="page=1">
            <el-option v-for="l in libs" :key="l.id" :label="l.name + ' (' + l.slug + ')'" :value="l.id" />
          </el-select>
          <span class="jobs-toolbar-label">状态</span>
          <el-select v-model="filters.status" class="jobs-filter-status" placeholder="全部" clearable @change="page=1">
            <el-option label="待处理" value="pending" /><el-option label="处理中" value="processing" />
            <el-option label="已完成" value="done" /><el-option label="已失败" value="failed" />
            <el-option label="已取消" value="cancelled" /><el-option label="已覆盖" value="superseded" />
          </el-select>
          <span class="jobs-toolbar-label">Worker ID</span>
          <el-input v-model="filters.worker_id" class="jobs-filter-text" placeholder="Worker ID" clearable @input="page=1" />
          <span class="jobs-toolbar-label">文档 ID</span>
          <el-input v-model="filters.document_id" class="jobs-filter-text" placeholder="文档 ID" clearable @input="page=1" />
        </div>
        <div class="jobs-toolbar-row">
          <span class="jobs-toolbar-label">起始</span>
          <el-date-picker v-model="filters.dateFrom" class="jobs-filter-date" type="date" placeholder="创建开始" value-format="YYYY-MM-DD" @change="page=1" />
          <span class="jobs-toolbar-label">截止</span>
          <el-date-picker v-model="filters.dateTo" class="jobs-filter-date" type="date" placeholder="创建结束" value-format="YYYY-MM-DD" @change="page=1" />
          <el-button @click="resetFilters">重置筛选</el-button>
          <el-button type="danger" plain :disabled="resetting || (!filters.library_id && !statsFailed && !stats.retryable_failed)" :loading="resetting" @click="resetFailed">
            重置失败向量任务<span v-if="!filters.library_id && !statsFailed && stats.retryable_failed"> ({{ stats.retryable_failed }})</span>
          </el-button>
        </div>
      </section>

      <el-alert v-if="truncated" type="info" :closable="false" show-icon title="结果可能被截断，仅展示前500条" />
      <el-alert v-if="libsFailed" type="warning" :closable="false" show-icon title="知识库列表加载失败，不影响任务查看" />

      <section class="jobs-table-card">
        <div class="jobs-table-shell">
          <el-table :data="paged.items" v-loading="loading"
                    @row-click="openDetail"
                    :row-class-name="function({row}){return isSelected(row) ? 'jobs-row-selected' : ''}">
            <template #empty>
              <div class="illustration-empty-wrapper">
                <img :src="taskEmpty" class="illustration-task-empty" alt="" aria-hidden="true" />
                <p>暂无任务</p>
              </div>
            </template>
            <el-table-column label="任务 / 文档" min-width="200">
              <template #default="{row}">
                <div class="jobs-doc-id">{{ row.title || shortId(row.document_id) }}</div>
                <div class="jobs-doc-ver">{{ shortId(row.document_id) }}<span v-if="row.document_revision"> · v{{ row.document_revision }}</span></div>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="100" align="center">
              <template #default="{row}">{{ TASK_TYPE_LABEL[row.task_type] || row.task_type }}</template>
            </el-table-column>
            <el-table-column label="知识库" min-width="140">
              <template #default="{row}">{{ libraryName(libs, row.library_id) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="130" align="center">
              <template #default="{row}">
                <local-icon v-if="STATUS_ICON[row.status]" :icon="STATUS_ICON[row.status]" class="jobs-status-icon" />
                <el-tag :type="STATUS_TAG[row.status]" size="small">{{ STATUS_LABEL[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="当前阶段" min-width="140">
              <template #default="{row}">{{ STAGE_LABEL[row.stage] || row.stage || '—' }}</template>
            </el-table-column>
            <el-table-column label="Worker" width="120" show-overflow-tooltip prop="worker_id" />
            <el-table-column label="尝试" width="60" align="center" prop="attempt_count" />
            <el-table-column label="耗时" width="80" align="center">
              <template #default="{row}">{{ jobDuration(row) }}</template>
            </el-table-column>
            <el-table-column label="最后错误" min-width="160" show-overflow-tooltip>
              <template #default="{row}"><span class="jobs-error-text">{{ row.last_error || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="创建时间" width="150">
              <template #default="{row}">{{ formatJobTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="160" align="center" fixed="right">
              <template #default="{row}">
                <el-button size="small" type="primary"
                           :aria-expanded="isSelected(row) ? 'true' : 'false'"
                           @click.stop="openDetail(row)">
                  {{ isSelected(row) ? '收起详情' : '查看详情' }}
                </el-button>
                <el-button size="small" link type="primary"
                           :loading="retryingId === row.id"
                           :disabled="!row.retryable"
                           @click.stop="retry(row)">
                  <local-icon icon="status:retry" class="jobs-retry-icon" />重试
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="jobs-detail-card" v-if="selectedJob" ref="detailCardRef">
          <div class="jobs-detail-header">
            <span class="jobs-detail-title">任务详情 · {{ shortId(selectedJob.document_id) }}</span>
            <el-button size="small" @click="closeDetail">收起</el-button>
          </div>
          <dl class="jobs-detail-meta">
            <dt>任务 ID</dt><dd class="mono">{{ selectedJob.id }}</dd>
            <dt>文档 ID</dt><dd class="mono">{{ selectedJob.document_id }}</dd>
            <dt>任务类型</dt><dd>{{ TASK_TYPE_LABEL[selectedJob.task_type] || selectedJob.task_type }}</dd>
            <dt>知识库</dt><dd>{{ libraryName(libs, selectedJob.library_id) }}</dd>
            <dt>状态</dt><dd><el-tag :type="STATUS_TAG[selectedJob.status]" size="small">{{ STATUS_LABEL[selectedJob.status] || selectedJob.status }}</el-tag></dd>
            <dt>当前阶段</dt><dd>{{ STAGE_LABEL[selectedJob.stage] || selectedJob.stage || '—' }}</dd>
            <dt>原始状态</dt><dd>{{ selectedJob.raw_status }}</dd>
            <template v-if="selectedJob.task_type === 'graph'">
              <dt>构建模式</dt><dd>{{ buildModeLabel(selectedJob.build_mode) }}</dd>
              <dt>抽取进度</dt><dd>{{ selectedJob.progress?.completed || 0 }} / {{ selectedJob.progress?.total || 0 }} Unit（{{ selectedJob.progress?.percent || 0 }}%）</dd>
              <dt>批次进度</dt><dd>{{ selectedJob.progress?.completed_batches || 0 }} / {{ selectedJob.progress?.planned_batches || 0 }} Batch（估算）</dd>
              <dt>自动发布</dt><dd>{{ publicationStatusLabel(selectedJob.publication_status) }}</dd>
              <dt>实时并发</dt><dd>配置 {{ selectedJob.metrics?.configured_concurrency ?? '—' }} / 生效 {{ selectedJob.metrics?.effective_concurrency ?? '—' }} / 处理中 {{ selectedJob.metrics?.in_flight ?? 0 }}</dd>
              <dt>预计剩余</dt><dd>{{ formatEta(selectedJob.metrics?.eta_seconds) }}</dd>
              <dt>缓存命中</dt><dd>{{ selectedJob.metrics?.cache_hits || 0 }}</dd>
              <dt>限流 / 重试</dt><dd>{{ selectedJob.metrics?.throttled_count || 0 }} / {{ selectedJob.metrics?.retry_count || 0 }}</dd>
            </template>
            <dt>Worker</dt><dd>{{ selectedJob.worker_id || '—' }}</dd>
            <dt>尝试次数</dt><dd>{{ selectedJob.attempt_count || 0 }}</dd>
            <dt>耗时</dt><dd>{{ jobDuration(selectedJob) }}</dd>
            <dt>创建时间</dt><dd>{{ formatJobTime(selectedJob.created_at) }}</dd>
            <dt>开始时间</dt><dd>{{ formatJobTime(selectedJob.claimed_at) }}</dd>
            <dt>完成时间</dt><dd>{{ formatJobTime(selectedJob.finished_at) }}</dd>
            <dt>最后错误</dt><dd class="jobs-error-text">{{ selectedJob.last_error || '—' }}</dd>
          </dl>
        </div>

        <div class="jobs-pagination">
          <span>共 {{ paged.total }} 条</span>
          <el-pagination
              v-model:current-page="page" v-model:page-size="pageSize"
              :total="paged.total" :page-sizes="[10, 20, 50]"
              layout="sizes, prev, pager, next"
          />
        </div>
      </section>
    </div>
    `,
};
