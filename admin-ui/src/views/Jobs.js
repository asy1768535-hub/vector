import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { taskEmpty } from '../illustrations.js';
import { STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL, formatJobTime, jobDuration, shortId, filterJobs, paginateJobs, libraryName, jobErrorText, jobStageLabel, retryReasonLabel, statsStatusTotal, retryTargetKey, isRetrySelectable, uniqueRetryRows, retryItem, retryTypeSummary } from '../jobs_ui.js';

const BUILD_MODE_LABEL = { fast: '快速', standard: '标准', deep: '深度' };
const PUBLICATION_STATUS_LABEL = {
    pending: '等待抽取完成',
    publishing: '自动发布中',
    available: '已发布可用',
    retryable: '自动发布失败，可重试；当前正式图谱未切换',
    entities_only: '仅有实体、暂无有效关系，未发布',
    not_required: '无合格事实，无需发布',
};
const PUBLICATION_FAILURE_REASON_LABEL = {
    no_valid_relation: '仅有实体、暂无有效关系',
    job_not_publishable: '任务未满足自动发布条件',
};

function buildModeLabel(value) {
    return BUILD_MODE_LABEL[value] || BUILD_MODE_LABEL.standard;
}

function publicationStatusLabel(value) {
    return PUBLICATION_STATUS_LABEL[value] || '等待抽取完成';
}

function publicationFailureReasonLabel(value) {
    return PUBLICATION_FAILURE_REASON_LABEL[value] || value || '—';
}

