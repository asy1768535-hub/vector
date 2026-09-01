import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';
import { actionLabel, actorDisplay, targetSummary, targetTypeMeta, targetDisplay, fmtAuditTime, prettyTarget, ACTION_LABELS, actionTone } from '../admin_activity_ui.js';
import { dataEmpty } from '../illustrations.js';
import { paginate, filterAuditLogs, downloadCSV } from '../logs_ui.js';

const TYPE_OPTIONS = [
    { value: 'library', label: '知识库' },
    { value: 'user', label: '用户' },
    { value: 'permission', label: '权限' },
    { value: 'job', label: '任务' },
    { value: 'document', label: '文档' },
    { value: 'api_key', label: 'API Key' },
    { value: 'other', label: '其他' },
];

export default {
    setup() {
        const logs = ref([]);
        const users = ref([]);
        const loading = ref(false);
        const loadError = ref(false);
        const filters = reactive({ range: null, action: '', targetType: '', actor: '', keyword: '' });
        const page = ref(1);
        const pageSize = ref(10);
        const expandRowKeys = ref([]);

        async function load(forceRefresh = false) {
            loading.value = true;
            try {
                const [auditResult, usersResult] = await Promise.allSettled([
                    api.listAudit({ limit: 500 }, forceRefresh),
                    api.listUsers({ include_deleted: 'true', limit: 500 }, forceRefresh),
                ]);
                if (auditResult.status === 'rejected') throw auditResult.reason;
                logs.value = auditResult.value || [];
                if (usersResult.status === 'fulfilled') users.value = usersResult.value || [];
                const ids = new Set(logs.value.map((r) => r.id));
                expandRowKeys.value = expandRowKeys.value.filter((k) => ids.has(k));
                loadError.value = false;
            } catch (e) {
                if (!logs.value.length) loadError.value = true;
                ElMessage.error(e.message);
            } finally { loading.value = false; }
        }

        const filtered = computed(() => filterAuditLogs(logs.value, filters));
        const paged = computed(() => paginate(filtered.value, page.value, pageSize.value));
        const totalCount = computed(() => filtered.value.length);

        const actionOptions = computed(() => {
            const seen = new Set();
            const opts = [];
            for (const code of Object.keys(ACTION_LABELS)) {
                if (!seen.has(code)) { seen.add(code); opts.push({ value: code, label: actionLabel(code) }); }
            }
            for (const r of logs.value) {
                if (r.action && !seen.has(r.action)) { seen.add(r.action); opts.push({ value: r.action, label: actionLabel(r.action) }); }
            }
            return opts.sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'));
        });

        function resetFilters() {
            Object.assign(filters, { range: null, action: '', targetType: '', actor: '', keyword: '' });
            page.value = 1;
        }
        function toggleExpand(row) {
            const idx = expandRowKeys.value.indexOf(row.id);
            if (idx >= 0) expandRowKeys.value.splice(idx, 1); else expandRowKeys.value.push(row.id);
        }
        function handleExpandChange(_row, expandedRows) {
            expandRowKeys.value = (expandedRows || []).map((item) => item.id);
        }

        function exportCSV() {
            const headers = ['时间', '操作者', '动作', '目标类型', '目标名称', '原始target'];
            const data = filtered.value.map((r) => [
                fmtAuditTime(r.at), r.actor_user_id || '', r.action,
                targetTypeMeta(r.action).type, targetDisplay(r.action, r.target),
                prettyTarget(r.target),
            ]);
            downloadCSV('audit-logs.csv', headers, data);
        }

        onMounted(() => load(false));
        return {
            logs, users, loading, loadError, filters, page, pageSize, expandRowKeys,
            filtered, paged, totalCount, actionOptions, TYPE_OPTIONS,
            load, resetFilters, toggleExpand, handleExpandChange, exportCSV,
            actionLabel, actorDisplay, targetSummary, targetTypeMeta, targetDisplay, fmtAuditTime, prettyTarget, actionTone, dataEmpty, store,
        };
    },
    template: `
    <div class="audit-workspace">
      <header class="audit-header">
        <div>
          <h2 class="audit-title"><local-icon icon="sidebar:audit" class="audit-title-icon" />审计日志</h2>
          <p class="audit-desc">记录系统中的关键操作，便于追踪与审计</p>
        </div>
        <div class="audit-header-actions">
          <el-button class="app-refresh-button" @click="load(true)" :loading="loading">
            <span class="app-refresh-icon" aria-hidden="true"></span>刷新
          </el-button>
          <el-button @click="exportCSV" :disabled="!filtered.length">导出 CSV</el-button>
        </div>
      </header>

      <section class="logs-toolbar">
        <div class="logs-toolbar-row">
          <div class="logs-filter-field"><span class="logs-toolbar-label">时间范围</span><el-date-picker v-model="filters.range" class="logs-filter-range" type="datetimerange" range-separator="至" start-placeholder="起始" end-placeholder="截止" value-format="YYYY-MM-DDTHH:mm:ss" @change="page=1" /></div>
          <div class="logs-filter-field"><span class="logs-toolbar-label">动作</span><el-select v-model="filters.action" class="logs-filter-select" placeholder="全部动作" clearable @change="page=1"><el-option v-for="a in actionOptions" :key="a.value" :label="a.label" :value="a.value" /></el-select></div>
          <div class="logs-filter-field"><span class="logs-toolbar-label">目标类型</span><el-select v-model="filters.targetType" class="logs-filter-select" placeholder="全部类型" clearable @change="page=1"><el-option v-for="t in TYPE_OPTIONS" :key="t.value" :label="t.label" :value="t.value" /></el-select></div>
        </div>
        <div class="logs-toolbar-row">
          <div class="logs-filter-field"><span class="logs-toolbar-label">操作者</span><el-input v-model="filters.actor" class="logs-filter-input" placeholder="用户名、邮箱或 ID" clearable @input="page=1" /></div>
          <div class="logs-filter-field"><span class="logs-toolbar-label">目标名称/ID</span><el-input v-model="filters.keyword" class="logs-filter-input" placeholder="搜索动作/操作者/目标" clearable @input="page=1" /></div>
          <el-button @click="resetFilters">重置筛选</el-button>
        </div>
      </section>

      <section class="logs-table-card" v-if="!loadError || logs.length">
        <div class="logs-table-shell">
          <el-table :data="paged.items" border v-loading="loading" row-key="id" :expand-row-keys="expandRowKeys" @expand-change="handleExpandChange">
            <template #empty><div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>暂无审计记录</p></div></template>
            <el-table-column type="expand">
              <template #default="{row}">
                <div class="logs-detail-grid">
                  <div class="logs-detail-cell logs-detail-cell--full"><div class="logs-detail-label">技术详情</div><div class="logs-detail-value">以下内容保留原始动作码和完整标识，便于排查。</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">日志 ID</div><div class="logs-detail-value audit-detail-mono">{{ row.id || '—' }}</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">时间</div><div class="logs-detail-value">{{ fmtAuditTime(row.at) }}</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">操作者</div><div class="logs-detail-value audit-detail-mono">{{ row.actor_user_id || '—' }}</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">动作</div><div class="logs-detail-value">{{ actionLabel(row.action) }} <span class="audit-detail-code">{{ row.action }}</span></div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">目标类型</div><div class="logs-detail-value">{{ targetTypeMeta(row.action).type }}</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">目标名称</div><div class="logs-detail-value">{{ targetDisplay(row.action, row.target) || '—' }}</div></div>
                  <div class="logs-detail-cell"><div class="logs-detail-label">摘要</div><div class="logs-detail-value">{{ targetSummary(row.action, row.target) || '—' }}</div></div>
                  <div class="logs-detail-cell logs-detail-cell--full"><div class="logs-detail-label">原始数据</div><pre class="logs-detail-pre audit-detail-json">{{ prettyTarget(row.target) }}</pre></div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="170"><template #default="{row}"><span class="audit-time-cell">{{ fmtAuditTime(row.at) }}</span></template></el-table-column>
            <el-table-column label="操作者" width="200"><template #default="{row}"><span class="audit-actor-cell">{{ actorDisplay(row, users, store.user) }}</span></template></el-table-column>
            <el-table-column label="动作" width="140"><template #default="{row}"><span :class="'action-tone ' + actionTone(row.action)">{{ actionLabel(row.action) }}</span></template></el-table-column>
            <el-table-column label="目标" min-width="260">
              <template #default="{row}">
                <div class="audit-target-cell">
                  <local-icon :icon="targetTypeMeta(row.action).icon" class="audit-target-icon" />
                  <div class="audit-target-body">
                    <div class="audit-target-name">{{ targetDisplay(row.action, row.target) || '—' }}</div>
                    <div class="audit-target-sub">{{ targetSummary(row.action, row.target) || targetTypeMeta(row.action).type }}</div>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center">
              <template #default="{row}"><el-button size="small" link type="primary" @click.stop="toggleExpand(row)">{{ expandRowKeys.includes(row.id) ? '收起详情' : '查看详情' }}</el-button></template>
            </el-table-column>
          </el-table>
        </div>
        <div class="logs-pagination">
          <span>当前加载 {{ totalCount }} 条<span v-if="logs.length >= 500">（已截断至 500 条）</span></span>
          <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="totalCount" :page-sizes="[10, 20, 50]" layout="sizes, prev, pager, next" />
        </div>
      </section>

      <section class="logs-error-state" v-if="loadError && !logs.length">
        <div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>审计日志加载失败</p><el-button type="primary" size="small" @click="load(false)" :loading="loading">重新加载</el-button></div>
      </section>
    </div>
    `,
};
