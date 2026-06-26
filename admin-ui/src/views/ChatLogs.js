import { onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const rows = ref([]);
        const libs = ref([]);
        const loading = ref(false);
        const filter = reactive({ library_slug: '', status: '', user_id: '', range: null });

        async function loadLibs() {
            try {
                libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
            } catch (e) { /* ignore */ }
        }

        async function load() {
            loading.value = true;
            try {
                const params = { limit: 200 };
                if (filter.library_slug) params.library_slug = filter.library_slug;
                if (filter.status) params.status = filter.status;
                if (filter.user_id.trim()) params.user_id = filter.user_id.trim();
                if (filter.range && filter.range.length === 2) {
                    params.start = new Date(filter.range[0]).toISOString();
                    params.end = new Date(filter.range[1]).toISOString();
                }
                rows.value = await api.adminListChatLogs(params);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function fmtTime(t) {
            if (!t) return '';
            const d = new Date(t);
            const p = (n) => String(n).padStart(2, '0');
            return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
        }
        function fmtScore(s) { return (Number(s || 0) * 100).toFixed(1) + '%'; }

        onMounted(async () => { await loadLibs(); await load(); });
        return { rows, libs, loading, filter, load, fmtTime, fmtScore };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>问答日志</h2>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                <el-select v-model="filter.library_slug" placeholder="知识库" clearable style="width:160px">
                    <el-option v-for="l in libs" :key="l.slug" :label="l.name" :value="l.slug" />
                </el-select>
                <el-select v-model="filter.status" placeholder="状态" clearable style="width:120px">
                    <el-option value="success" label="成功" />
                    <el-option value="failed" label="失败" />
                </el-select>
                <el-input v-model="filter.user_id" placeholder="用户ID(可选)" clearable style="width:200px" />
                <el-date-picker v-model="filter.range" type="datetimerange" range-separator="至"
                                start-placeholder="开始" end-placeholder="结束" style="width:340px" />
                <el-button type="primary" @click="load" :loading="loading">查询</el-button>
            </div>
        </div>
        <el-table :data="rows" border v-loading="loading">
            <el-table-column type="expand">
                <template #default="{row}">
                    <div style="padding:8px 16px">
                        <div style="margin-bottom:6px"><b>问题：</b>{{ row.question }}</div>
                        <div style="margin-bottom:6px"><b>答案：</b><span style="white-space:pre-wrap">{{ row.answer || '(无)' }}</span></div>
                        <div v-if="row.rewritten_query" style="margin-bottom:6px;color:#909399">检索改写：{{ row.rewritten_query }}</div>
                        <div v-if="row.error_message" style="margin-bottom:6px;color:var(--el-color-danger)">错误：{{ row.error_message }}</div>
                        <div v-if="row.sources && row.sources.length">
                            <b>引用来源（{{ row.sources.length }}）：</b>
                            <div v-for="(s, si) in row.sources" :key="si"
                                 style="padding:6px 10px;margin-top:6px;border:1px solid var(--el-border-color-lighter);border-radius:6px">
                                <span style="color:#909399">[{{ si + 1 }}]</span>
                                <b>{{ s.title || '(无标题)' }}</b>
                                <el-tag size="small" style="margin-left:6px">{{ fmtScore(s.score) }}</el-tag>
                                <div style="font-size:13px;color:var(--el-text-color-regular);white-space:pre-wrap;margin-top:4px">{{ s.content }}</div>
                            </div>
                        </div>
                    </div>
                </template>
            </el-table-column>
            <el-table-column label="时间" width="170">
                <template #default="{row}">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="用户" width="200">
                <template #default="{row}"><span class="mono" style="font-size:12px">{{ row.user_id || '-' }}</span></template>
            </el-table-column>
            <el-table-column prop="library_slug" label="知识库" width="140" />
            <el-table-column label="问题" min-width="240" show-overflow-tooltip>
                <template #default="{row}">{{ row.question }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
                <template #default="{row}">
                    <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                        {{ row.status === 'success' ? '成功' : '失败' }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
                <template #default="{row}">{{ row.latency_ms != null ? row.latency_ms + 'ms' : '-' }}</template>
            </el-table-column>
        </el-table>
    </div>
    `,
};