function publicationDiffLabel(value) {
    if (!value?.entity || !value?.relation) return '—';
    const summary = (label, item) => (
        `${label}：新增 ${item.added || 0}，保留 ${item.retained || 0}，变化 ${item.changed || 0}，移除 ${item.removed || 0}`
    );
    return `${summary('实体', value.entity)}；${summary('关系', value.relation)}`;
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
        const retryingSelected = ref(false);
        const selectedFailedJobs = ref([]);
        const advancedOpen = ref(false);
        const emptyStats = () => ({
            pending: 0, processing: 0, done: 0, failed: 0,
            cancelled: 0, superseded: 0, retryable_failed: 0, retryable_embedding_failed: 0, total: 0,
        });
        const stats = ref(emptyStats());
        const statsFailed = ref(false);
        const filters = reactive({ task_type: '', status: '', library_id: '', worker_id: '', document_id: '', dateFrom: '', dateTo: '' });
        const page = ref(1);
        const pageSize = ref(10);
        const selectedJob = ref(null);
        const detailOpen = ref(false);
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
                truncated.value = (jr.value || []).length >= 1500;
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
        const retryableCount = computed(() => uniqueRetryRows(filtered.value).length);
        const canResetFailed = computed(() => filters.library_id
            ? jobs.value.some((job) => job.library_id === filters.library_id
                && isRetrySelectable(job) && job.retry_target_type === 'embedding')
            : !statsFailed.value && stats.value.retryable_embedding_failed > 0);

        watch([filtered, paged], () => {
            if (selectedJob.value && !paged.value.items.some((job) => job.id === selectedJob.value.id)) {
                closeDetail();
            }
        });

        function resetFilters() {
            Object.assign(filters, { task_type: '', status: '', library_id: '', worker_id: '', document_id: '', dateFrom: '', dateTo: '' });
            page.value = 1;
        }

        function isSelected(row) {
            return selectedJob.value && selectedJob.value.id === row.id;
        }

        function openDetail(row, _column, event) {
            if (event?.target?.closest('button, input, .el-checkbox')) return;
            selectedJob.value = row;
            detailOpen.value = true;
        }

        function closeDetail() { detailOpen.value = false; }
        function clearDetail() { selectedJob.value = null; }

        async function retry(row) {
            if (retryingId.value) return;
            if (!isRetrySelectable(row)) return;
            retryingId.value = retryTargetKey(row);
            try {
                const response = await api.retryMonitoredTasks([retryItem(row)]);
                const result = response.results?.[0];
                if (result?.status === 'succeeded') ElMessage.success(result.message);
                else ElMessage.warning(result?.message || '重试被拒绝');
                await refreshAll();
            } catch (e) { ElMessage.error(e.message); }
            finally { retryingId.value = null; }
            return;
        }

        function onFailedSelectionChange(rows) {
            selectedFailedJobs.value = uniqueRetryRows(rows);
        }

        function retryResultSummary(results) {
            return (results || [])
                .filter((result) => result.status === 'rejected')
                .map((result) => `${result.task_type}:${shortId(result.job_id)} ${result.message}`)
                .join('；');
        }

        async function retrySelected() {
            const rows = uniqueRetryRows(selectedFailedJobs.value);
            if (!rows.length || retryingSelected.value) return;
            try {
                await ElMessageBox.confirm(
                    `将重试所选 ${rows.length} 条失败任务（${retryTypeSummary(rows)}），确认继续？`,
                    '重试所选任务',
                    { type: 'warning', confirmButtonText: '重试', cancelButtonText: '取消' },
                );
            } catch (_) { return; }
            retryingSelected.value = true;
            try {
                const response = await api.retryMonitoredTasks(rows.map(retryItem));
                const results = response.results || [];
                const succeeded = results.filter((result) => result.status === 'succeeded').length;
                const rejected = results.length - succeeded;
                if (succeeded) ElMessage.success(`已重试 ${succeeded} 条任务`);
                if (rejected) ElMessage.warning(`有 ${rejected} 条任务被拒绝：${retryResultSummary(results)}`);
                await refreshAll();
            } catch (e) { ElMessage.error(e.message); }
            finally { retryingSelected.value = false; }
            return;
        }

        function handleMoreAction(command) {
            if (command === 'reset-failed') resetFailed();
        }

        async function resetFailed() {
            if (!filters.library_id && statsFailed.value) {
                ElMessage.info('当前无法确定可重试任务数量，请先刷新统计');
                return;
            }
            if (filters.library_id) {
                try {
                    await ElMessageBox.confirm(
                        '将重试当前筛选知识库中的全部失败向量任务，处理服务会自动重新执行。确定？',
                        '重试当前知识库的全部失败向量任务', { type: 'warning', confirmButtonText: '重试全部', cancelButtonText: '取消' }
                    );
                } catch (_) { return; }
            } else {
                if (statsFailed.value) {
                    try {
                        await ElMessageBox.confirm(
                            '将重试系统中的全部失败向量任务，处理服务会自动重新执行。确定？',
                            '重试全部失败的向量任务', { type: 'warning', confirmButtonText: '重试全部', cancelButtonText: '取消' }
                        );
                    } catch (_) { return; }
                } else {
                    if (!stats.value.retryable_failed) { ElMessage.info('当前没有可重试的失败向量任务'); return; }
                    try {
                        await ElMessageBox.confirm(
                            `将重试系统中的全部 ${stats.value.retryable_failed} 条失败向量任务，处理服务会自动重新执行。确定？`,
                            '重试全部失败的向量任务', { type: 'warning', confirmButtonText: '重试全部', cancelButtonText: '取消' }
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
            jobs, libs, loading, resetting, retryingId, retryingSelected, selectedFailedJobs, advancedOpen, stats, statsFailed,
            filters, page, pageSize, selectedJob, detailOpen, truncated, libsFailed,
            filtered, paged, retryableCount, canResetFailed, load, refreshAll, resetFilters, openDetail, closeDetail, retry, retrySelected, resetFailed,
            clearDetail, isSelected, retryTargetKey, isRetrySelectable, onFailedSelectionChange, handleMoreAction,
            STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL,
            formatJobTime, jobDuration, shortId, libraryName, taskEmpty,
            buildModeLabel, publicationStatusLabel, publicationFailureReasonLabel,
            publicationDiffLabel, formatEta, jobErrorText, jobStageLabel, retryReasonLabel, statsStatusTotal,
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
          <el-dropdown trigger="click" @command="handleMoreAction">
            <el-button>更多操作</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="reset-failed" :disabled="resetting || !canResetFailed">
                  {{ filters.library_id ? '重试当前知识库的全部失败向量任务' : '重试全部失败的向量任务' }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button class="app-refresh-button" @click="refreshAll" :loading="loading">
            <span class="app-refresh-icon" aria-hidden="true"></span>刷新
          </el-button>
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
        <div class="jobs-stat jobs-stat--cancelled">
          <local-icon icon="status:skipped" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.cancelled }}</b><span>已取消</span></div>
        </div>
        <div class="jobs-stat jobs-stat--superseded">
          <local-icon icon="status:skipped" class="jobs-stat-icon" />
          <div class="jobs-stat-body"><b>{{ statsFailed ? '—' : stats.superseded }}</b><span>已覆盖</span></div>
        </div>
        <div class="jobs-stats-total">统计总数：{{ statsFailed ? '—' : statsStatusTotal(stats) }} / {{ statsFailed ? '—' : stats.total }}</div>
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
          <el-button @click="resetFilters">重置筛选</el-button>
          <el-button text @click="advancedOpen = !advancedOpen">{{ advancedOpen ? '收起高级筛选' : '高级筛选' }}</el-button>
        </div>
        <div v-show="advancedOpen" class="jobs-toolbar-row jobs-advanced-filters">
          <span class="jobs-toolbar-label">处理服务（Worker）ID</span>
          <el-input v-model="filters.worker_id" class="jobs-filter-text" placeholder="处理服务 ID" clearable @input="page=1" />
          <span class="jobs-toolbar-label">文档 ID</span>
          <el-input v-model="filters.document_id" class="jobs-filter-text" placeholder="文档 ID" clearable @input="page=1" />
          <span class="jobs-toolbar-label">起始</span>
          <el-date-picker v-model="filters.dateFrom" class="jobs-filter-date" type="date" placeholder="创建开始" value-format="YYYY-MM-DD" @change="page=1" />
          <span class="jobs-toolbar-label">截止</span>
          <el-date-picker v-model="filters.dateTo" class="jobs-filter-date" type="date" placeholder="创建结束" value-format="YYYY-MM-DD" @change="page=1" />
        </div>
      </section>

      <el-alert v-if="truncated" type="info" :closable="false" show-icon title="结果可能被截断，仅展示前1500条" />
      <el-alert v-if="libsFailed" type="warning" :closable="false" show-icon title="知识库列表加载失败，不影响任务查看" />

      <section class="jobs-table-card">
        <div class="jobs-table-actions">
          <span>仅失败且可重试的任务可以勾选；当前可选择 {{ retryableCount }} 条。表格可横向滚动查看全部列</span>
          <el-button type="primary" plain :disabled="!selectedFailedJobs.length || !retryableCount" :loading="retryingSelected" @click="retrySelected">
            重试所选<span v-if="selectedFailedJobs.length">（{{ selectedFailedJobs.length }}）</span>
          </el-button>
          <span v-if="!retryableCount" class="jobs-retry-unavailable">当前失败任务均不可重试</span>
        </div>
        <div class="jobs-table-shell">
          <el-table :data="paged.items" v-loading="loading"
                    @row-click="openDetail"
                    @selection-change="onFailedSelectionChange"
                    :row-class-name="function({row}){return isSelected(row) ? 'jobs-row-selected' : ''}">
            <template #empty>
              <div class="illustration-empty-wrapper">
                <img :src="taskEmpty" class="illustration-task-empty" alt="" aria-hidden="true" />
                <p>暂无任务</p>
              </div>
            </template>
            <el-table-column type="selection" width="46" :selectable="isRetrySelectable" />
            <el-table-column label="任务 / 文档" min-width="200">
              <template #default="{row}">
                <div class="jobs-doc-id" :title="row.title || row.document_id">{{ row.title || shortId(row.document_id) }}</div>
                <div class="jobs-doc-ver">{{ shortId(row.document_id) }}<span v-if="row.document_revision"> · v{{ row.document_revision }}</span></div>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="100" align="center">
              <template #default="{row}">{{ TASK_TYPE_LABEL[row.task_type] || row.task_type }}</template>
            </el-table-column>
            <el-table-column label="知识库" min-width="140">
              <template #default="{row}"><span class="jobs-library-name" :title="libraryName(libs, row.library_id)">{{ libraryName(libs, row.library_id) }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="130" align="center">
              <template #default="{row}">
                <local-icon v-if="STATUS_ICON[row.status]" :icon="STATUS_ICON[row.status]" class="jobs-status-icon" />
                <el-tag :type="STATUS_TAG[row.status]" size="small">{{ STATUS_LABEL[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="当前阶段" min-width="140">
              <template #default="{row}">{{ jobStageLabel(row) }}</template>
            </el-table-column>
            <el-table-column label="处理服务" width="120" show-overflow-tooltip prop="worker_id" />
            <el-table-column label="尝试" width="60" align="center" prop="attempt_count" />
            <el-table-column label="耗时" width="80" align="center">
              <template #default="{row}">{{ jobDuration(row) }}</template>
            </el-table-column>
            <el-table-column label="最后错误" min-width="160" show-overflow-tooltip>
              <template #default="{row}"><span class="jobs-error-text" :title="jobErrorText(row)">{{ jobErrorText(row) }}</span></template>
            </el-table-column>
            <el-table-column label="创建时间" width="150">
              <template #default="{row}">{{ formatJobTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="190" align="center">
              <template #default="{row}">
                <el-button size="small" type="primary"
                           :aria-expanded="isSelected(row) ? 'true' : 'false'"
                           @click.stop="openDetail(row)">
                  查看详情
                </el-button>
                <el-button v-if="row.retryable" size="small" link type="primary"
                           :loading="retryingId === retryTargetKey(row)"
                           @click.stop="retry(row)">
                  <local-icon icon="status:retry" class="jobs-retry-icon" />重试
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <el-dialog v-if="selectedJob" v-model="detailOpen" class="jobs-detail-dialog" width="820px" top="5vh" @closed="clearDetail">
          <div class="jobs-detail-header">
            <span class="jobs-detail-title">任务详情 · {{ shortId(selectedJob.document_id) }}</span>
            <el-button size="small" @click="closeDetail">收起</el-button>
          </div>
          <el-alert v-if="selectedJob.task_type === 'graph' && selectedJob.publication_status === 'entities_only'"
                    type="warning" :closable="false" show-icon
                    title="仅有实体、暂无有效关系，已保留实体候选供审核，未发布为可用图谱。" />
          <el-alert v-if="selectedJob.task_type === 'graph' && selectedJob.metrics?.current_graph_unchanged"
                    type="info" :closable="false" show-icon
                    title="本次更新没有切换正式图谱；如果此前已有发布版本，该版本仍被保留，未验证结果不会混入问答。" />
          <dl class="jobs-detail-meta">
            <dt>任务 ID</dt><dd class="mono">{{ selectedJob.id }}</dd>
            <dt>文档 ID</dt><dd class="mono">{{ selectedJob.document_id }}</dd>
            <dt>任务类型</dt><dd>{{ TASK_TYPE_LABEL[selectedJob.task_type] || selectedJob.task_type }}</dd>
            <dt>知识库</dt><dd>{{ libraryName(libs, selectedJob.library_id) }}</dd>
            <dt>状态</dt><dd><el-tag :type="STATUS_TAG[selectedJob.status]" size="small">{{ STATUS_LABEL[selectedJob.status] || selectedJob.status }}</el-tag></dd>
            <dt>当前阶段</dt><dd>{{ jobStageLabel(selectedJob) }}</dd>
            <dt>原始状态</dt><dd>{{ selectedJob.raw_status }}</dd>
            <dt>重试能力</dt><dd>{{ retryReasonLabel(selectedJob) }}</dd>
            <template v-if="selectedJob.task_type === 'graph'">
              <dt>构建模式</dt><dd>{{ buildModeLabel(selectedJob.build_mode) }}</dd>
              <dt>抽取进度</dt><dd>{{ selectedJob.progress?.completed || 0 }} / {{ selectedJob.progress?.total || 0 }} Unit（{{ selectedJob.progress?.percent || 0 }}%）</dd>
              <dt>批次进度</dt><dd>{{ selectedJob.progress?.completed_batches || 0 }} / {{ selectedJob.progress?.planned_batches || 0 }} Batch（估算）</dd>
              <dt>自动发布</dt><dd>{{ publicationStatusLabel(selectedJob.publication_status) }}</dd>
              <dt>抽取数量</dt><dd>实体 {{ selectedJob.metrics?.stage_counts?.extraction?.entities ?? 0 }} / 关系 {{ selectedJob.metrics?.stage_counts?.extraction?.relations ?? 0 }}</dd>
              <dt>校验通过</dt><dd>实体 {{ selectedJob.metrics?.stage_counts?.validation?.entities ?? 0 }} / 关系 {{ selectedJob.metrics?.stage_counts?.validation?.relations ?? 0 }}</dd>
              <dt>物化数量</dt><dd>实体 {{ selectedJob.metrics?.stage_counts?.materialization?.entities ?? 0 }} / 关系 {{ selectedJob.metrics?.stage_counts?.materialization?.relations ?? 0 }}</dd>
              <dt>发布数量</dt><dd>实体 {{ selectedJob.metrics?.stage_counts?.publication?.entities ?? 0 }} / 关系 {{ selectedJob.metrics?.stage_counts?.publication?.relations ?? 0 }}</dd>
              <template v-if="selectedJob.metrics?.publication_diff">
                <dt>图谱变化</dt><dd>{{ publicationDiffLabel(selectedJob.metrics.publication_diff) }}</dd>
              </template>
              <dt>未发布原因</dt><dd>{{ publicationFailureReasonLabel(selectedJob.metrics?.failure_reasons?.publication) }}</dd>
              <dt>实时并发</dt><dd>配置 {{ selectedJob.metrics?.configured_concurrency ?? '—' }} / 生效 {{ selectedJob.metrics?.effective_concurrency ?? '—' }} / 处理中 {{ selectedJob.metrics?.in_flight ?? 0 }}</dd>
              <dt>预计剩余</dt><dd>{{ formatEta(selectedJob.metrics?.eta_seconds) }}</dd>
              <dt>缓存命中</dt><dd>{{ selectedJob.metrics?.cache_hits || 0 }}</dd>
              <dt>限流 / 重试</dt><dd>{{ selectedJob.metrics?.throttled_count || 0 }} / {{ selectedJob.metrics?.retry_count || 0 }}</dd>
            </template>
            <dt>处理服务（Worker）</dt><dd>{{ selectedJob.worker_id || '—' }}</dd>
            <dt>尝试次数</dt><dd>{{ selectedJob.attempt_count || 0 }}</dd>
            <dt>耗时</dt><dd>{{ jobDuration(selectedJob) }}</dd>
            <dt>创建时间</dt><dd>{{ formatJobTime(selectedJob.created_at) }}</dd>
            <dt>开始时间</dt><dd>{{ formatJobTime(selectedJob.claimed_at) }}</dd>
            <dt>完成时间</dt><dd>{{ formatJobTime(selectedJob.finished_at) }}</dd>
            <dt>最后错误</dt><dd class="jobs-error-text">{{ jobErrorText(selectedJob) }}</dd>
            <dt>原始错误</dt><dd class="jobs-error-text">{{ selectedJob.raw_error || '历史任务未记录错误' }}</dd>
          </dl>
        </el-dialog>

        <div class="jobs-pagination">
          <span>当前列表 {{ paged.total }} 条{{ truncated ? '（最多展示前1500条）' : '' }}</span>
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
