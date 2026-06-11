import { onMounted, ref, reactive } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const jobs = ref([]);
        const loading = ref(false);
        const filter = reactive({ status: '', library_id: '' });

        async function load() {
            loading.value = true;
            try {
                const params = { limit: 200 };
                if (filter.status) params.status = filter.status;
                if (filter.library_id) params.library_id = filter.library_id;
                jobs.value = await api.listJobs(params);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        async function retry(row) {
            try {
                await api.retryJob(row.id);
                ElMessage.success('已重置为 pending');
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        onMounted(load);
        return { jobs, loading, filter, load, retry };
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
            </div>
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
                    <el-button size="small" :disabled="!['failed','processing'].includes(row.status)" @click="retry(row)">重试</el-button>
                </template>
            </el-table-column>
        </el-table>
    </div>
    `,
};
