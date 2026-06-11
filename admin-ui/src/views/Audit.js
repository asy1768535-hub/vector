import { onMounted, ref, reactive } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        const filter = reactive({ action: '' });

        async function load() {
            loading.value = true;
            try {
                const params = { limit: 200 };
                if (filter.action) params.action = filter.action;
                logs.value = await api.listAudit(params);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function fmtTarget(t) {
            if (!t) return '';
            try { return JSON.stringify(t); } catch (_) { return String(t); }
        }

        onMounted(load);
        return { logs, loading, filter, load, fmtTarget };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>审计日志</h2>
            <div>
                <el-input v-model="filter.action" placeholder="动作（如 library.create）" clearable style="width:240px;margin-right:8px" />
                <el-button @click="load" :loading="loading">查询</el-button>
            </div>
        </div>
        <el-table :data="logs" border v-loading="loading">
            <el-table-column prop="at" label="时间" width="200" />
            <el-table-column label="操作者" width="200">
                <template #default="{row}"><span class="mono">{{ row.actor_user_id || '-' }}</span></template>
            </el-table-column>
            <el-table-column prop="action" label="动作" width="220" />
            <el-table-column label="目标对象 (target)" min-width="320">
                <template #default="{row}"><span class="mono">{{ fmtTarget(row.target) }}</span></template>
            </el-table-column>
        </el-table>
    </div>
    `,
};
