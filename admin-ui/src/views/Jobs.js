import { onMounted, ref, reactive } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const jobs = ref([]);
        const loading = ref(false);
        const resetting = ref(false);
        const stats = ref({ pending: 0, processing: 0, done: 0, failed: 0, total: 0 });
        const filter = reactive({ status: '', library_id: '' });

        async function loadStats() {
            try { stats.value = await api.jobStats(); }
            catch (e) { /* 统计失败不打断主列表 */ }
        }

        async function load() {
            loading.value = true;
            try {
                const params = { limit: 200 };
                if (filter.status) params.status = filter.status;
                if (filter.library_id) params.library_id = filter.library_id;
                jobs.value = await api.listJobs(params);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
            loadStats();
        }

        async function retry(row) {
            try {
                await api.retryJob(row.id);
                ElMessage.success('已重置为 pending');
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function resetFailed() {
            if (!stats.value.failed) { ElMessage.info('当前没有失败任务'); return; }
            try {
                await ElMessageBox.confirm(
                    `将把全部 ${stats.value.failed} 条「失败」任务重置为 pending（尝试次数归零），worker 会自动重跑。确定？`,
                    '重置所有失败任务', { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' }
                );
            } catch (_) { return; }  // 用户取消
            resetting.value = true;
            try {
                const r = await api.resetFailedJobs();
                ElMessage.success(`已重置 ${r.reset_count} 条失败任务`);
                load();
            } catch (e) { ElMessage.error(e.message); }
            finally { resetting.value = false; }
        }

        onMounted(load);
        return { jobs, loading, resetting, stats, filter, load, retry, resetFailed };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>任务监控 (embedding_jobs)</h2>
            <div>
                <el-select v-model="filter.status" placeholder="全部状态" clearable style="width:140px;margin-right:8px">
                    <el-option label="等待中 (pending)" value="pending" />
                    <el-option label="处理中 (processing)" value="processing" />
                    <el-option label="已完成 (done)" value="done" />
                    <el-option label="已失败 (failed)" value="failed" />
                </el-select>
                <el-button @click="load" :loading="loading">查询</el-button>
                <el-button type="danger" plain :disabled="!stats.failed" :loading="resetting" @click="resetFailed">
                    重置所有失败任务<span v-if="stats.failed"> ({{ stats.failed }})</span>
                </el-button>
            </div>
        </div>

        <div style="margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap">
            <el-tag type="info" size="large">总计 {{ stats.total }}</el-tag>
            <el-tag type="warning" size="large">等待 pending {{ stats.pending }}</el-tag>
            <el-tag size="large">处理 processing {{ stats.processing }}</el-tag>
            <el-tag type="success" size="large">完成 done {{ stats.done }}</el-tag>
            <el-tag type="danger" size="large" :effect="stats.failed ? 'dark' : 'light'">失败 failed {{ stats.failed }}</el-tag>
        </div>

        <el-table :data="jobs" border v-loading="loading">
            <el-table-column label="任务 ID (Job ID)" width="120">
                <template #default="{row}"><span class="mono">{{ row.id.slice(0, 8) }}…</span></template>
            </el-table-column>
            <el-table-column label="文档 ID (Document ID)" width="120">
                <template #default="{row}"><span class="mono">{{ row.document_id.slice(0, 8) }}…</span></template>
            </el-table-column>
            <el-table-column label="状态" width="100">
                <template #default="{row}">
                    <el-tag :type="row.status === 'done' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'" size="small">
                        {{ row.status }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column prop="worker_id" label="工作器 (Worker)" width="180" />
            <el-table-column prop="attempt_count" label="尝试" width="80" />
            <el-table-column prop="last_error" label="最后错误" min-width="240" show-overflow-tooltip />
            <el-table-column prop="created_at" label="创建" width="180" />
            <el-table-column prop="finished_at" label="完成" width="180" />
            <el-table-column label="操作" width="100" fixed="right">
                <template #default="{row}">
                    <el-button size="small" :disabled="!['failed','processing','pending'].includes(row.status)" @click="retry(row)">重试</el-button>
                </template>
            </el-table-column>
        </el-table>
    </div>
    `,
};
