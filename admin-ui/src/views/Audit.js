import { onMounted, ref, reactive, computed } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

// action code → 中文动作名（仅展示层映射，后端仍存原始 code）
const ACTION_LABELS = {
    'permission.grant': '授权权限',
    'permission.revoke': '撤销权限',
    'library.create': '创建知识库',
    'library.update': '修改知识库',
    'library.delete': '删除知识库',
    'library.faq.create': '新增常用问题',
    'library.faq.update': '修改常用问题',
    'library.faq.delete': '删除常用问题',
    'user.create': '创建用户',
    'user.update': '修改用户',
    'user.disable': '禁用用户',
    'job.retry': '重试任务',
};

// 权限动作 → 中文（permission.grant/revoke 的 actions 字段用）
const PERM_LABELS = {
    read: '读取', insert: '写入', delete: '删除',
    admin: '管理', submit: '提交', review: '审核',
};

function actionLabel(code) {
    return ACTION_LABELS[code] || code;
}

// actions 可能是数组或单个字符串；逐个映射成中文后用「、」连接
function permActions(actions) {
    if (!actions) return '';
    const arr = Array.isArray(actions) ? actions : [actions];
    return arr.map((a) => PERM_LABELS[a] || a).join('、');
}

// 原始 target 整段 JSON（兜底 / 详情区用）
function fmtTarget(t) {
    if (t === null || t === undefined) return '';
    try { return JSON.stringify(t); } catch (_) { return String(t); }
}

// target → 中文摘要；未知 action 回退到原始 JSON
function targetSummary(action, t) {
    if (!t) return '';
    switch (action) {
        case 'permission.grant':
            return `授予 ${t.user_id} 在 ${t.library_slug} 的 ${permActions(t.actions)} 权限`;
        case 'permission.revoke':
            return `撤销 ${t.user_id} 在 ${t.library_slug} 的 ${permActions(t.actions)} 权限`;
        case 'library.create':
            return `创建知识库 ${t.slug}`;
        case 'library.delete':
            return `删除知识库 ${t.slug}`;
        case 'user.create':
            return `创建用户 ${t.email}`;
        default:
            return fmtTarget(t);
    }
}

// ISO 时间 → 本地中文格式 YYYY-MM-DD HH:mm:ss
function fmtTime(at) {
    if (!at) return '';
    const d = new Date(at);
    if (Number.isNaN(d.getTime())) return String(at);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
        + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 详情区 JSON 美化（缩进 2 空格）
function prettyTarget(t) {
    if (t === null || t === undefined) return '';
    try { return JSON.stringify(t, null, 2); } catch (_) { return String(t); }
}

export default {
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        // q：前端按动作搜索关键词（匹配中文名 / 原始 code / 摘要）
        const filter = reactive({ q: '' });

        async function load() {
            loading.value = true;
            try {
                // 一次性拉取，搜索在前端做（后端 action 过滤只认原始 code，
                // 中文搜索需在展示层匹配）；存储格式与接口不变。
                logs.value = await api.listAudit({ limit: 200 });
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        const filtered = computed(() => {
            const q = filter.q.trim().toLowerCase();
            if (!q) return logs.value;
            return logs.value.filter((row) => {
                const label = actionLabel(row.action);
                const summary = targetSummary(row.action, row.target);
                return (row.action || '').toLowerCase().includes(q)
                    || label.toLowerCase().includes(q)
                    || (summary && summary.toLowerCase().includes(q));
            });
        });

        onMounted(load);
        return {
            logs, loading, filter, filtered, load,
            actionLabel, targetSummary, fmtTime, prettyTarget,
        };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>审计日志</h2>
            <div>
                <el-input v-model="filter.q" placeholder="按动作搜索，如：创建知识库 / 授权权限" clearable style="width:280px;margin-right:8px" />
                <el-button @click="load" :loading="loading">刷新</el-button>
            </div>
        </div>
        <el-table :data="filtered" border v-loading="loading">
            <el-table-column type="expand">
                <template #default="{row}">
                    <div style="padding:8px 16px">
                        <div style="margin-bottom:6px;color:#909399;font-size:12px">原始 action：<span class="mono">{{ row.action }}</span></div>
                        <pre class="mono" style="margin:0;white-space:pre-wrap;word-break:break-all">{{ prettyTarget(row.target) }}</pre>
                    </div>
                </template>
            </el-table-column>
            <el-table-column label="时间" width="190">
                <template #default="{row}">{{ fmtTime(row.at) }}</template>
            </el-table-column>
            <el-table-column label="操作者" width="200">
                <template #default="{row}"><span class="mono">{{ row.actor_user_id || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="动作" width="180">
                <template #default="{row}">
                    <el-tooltip :content="row.action" placement="top">
                        <span>{{ actionLabel(row.action) }}</span>
                    </el-tooltip>
                    <div class="mono" style="font-size:12px;color:#909399">{{ row.action }}</div>
                </template>
            </el-table-column>
            <el-table-column label="详情" min-width="320">
                <template #default="{row}">{{ targetSummary(row.action, row.target) }}</template>
            </el-table-column>
        </el-table>
    </div>
    `,
};
