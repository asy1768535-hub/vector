import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import * as api from '../api.js';
import { actionLabel, targetSummary, relAuditTime, fmtAuditTime } from '../admin_activity_ui.js';
import { SERVICE_LABELS, STATUS_TAG, STATUS_TEXT, relTime } from '../operations_ui.js';
import { createRequestFence, readProjection } from '../read_state_ui.js';

const CORE_READS = ['operations', 'libraries', 'audits'];
const READ_LABELS = {
    operations: '运行状态',
    libraries: '知识库列表',
    audits: '审计日志',
    health: '健康检查',
};

export default {
    setup() {
        const router = useRouter();
        const health = ref(null);
        const ops = ref(null);
        const libs = ref([]);       // 最多 500 条
        const audits = ref([]);     // 最近 6 条
        const libsTruncated = ref(false);
        const loading = ref(false);
        const healthLoading = ref(false);
        const loadStarted = ref(false);
        const resolvedReads = ref([]);
        const failedReads = ref([]);
        const requestFence = createRequestFence();

        function updateReadResult(key, succeeded) {
            const resolved = new Set(resolvedReads.value);
            const failed = new Set(failedReads.value);
            if (succeeded) {
                resolved.add(key);
                failed.delete(key);
            } else {
                failed.add(key);
            }
            resolvedReads.value = [...resolved];
            failedReads.value = [...failed];
        }

        async function load(forceRefresh = false, requestedReads = null) {
            const targets = requestedReads?.length
                ? [...new Set(requestedReads)]
                : [...CORE_READS, 'health'];
            const coreTargets = CORE_READS.filter((key) => targets.includes(key));
            const requestToken = requestFence.begin();
            loadStarted.value = true;
            failedReads.value = failedReads.value.filter((key) => !targets.includes(key));

            if (coreTargets.length) {
                loading.value = true;
                const requests = {
                    operations: () => api.operationsStatus(forceRefresh),
                    libraries: () => api.listLibraries({ limit: 500 }, forceRefresh),
                    audits: () => api.listAudit({ limit: 6 }, forceRefresh),
                };
                const results = await Promise.allSettled(coreTargets.map((key) => requests[key]()));
                if (!requestFence.isCurrent(requestToken)) return;

                coreTargets.forEach((key, index) => {
                    const result = results[index];
                    updateReadResult(key, result.status === 'fulfilled');
                    if (result.status !== 'fulfilled') return;
                    if (key === 'operations') ops.value = result.value;
                    if (key === 'libraries') {
                        const list = (result.value || []).filter((lib) => !lib.deleted_at);
                        libsTruncated.value = list.length >= 500;
                        libs.value = list;
                    }
                    if (key === 'audits') audits.value = result.value || [];
                });
                loading.value = false;
            }

            if (!targets.includes('health')) return;
            healthLoading.value = true;
            try {
                const result = await api.health(forceRefresh);
                if (!requestFence.isCurrent(requestToken)) return;
                health.value = result;
                updateReadResult('health', true);
            } catch (_) {
                if (!requestFence.isCurrent(requestToken)) return;
                updateReadResult('health', false);
            } finally {
                if (requestFence.isCurrent(requestToken)) healthLoading.value = false;
            }
        }

        function retryFailedReads() {
            if (loading.value || healthLoading.value || !failedReads.value.length) return;
            return load(true, failedReads.value);
        }

        function refreshDashboard() {
            if (loading.value || healthLoading.value) return;
            return load(true);
        }

        const libCount = computed(() =>
            !resolvedReads.value.includes('libraries')
                ? '—'
                : libsTruncated.value ? '500+' : String(libs.value.length)
        );

        const jobStats = computed(() => {
            const jobs = ops.value?.embedding_jobs || {};
            return {
                pending: jobs.pending || 0,
                processing: jobs.processing || 0,
                failed: jobs.failed || 0,
            };
        });

        const onlineServices = computed(() => {
            if (!resolvedReads.value.includes('operations')) return '—';
            if (!ops.value?.services) return 0;
            return ops.value.services.filter((s) => s.status === 'online').length;
        });

        const hasPrimaryData = computed(() =>
            CORE_READS.some((key) => resolvedReads.value.includes(key))
        );
        const allPrimaryFailed = computed(() =>
            CORE_READS.every((key) => failedReads.value.includes(key))
        );
        const fatalError = computed(() =>
            !loading.value && !hasPrimaryData.value && allPrimaryFailed.value
                ? '运行状态、知识库列表和审计日志均加载失败'
                : ''
        );
        const dashboardReadState = computed(() => readProjection({
            started: loadStarted.value,
            loading: loading.value,
            hasResolved: hasPrimaryData.value,
            error: fatalError.value,
        }));
        const errors = computed(() =>
            failedReads.value.map((key) => READ_LABELS[key])
        );
        const operationsResolved = computed(() => resolvedReads.value.includes('operations'));
        const librariesResolved = computed(() => resolvedReads.value.includes('libraries'));
        const auditsResolved = computed(() => resolvedReads.value.includes('audits'));
        const servicesResolved = computed(() =>
            operationsResolved.value || resolvedReads.value.includes('health')
        );

        const recentLibs = computed(() =>
            libs.value
                .slice()
                .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
                .slice(0, 5)
        );

        const allServices = computed(() => {
            const svc = ops.value?.services || [];
            // Merge with health data for non-worker services
            const list = [];
            // 1) Worker services from operationsStatus
            for (const s of svc) {
                list.push({
                    name: SERVICE_LABELS[s.service_type] || s.service_type,
                    status: s.status,
                    statusText: STATUS_TEXT[s.status] || s.status,
                    tag: STATUS_TAG[s.status] || 'info',
                    instances: `${s.online_instances || 0} / ${s.known_instances || 0}`,
                    heartbeat: s.latest ? relTime(s.latest.seconds_since_last_seen) : '—',
                });
            }
            // 2) Infra services from health
            const h = health.value;
            if (h) {
                const ok = 'online', fail = 'offline';
                list.push({ name: 'PostgreSQL', status: h.db ? ok : fail, statusText: h.db ? '在线' : '离线', tag: h.db ? 'success' : 'danger', instances: '—', heartbeat: '—' });
                list.push({ name: 'Qdrant', status: h.qdrant ? ok : fail, statusText: h.qdrant ? '在线' : '离线', tag: h.qdrant ? 'success' : 'danger', instances: '—', heartbeat: '—' });
                const embOk = h.embedding === 'ok';
                list.push({ name: 'Embedding', status: embOk ? ok : fail, statusText: embOk ? '在线' : '离线', tag: embOk ? 'success' : 'danger', instances: '—', heartbeat: '—' });
            }
            return list;
        });

        onMounted(() => load(false));

        return {
            health, ops, libs, audits, loading, healthLoading, errors, libCount,
            jobStats, onlineServices, recentLibs, allServices,
            dashboardReadState, fatalError, operationsResolved, librariesResolved, auditsResolved, servicesResolved,
            refreshDashboard, retryFailedReads,
            actionLabel, targetSummary, relAuditTime, fmtAuditTime,
            router,
        };
    },
    template: `
    <div class="dashboard-workspace">
        <!-- Header -->
        <div class="dashboard-header">
            <h2 class="dashboard-title">概览</h2>
            <el-button :loading="loading || healthLoading" @click="refreshDashboard">刷新</el-button>
        </div>

        <section v-if="dashboardReadState === 'idle' || dashboardReadState === 'loading'"
                 class="dashboard-read-state" v-loading="true">
            <span>正在加载运营数据</span>
        </section>

        <section v-else-if="dashboardReadState === 'fatal'" class="dashboard-read-state dashboard-read-state--error" role="alert">
            <div>
                <strong>运营总览加载失败</strong>
                <p>{{ fatalError }}</p>
            </div>
            <el-button type="primary" :loading="loading || healthLoading" @click="retryFailedReads">重试</el-button>
        </section>

        <template v-else>
        <!-- Stats row -->
        <div class="dashboard-stats">
            <div class="dashboard-stat-card">
                <local-icon icon="overview:kb-count" class="dashboard-stat-icon"></local-icon>
                <div class="dashboard-stat-num">{{ libCount }}</div>
                <div class="dashboard-stat-label">知识库</div>
            </div>
            <div class="dashboard-stat-card dashboard-stat--pending">
                <local-icon icon="overview:pending-jobs" class="dashboard-stat-icon"></local-icon>
                <div class="dashboard-stat-num">{{ operationsResolved ? jobStats.pending : '—' }}</div>
                <div class="dashboard-stat-label">待处理任务</div>
            </div>
            <div class="dashboard-stat-card dashboard-stat--processing">
                <local-icon icon="overview:processing-jobs" class="dashboard-stat-icon"></local-icon>
                <div class="dashboard-stat-num">{{ operationsResolved ? jobStats.processing : '—' }}</div>
                <div class="dashboard-stat-label">处理中</div>
            </div>
            <div class="dashboard-stat-card dashboard-stat--failed">
                <local-icon icon="overview:failed-jobs" class="dashboard-stat-icon"></local-icon>
                <div class="dashboard-stat-num">{{ operationsResolved ? jobStats.failed : '—' }}</div>
                <div class="dashboard-stat-label">失败任务</div>
            </div>
            <div class="dashboard-stat-card dashboard-stat--online">
                <local-icon icon="overview:online-services" class="dashboard-stat-icon"></local-icon>
                <div class="dashboard-stat-num">{{ onlineServices }}</div>
                <div class="dashboard-stat-label">在线服务</div>
            </div>
        </div>

        <!-- Errors -->
        <el-alert v-if="errors.length" type="warning" :closable="false" show-icon
                  :title="'部分数据加载失败：' + errors.join('、')">
            <template #default>
                <el-button link type="primary" :disabled="loading || healthLoading" @click="retryFailedReads">重试失败项</el-button>
            </template>
        </el-alert>

        <!-- Four-quadrant grid -->
        <div class="dashboard-grid">
            <!-- Services -->
            <section class="dashboard-card">
                <div class="dashboard-card-title"><local-icon icon="overview:service-status" class="dashboard-card-title-icon"></local-icon>服务状态</div>
                <div class="dashboard-table-shell">
                    <div v-if="!servicesResolved" class="dashboard-empty">服务状态加载失败</div>
                    <el-table v-else :data="allServices" size="small">
                        <el-table-column label="服务" prop="name" min-width="120" />
                        <el-table-column label="状态" width="70" align="center">
                            <template #default="{row}">
                                <el-tag :type="row.tag" size="small">{{ row.statusText }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="实例" width="70" align="center" prop="instances" />
                        <el-table-column label="心跳" width="90" align="center" prop="heartbeat" />
                    </el-table>
                </div>
            </section>

            <!-- Activity -->
            <section class="dashboard-card">
                <div class="dashboard-card-title"><local-icon icon="overview:recent-activity" class="dashboard-card-title-icon"></local-icon>最近活动</div>
                <div v-if="!auditsResolved" class="dashboard-empty">活动记录加载失败</div>
                <div v-else-if="!audits.length" class="dashboard-empty">暂无活动记录</div>
                <div v-for="a in audits" :key="a.id" class="dashboard-activity-item">
                    <div class="dashboard-activity-action">{{ actionLabel(a.action) }}</div>
                    <div class="dashboard-activity-summary">{{ targetSummary(a.action, a.target) }}</div>
                    <div class="dashboard-activity-time">{{ relAuditTime(a.at) }}</div>
                </div>
                <el-button text class="dashboard-card-link" @click="router.push('/audit-center/operations')">查看更多 →</el-button>
            </section>

            <!-- Jobs -->
            <section class="dashboard-card">
                <div class="dashboard-card-title"><local-icon icon="overview:rebuild" class="dashboard-card-title-icon"></local-icon>任务处理概况</div>
                <div v-if="!operationsResolved" class="dashboard-empty">任务状态加载失败</div>
                <div v-else class="dashboard-jobs-row">
                    <div class="dashboard-job-item dashboard-job--pending">
                        <div class="dashboard-job-num">{{ jobStats.pending }}</div>
                        <div class="dashboard-job-label">待处理</div>
                    </div>
                    <div class="dashboard-job-item dashboard-job--processing">
                        <div class="dashboard-job-num">{{ jobStats.processing }}</div>
                        <div class="dashboard-job-label">处理中</div>
                    </div>
                    <div class="dashboard-job-item dashboard-job--failed">
                        <div class="dashboard-job-num">{{ jobStats.failed }}</div>
                        <div class="dashboard-job-label">失败</div>
                    </div>
                </div>
                <el-button text class="dashboard-card-link" @click="router.push('/operations-center/jobs')">查看任务队列 →</el-button>
            </section>

            <!-- Libraries -->
            <section class="dashboard-card">
                <div class="dashboard-card-title"><local-icon icon="overview:kb-count" class="dashboard-card-title-icon"></local-icon>最近创建的知识库</div>
                <div v-if="!librariesResolved" class="dashboard-empty">知识库列表加载失败</div>
                <div v-else-if="!recentLibs.length" class="dashboard-empty">暂无知识库</div>
                <div v-for="l in recentLibs" :key="l.id" class="dashboard-lib-item">
                    <div class="dashboard-lib-name">{{ l.name }}</div>
                    <div class="dashboard-lib-meta">
                        <el-tag size="small" :type="l.index_state === 'ready' ? 'success' : l.index_state === 'failed' ? 'danger' : 'warning'">{{ l.index_state }}</el-tag>
                        <span>{{ l.embedding_model }}</span>
                        <span>{{ fmtAuditTime(l.created_at).slice(0, 10) }}</span>
                    </div>
                </div>
                <el-button text class="dashboard-card-link" @click="router.push('/libraries')">查看全部 →</el-button>
            </section>
        </div>
        </template>
    </div>
    `,
};
