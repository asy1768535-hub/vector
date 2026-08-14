import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import {
    SERVICE_LABELS, STATUS_TAG, STATUS_TEXT, relTime,
    computeRuntimeSummary, formatOperationTime, rebuildStatusMeta,
} from '../operations_ui.js';
import { serviceError } from '../illustrations.js';

const SERVICE_ICON = {
    api: 'service:api',
    embedding_worker: 'service:embedding-worker',
    cleanup_worker: 'service:cleanup-worker',
};
const REFRESH_SECONDS = 30;

export default {
    setup() {
        const data = ref(null);
        const loading = ref(false);
        const loadError = ref(false);
        const autoRefresh = ref(true);
        const dataFetchedAt = ref(null);
        let refreshTimer = null;
        let loadingSeq = 0;

        async function load(forceRefresh = false) {
            if (loading.value) return;
            const seq = ++loadingSeq;
            loading.value = true;
            try {
                const d = await api.operationsStatus(forceRefresh);
                if (seq !== loadingSeq) return;
                data.value = d;
                dataFetchedAt.value = d.now || new Date().toISOString();
                loadError.value = false;
            } catch (e) {
                if (seq !== loadingSeq) return;
                if (!data.value) loadError.value = true;
                ElMessage.error(e.message);
            } finally {
                if (seq === loadingSeq) loading.value = false;
            }
        }

        function startAutoRefresh() {
            if (refreshTimer) return;
            refreshTimer = setInterval(() => {
                if (!autoRefresh.value || loading.value) return;
                if (typeof document !== 'undefined' && document.hidden) return;
                load(true);
            }, REFRESH_SECONDS * 1000);
        }

        function stopAutoRefresh() {
            if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
        }

        watch(autoRefresh, (on) => { if (on) startAutoRefresh(); else stopAutoRefresh(); });

        onMounted(() => { load(false); startAutoRefresh(); });
        onBeforeUnmount(() => { stopAutoRefresh(); });

        const summary = computed(() => computeRuntimeSummary(data.value));

        const services = computed(() => {
            const list = (data.value && data.value.services) || [];
            return list.map((s) => {
                const latest = s.latest;
                return {
                    ...s,
                    label: SERVICE_LABELS[s.service_type] || s.service_type,
                    icon: SERVICE_ICON[s.service_type] || null,
                    tagType: STATUS_TAG[s.status] || 'info',
                    statusText: STATUS_TEXT[s.status] || s.status,
                    instances: s.online_instances != null && s.known_instances != null
                        ? `${s.online_instances} / ${s.known_instances}`
                        : '—',
                    lastSeenAt: latest && latest.last_seen_at ? formatOperationTime(latest.last_seen_at) : '—',
                    lastSeenRel: latest ? relTime(latest.seconds_since_last_seen) : '—',
                    hostname: latest ? latest.hostname : '—',
                    pid: latest ? String(latest.pid) : '—',
                };
            });
        });

        const jobs = computed(() => (data.value && data.value.embedding_jobs) || {});
        const outbox = computed(() => (data.value && data.value.cleanup_outbox) || {});
        const libraries = computed(() => (data.value && data.value.libraries) || {});
        const rebuilds = computed(() => (data.value && data.value.rebuild_operations) || []);

        return {
            data, loading, loadError, autoRefresh, dataFetchedAt,
            summary, services, jobs, outbox, libraries, rebuilds,
            load, serviceError, formatOperationTime, rebuildStatusMeta,
        };
    },
    template: `
    <div class="runtime-workspace">
      <header class="runtime-header">
        <div>
          <h2 class="runtime-title"><local-icon icon="sidebar:runtime" class="runtime-title-icon" />运行状态</h2>
          <p class="runtime-desc">监控服务进程和后台任务的运行情况</p>
        </div>
      </header>

      <!-- ═══ 顶部总览 ═══ -->
      <section class="runtime-overview">
        <div class="runtime-overview-item">
          <local-icon icon="service:api" class="runtime-overview-icon runtime-overview-icon--online" />
          <div class="runtime-overview-body">
            <b>{{ summary.onlineServices != null ? summary.onlineServices : '—' }}</b>
            <span>在线服务</span>
          </div>
        </div>
        <div class="runtime-overview-item">
          <local-icon icon="status:failed" class="runtime-overview-icon runtime-overview-icon--abnormal" />
          <div class="runtime-overview-body">
            <b>{{ summary.abnormalServices != null ? summary.abnormalServices : '—' }}</b>
            <span>异常服务</span>
          </div>
        </div>
        <div class="runtime-overview-item">
          <local-icon icon="service:embedding-worker" class="runtime-overview-icon runtime-overview-icon--embedding" />
          <div class="runtime-overview-body">
            <b>{{ summary.embeddingPending != null ? summary.embeddingPending : '—' }}</b>
            <span>向量化队列</span>
          </div>
        </div>
        <div class="runtime-overview-item">
          <local-icon icon="service:cleanup-worker" class="runtime-overview-icon runtime-overview-icon--cleanup" />
          <div class="runtime-overview-body">
            <b>{{ summary.cleanupPending != null ? summary.cleanupPending : '—' }}</b>
            <span>Cleanup 待处理</span>
          </div>
        </div>
        <div class="runtime-overview-item">
          <local-icon icon="status:processing" class="runtime-overview-icon runtime-overview-icon--rebuilding" />
          <div class="runtime-overview-body">
            <b>{{ summary.rebuilding != null ? summary.rebuilding : '—' }}</b>
            <span>重建中</span>
          </div>
        </div>
      </section>

      <!-- ═══ 首次加载失败 ═══ -->
      <section class="runtime-error-state" v-if="!data && loadError">
        <div class="illustration-empty-wrapper">
          <img :src="serviceError" class="illustration-service-error" alt="" aria-hidden="true" />
          <p>服务状态暂不可用</p>
          <el-button type="primary" size="small" @click="load(false)" :loading="loading">重新加载</el-button>
        </div>
      </section>

      <!-- ═══ 服务在线状态 ═══ -->
      <section class="runtime-card" v-if="data">
        <div class="runtime-card-header">
          <span class="runtime-card-title">服务在线状态</span>
          <div class="runtime-card-header-actions">
            <el-button class="app-refresh-button" size="small" @click="load(true)" :loading="loading">
              <span class="app-refresh-icon" aria-hidden="true"></span>刷新
            </el-button>
            <span class="runtime-auto-label">自动刷新</span>
            <el-switch v-model="autoRefresh" size="small" />
          </div>
        </div>
        <div class="runtime-table-shell">
          <el-table :data="services" v-loading="loading" border>
            <template #empty>
              <div class="illustration-empty-wrapper">
                <img :src="serviceError" class="illustration-service-error" alt="" aria-hidden="true" />
                <p>服务状态暂不可用</p>
              </div>
            </template>
            <el-table-column label="服务" min-width="200">
              <template #default="{row}">
                <local-icon v-if="row.icon" :icon="row.icon" class="runtime-service-icon" />
                <span class="runtime-service-name">{{ row.label }}</span>
              </template>
            </el-table-column>
            <el-table-column label="在线状态" width="120" align="center">
              <template #default="{row}">
                <el-tag :type="row.tagType" size="small" effect="light">{{ row.statusText }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="在线 / 已知实例" width="140" align="center">
              <template #default="{row}">{{ row.instances }}</template>
            </el-table-column>
            <el-table-column label="最后心跳" min-width="180">
              <template #default="{row}">
                <div>{{ row.lastSeenAt }}</div>
                <div class="runtime-heartbeat-rel">{{ row.lastSeenRel }}</div>
              </template>
            </el-table-column>
            <el-table-column label="主机" min-width="140" show-overflow-tooltip>
              <template #default="{row}">{{ row.hostname }}</template>
            </el-table-column>
            <el-table-column label="PID" width="90" align="center">
              <template #default="{row}">{{ row.pid }}</template>
            </el-table-column>
          </el-table>
        </div>
        <div class="runtime-table-footer">
          <span>共 {{ services.length }} 条</span>
          <span class="runtime-table-time">数据时间：{{ dataFetchedAt ? formatOperationTime(dataFetchedAt) : '—' }}</span>
        </div>
      </section>

      <!-- ═══ 底部三列卡片 ═══ -->
      <section class="runtime-bottom-grid" v-if="data">
        <!-- Embedding 任务 -->
        <div class="runtime-card">
          <div class="runtime-card-header">
            <span class="runtime-card-title">向量化任务</span>
          </div>
          <div class="runtime-card-body">
            <div class="runtime-metrics">
              <div class="runtime-metric"><span class="runtime-metric-label">总计</span><b>{{ jobs.total != null ? jobs.total : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">待处理</span><b class="runtime-metric--pending">{{ jobs.pending != null ? jobs.pending : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">处理中</span><b>{{ jobs.processing != null ? jobs.processing : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">已完成</span><b class="runtime-metric--done">{{ jobs.done != null ? jobs.done : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">失败</span><b class="runtime-metric--failed">{{ jobs.failed != null ? jobs.failed : '—' }}</b></div>
            </div>
            <router-link to="/operations-center/jobs" class="runtime-card-link">查看任务监控</router-link>
          </div>
        </div>

        <!-- Cleanup Outbox -->
        <div class="runtime-card">
          <div class="runtime-card-header">
            <span class="runtime-card-title">Cleanup Outbox</span>
          </div>
          <div class="runtime-card-body">
            <div class="runtime-metrics">
              <div class="runtime-metric"><span class="runtime-metric-label">总计</span><b>{{ outbox.total != null ? outbox.total : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">待处理</span><b class="runtime-metric--pending">{{ outbox.pending != null ? outbox.pending : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">处理中</span><b>{{ outbox.processing != null ? outbox.processing : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">已完成</span><b class="runtime-metric--done">{{ outbox.done != null ? outbox.done : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">失败</span><b class="runtime-metric--failed">{{ outbox.failed != null ? outbox.failed : '—' }}</b></div>
              <div class="runtime-metric"><span class="runtime-metric-label">死信</span><b class="runtime-metric--dead">{{ outbox.dead_letter != null ? outbox.dead_letter : '—' }}</b></div>
            </div>
            <div class="runtime-card-note">
              该队列异步清理已删除文档的向量数据，确保资源及时释放。
            </div>
          </div>
        </div>

        <!-- 重建状态 -->
        <div class="runtime-card">
          <div class="runtime-card-header">
            <span class="runtime-card-title">重建状态</span>
            <el-tag v-if="libraries.failed" type="danger" size="small" effect="light" class="runtime-rebuild-failed-tag">失败库 {{ libraries.failed }}</el-tag>
          </div>
          <div class="runtime-card-body">
            <div class="runtime-metrics" v-if="libraries.rebuilding != null || libraries.failed">
              <div class="runtime-metric"><span class="runtime-metric-label">重建中</span><b class="runtime-metric--rebuilding">{{ libraries.rebuilding != null ? libraries.rebuilding : '—' }}</b></div>
              <div class="runtime-metric" v-if="libraries.failed"><span class="runtime-metric-label">失败库</span><b class="runtime-metric--failed">{{ libraries.failed }}</b></div>
            </div>
            <template v-if="rebuilds.length">
              <div class="runtime-rebuild-list">
                <div class="runtime-rebuild-row" v-for="r in rebuilds" :key="r.library_slug">
                  <span class="runtime-rebuild-slug">{{ r.library_slug }}</span>
                  <el-tag :type="rebuildStatusMeta(r.status).type" size="small" effect="light">{{ rebuildStatusMeta(r.status).label }}</el-tag>
                  <div class="runtime-rebuild-progress">
                    <el-progress :percentage="r.progress_pct"
                      :format="function(){return r.done_job_count + '/' + r.expected_job_count + ' (' + r.progress_pct + '%)'}" />
                  </div>
                  <span class="runtime-rebuild-error" v-if="r.last_error">{{ r.last_error }}</span>
                </div>
              </div>
            </template>
            <div v-else-if="!libraries.rebuilding && !libraries.failed" class="runtime-empty-compact">
              当前没有进行中的重建。
            </div>
          </div>
        </div>
      </section>
    </div>
    `,
};
