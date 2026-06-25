import { onMounted, ref, computed } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

// 进程类型 → 中文显示名（固定三类，按设计顺序）。
const SERVICE_LABELS = {
    api: 'API',
    embedding_worker: 'Embedding Worker',
    cleanup_worker: 'Cleanup Worker',
};
const STATUS_TAG = { online: 'success', degraded: 'warning', offline: 'info' };
const STATUS_TEXT = { online: '在线', degraded: '降级', offline: '离线' };

function relTime(seconds) {
    if (seconds == null) return '—';
    if (seconds < 60) return `${seconds} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分前`;
    return `${Math.floor(seconds / 3600)} 小时前`;
}

export default {
    setup() {
        const loading = ref(false);
        const data = ref(null);

        async function load() {
            loading.value = true;
            try {
                data.value = await api.operationsStatus();
            } catch (e) {
                ElMessage.error(e.message);
            } finally {
                loading.value = false;
            }
        }

        // 三类进程恒定渲染，附中文名与展示字段。
        const services = computed(() => {
            const list = (data.value && data.value.services) || [];
            return list.map((s) => {
                const latest = s.latest;
                const degradedFlag = !!(latest && latest.heartbeat_metadata && latest.heartbeat_metadata.degraded);
                return {
                    ...s,
                    label: SERVICE_LABELS[s.service_type] || s.service_type,
                    tagType: STATUS_TAG[s.status] || 'info',
                    statusText: STATUS_TEXT[s.status] || s.status,
                    counts: `${s.online_instances}/${s.known_instances}`,
                    lastBeat: latest
                        ? relTime(latest.seconds_since_last_seen) + (degradedFlag ? '（降级）' : '')
                        : '—',
                    hostPid: latest ? `${latest.hostname} / ${latest.pid}` : '—',
                };
            });
        });

        const jobs = computed(() => (data.value && data.value.embedding_jobs) || {});
        const outbox = computed(() => (data.value && data.value.cleanup_outbox) || {});
        const libraries = computed(() => (data.value && data.value.libraries) || {});
        const rebuilds = computed(() => (data.value && data.value.rebuild_operations) || []);

        onMounted(load);
        return { loading, data, services, jobs, outbox, libraries, rebuilds, load };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>运行状态</h2>
            <el-button @click="load" :loading="loading">刷新</el-button>
        </div>

        <el-card shadow="never" style="margin-bottom:16px">
            <template #header>服务状态</template>
            <el-table :data="services" border v-loading="loading">
                <el-table-column label="服务" min-width="180">
                    <template #default="{row}"><strong>{{ row.label }}</strong></template>
                </el-table-column>
                <el-table-column label="状态" width="120">
                    <template #default="{row}">
                        <el-tag :type="row.tagType" size="small" effect="light">{{ row.statusText }}</el-tag>
                    </template>
                </el-table-column>
                <el-table-column prop="counts" label="在线/已知" width="120" />
                <el-table-column prop="lastBeat" label="最后心跳" min-width="160" />
                <el-table-column prop="hostPid" label="主机 / PID" min-width="200" show-overflow-tooltip />
            </el-table>
        </el-card>

        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px">
            <el-card shadow="never" style="flex:1;min-width:300px">
                <template #header>Embedding 任务</template>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                    <el-tag type="info" size="large">总计 {{ jobs.total || 0 }}</el-tag>
                    <el-tag type="warning" size="large">pending {{ jobs.pending || 0 }}</el-tag>
                    <el-tag size="large">processing {{ jobs.processing || 0 }}</el-tag>
                    <el-tag type="success" size="large">done {{ jobs.done || 0 }}</el-tag>
                    <el-tag type="danger" size="large" :effect="jobs.failed ? 'dark' : 'light'">failed {{ jobs.failed || 0 }}</el-tag>
                </div>
            </el-card>
            <el-card shadow="never" style="flex:1;min-width:300px">
                <template #header>Cleanup Outbox</template>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                    <el-tag type="info" size="large">总计 {{ outbox.total || 0 }}</el-tag>
                    <el-tag type="warning" size="large">pending {{ outbox.pending || 0 }}</el-tag>
                    <el-tag size="large">processing {{ outbox.processing || 0 }}</el-tag>
                    <el-tag type="success" size="large">done {{ outbox.done || 0 }}</el-tag>
                    <el-tag type="danger" size="large" :effect="outbox.dead_letter ? 'dark' : 'light'">失败/死信 {{ outbox.dead_letter || 0 }}</el-tag>
                </div>
            </el-card>
        </div>

        <el-card shadow="never">
            <template #header>
                重建
                <el-tag v-if="libraries.failed" type="danger" size="small" effect="light" style="margin-left:8px">失败库 {{ libraries.failed }}</el-tag>
            </template>
            <div v-if="!rebuilds.length" style="color:var(--el-text-color-secondary)">当前没有进行中的重建。</div>
            <el-table v-else :data="rebuilds" border>
                <el-table-column prop="library_slug" label="库" min-width="160" />
                <el-table-column prop="status" label="状态" width="120">
                    <template #default="{row}"><el-tag size="small" effect="light">{{ row.status }}</el-tag></template>
                </el-table-column>
                <el-table-column label="进度" min-width="220">
                    <template #default="{row}">
                        <el-progress :percentage="row.progress_pct"
                            :format="() => row.done_job_count + '/' + row.expected_job_count + ' (' + row.progress_pct + '%)'" />
                    </template>
                </el-table-column>
                <el-table-column prop="last_error" label="最后错误" min-width="200" show-overflow-tooltip />
            </el-table>
        </el-card>
    </div>
    `,
};
