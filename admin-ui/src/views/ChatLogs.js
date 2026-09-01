import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { dataEmpty } from '../illustrations.js';
import { documentTypeIcon } from '../documents_ui.js';
import { paginate, filterChatLogs, downloadCSV } from '../logs_ui.js';

function fmtTime(t) {
    if (!t) return '—';
    const d = new Date(t);
    if (Number.isNaN(d.getTime())) return '—';
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtLatency(ms) { return ms != null ? ms + 'ms' : '—'; }
function chatStatusLabel(s) { if (s === 'success') return '成功'; if (s === 'failed') return '失败'; return '未知'; }
function chatStatusTag(s) { if (s === 'success') return 'success'; if (s === 'failed') return 'danger'; return 'info'; }
function clampDisplayScore(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : null;
}
function fmtSourceScore(source) {
    const score = clampDisplayScore(source?.display_score);
    if ((source?.score_type === 'rerank' || source?.score_type === 'vector') && score !== null) {
        const label = source.score_type === 'rerank' ? '相关度' : '向量相似度';
        return `${label} ${(score * 100).toFixed(1)}%`;
    }
    if (source?.score_type === 'rrf') return '融合排序';
    if (source?.score_type === 'legacy') return '历史排序';
    return '未提供相关度';
}
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export default {
    setup() {
        const rows = ref([]);
        const libs = ref([]);
        const libsFailed = ref(false);
        const loading = ref(false);
        const loadError = ref(false);
        const filters = reactive({ library_slug: '', status: '', user_id: '', range: null, keyword: '', rewrite: '', hasSources: '' });
        const page = ref(1);
        const pageSize = ref(10);
        const expandRowKeys = ref([]);
        const sourceDetail = reactive({ open: false, item: null });

        const libNameMap = computed(() => {
            const m = {};
            for (const l of libs.value) m[l.slug] = l.name;
            return m;
        });

        function libDisplay(slug) {
            const name = libNameMap.value[slug];
            return name ? name : (slug || '—');
        }

        async function loadLibs() {
            libsFailed.value = false;
            try {
                const l = await api.listLibraries({ limit: 500 }, false);
                libs.value = (l || []).filter((x) => !x.deleted_at);
            } catch (_) { libsFailed.value = true; }
        }

        async function load(forceRefresh = false) {
            loading.value = true;
            try {
                const params = { limit: 500 };
                if (filters.library_slug) params.library_slug = filters.library_slug;
                if (filters.status) params.status = filters.status;
                if (filters.user_id) {
                    if (!UUID_RE.test(filters.user_id.trim())) { ElMessage.warning('用户 UUID 格式不正确'); loading.value = false; return; }
                    params.user_id = filters.user_id.trim();
                }
                if (filters.range && filters.range.length === 2 && filters.range[0] && filters.range[1]) {
                    params.start = new Date(filters.range[0]).toISOString();
                    params.end = new Date(filters.range[1]).toISOString();
                }
                const d = await api.adminListChatLogs(params, forceRefresh);
                rows.value = d || [];
                const ids = new Set(rows.value.map((r) => r.message_id));
                expandRowKeys.value = expandRowKeys.value.filter((k) => ids.has(k));
                loadError.value = false;
            } catch (e) {
                if (!rows.value.length) loadError.value = true;
                ElMessage.error(e.message);
            } finally { loading.value = false; }
        }

        onMounted(async () => { await Promise.allSettled([loadLibs(), load(false)]); });

        const filtered = computed(() => filterChatLogs(rows.value, filters));
        const paged = computed(() => paginate(filtered.value, page.value, pageSize.value));
        const totalCount = computed(() => filtered.value.length);

        async function resetFilters() {
            Object.assign(filters, { library_slug: '', status: '', user_id: '', range: null, keyword: '', rewrite: '', hasSources: '' });
            page.value = 1;
            await load(true);
        }
        function toggleExpand(row) {
            const idx = expandRowKeys.value.indexOf(row.message_id);
            if (idx >= 0) expandRowKeys.value.splice(idx, 1); else expandRowKeys.value.push(row.message_id);
        }
        function handleExpandChange(_row, expandedRows) {
            expandRowKeys.value = (expandedRows || []).map((item) => item.message_id);
        }

        function sourceMeta(source) {
            if (!source) return [];
            const meta = [];
            if (source.document_id) meta.push({ label: '文档', value: source.document_id, mono: true });
            const pageValue = source.page ?? source.page_number;
            if (pageValue !== undefined && pageValue !== null && pageValue !== '') meta.push({ label: '页码', value: `第 ${pageValue} 页` });
            const location = source.loc || source.location;
            if (location) meta.push({ label: '位置', value: location });
            meta.push({ label: '检索分', value: fmtSourceScore(source) });
            return meta;
        }
        function sourceScoreTone(source) {
            if (source?.score_type !== 'rerank' && source?.score_type !== 'vector') return 'neutral';
            const n = clampDisplayScore(source.display_score);
            if (n === null) return 'neutral';
            if (n >= 0.75) return 'high';
            if (n >= 0.5) return 'medium';
            return 'low';
        }
        function openSourceDetail(source) {
            sourceDetail.item = source || null;
            sourceDetail.open = true;
        }

        function exportCSV() {
            const headers = ['时间', '用户UUID', '知识库', '知识库名称', '问题', '状态', '耗时ms', '是否改写', '引用数量'];
            const data = filtered.value.map((r) => [
                fmtTime(r.created_at), r.user_id || '', r.library_slug || '', libDisplay(r.library_slug),
                r.question || '', r.status || '', r.latency_ms != null ? r.latency_ms : '',
                r.rewritten_query ? '是' : '否', (r.sources || []).length,
            ]);
            downloadCSV('chat-logs.csv', headers, data);
        }

        return {
            rows, libs, libsFailed, loading, loadError, filters, page, pageSize, expandRowKeys, sourceDetail,
            filtered, paged, totalCount, fmtTime, fmtLatency, chatStatusLabel, chatStatusTag,
            libDisplay, libNameMap, fmtSourceScore, sourceMeta, sourceScoreTone,
            load, resetFilters, toggleExpand, handleExpandChange, openSourceDetail, exportCSV,
            documentTypeIcon, dataEmpty,
        };
    },
    template: `
    <div class="chat-logs-workspace">
      <header class="chat-logs-header">
        <div>
          <h2 class="chat-logs-title"><local-icon icon="sidebar:qa-log" class="chat-logs-title-icon" />问答日志</h2>
          <p class="chat-logs-desc">记录用户的检索与问答行为，便于问题追踪与效果评估</p>
        </div>
        <div class="chat-logs-header-actions">
          <el-button class="app-refresh-button" @click="load(true)" :loading="loading">
            <span class="app-refresh-icon" aria-hidden="true"></span>刷新
          </el-button>
          <el-button @click="exportCSV" :disabled="!filtered.length">导出 CSV</el-button>
        </div>
      </header>

      <section class="logs-toolbar chat-logs-filter-grid">
        <div class="chat-logs-fg-lib logs-filter-field"><span class="logs-toolbar-label">知识库</span><el-select v-model="filters.library_slug" class="logs-filter-select" placeholder="全部" clearable @change="page=1"><el-option v-for="l in libs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" /></el-select></div>
        <div class="chat-logs-fg-status logs-filter-field"><span class="logs-toolbar-label">状态</span><el-select v-model="filters.status" class="logs-filter-status" placeholder="全部" clearable @change="page=1"><el-option label="成功" value="success" /><el-option label="失败" value="failed" /></el-select></div>
        <div class="chat-logs-fg-rewrite logs-filter-field"><span class="logs-toolbar-label">是否改写</span><el-select v-model="filters.rewrite" class="logs-filter-status" placeholder="全部" clearable @change="page=1"><el-option label="已改写" value="yes" /><el-option label="未改写" value="no" /></el-select></div>
        <div class="chat-logs-fg-sources logs-filter-field"><span class="logs-toolbar-label">是否有引用</span><el-select v-model="filters.hasSources" class="logs-filter-status" placeholder="全部" clearable @change="page=1"><el-option label="有引用" value="yes" /><el-option label="无引用" value="no" /></el-select></div>
        <div class="chat-logs-fg-user logs-filter-field"><span class="logs-toolbar-label">用户 UUID</span><el-input v-model="filters.user_id" class="logs-filter-input" placeholder="UUID" clearable @input="page=1" /></div>
        <div class="chat-logs-fg-time logs-filter-field"><span class="logs-toolbar-label">时间范围</span><el-date-picker v-model="filters.range" class="logs-filter-range" type="datetimerange" range-separator="至" start-placeholder="开始" end-placeholder="结束" @change="page=1" /></div>
        <div class="chat-logs-fg-keyword logs-filter-field"><span class="logs-toolbar-label">问题关键词</span><el-input v-model="filters.keyword" class="logs-filter-input" placeholder="问题关键词" clearable @input="page=1" /></div>
        <div class="chat-logs-fg-actions"><el-button @click="resetFilters" class="chat-logs-action-btn">重置</el-button><el-button type="primary" @click="load(true)" :loading="loading" class="chat-logs-action-btn">查询</el-button></div>
      </section>

      <el-alert v-if="libsFailed" type="warning" :closable="false" show-icon title="知识库列表加载失败，不影响日志查看" />

      <section class="logs-table-card" v-if="!loadError || rows.length">
        <div class="logs-table-shell">
          <el-table :data="paged.items" border v-loading="loading" row-key="message_id" :expand-row-keys="expandRowKeys" @expand-change="handleExpandChange">
            <template #empty><div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>暂无问答日志</p></div></template>
            <el-table-column type="expand">
              <template #default="{row}">
                <div class="logs-detail-grid">
                  <div class="logs-detail-cell"><div class="logs-detail-label">问题</div><div class="logs-detail-text">{{ row.question || '—' }}</div><div v-if="row.rewritten_query" class="logs-detail-sub">检索改写：{{ row.rewritten_query }}</div></div>
                  <div class="logs-detail-cell">
                    <div class="logs-detail-label">回答</div>
                    <div class="logs-detail-text" v-if="row.answer">{{ row.answer }}</div>
                    <div v-else-if="!row.error_message">—</div>
                    <div v-if="row.error_message" class="logs-detail-error">错误：{{ row.error_message }}</div>
                  </div>
                  <div class="logs-detail-cell logs-detail-cell--sources" v-if="row.sources && row.sources.length">
                    <div class="logs-detail-label">引用来源（{{ row.sources.length }}）</div>
                    <div class="logs-source-list">
                      <div class="logs-source-item" v-for="(s, si) in row.sources" :key="si">
                        <img v-if="documentTypeIcon({title: s.title || ''})" :src="documentTypeIcon({title: s.title || ''})" class="logs-source-file-icon" alt="" aria-hidden="true" />
                        <div class="logs-source-content">
                          <div class="logs-source-title">{{ s.title || '(无标题)' }}</div>
                          <div class="logs-source-summary-line">
                            <span class="logs-source-score-pill" :class="'logs-source-score-pill--' + sourceScoreTone(s)">{{ fmtSourceScore(s) }}</span>
                          </div>
                        </div>
                        <div class="logs-source-actions">
                          <button type="button" class="logs-source-detail-link" @click="openSourceDetail(s)">查看详情</button>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div class="logs-detail-cell logs-detail-cell--full">
                    <div class="logs-detail-label">其他信息</div>
                    <div class="logs-detail-meta-row">
                      <span>会话：<span class="mono">{{ row.conversation_id || '—' }}</span></span>
                      <span>消息：<span class="mono">{{ row.message_id || '—' }}</span></span>
                      <span>状态：<el-tag :type="chatStatusTag(row.status)" size="small">{{ chatStatusLabel(row.status) }}</el-tag></span>
                      <span>耗时：{{ fmtLatency(row.latency_ms) }}</span>
                    </div>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="170"><template #default="{row}">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="用户 UUID" width="200"><template #default="{row}"><span class="mono">{{ row.user_id || '—' }}</span></template></el-table-column>
            <el-table-column label="知识库" width="170">
              <template #default="{row}">
                <div>{{ libDisplay(row.library_slug) }}</div>
                <div class="chat-logs-lib-slug" v-if="libNameMap[row.library_slug]">{{ row.library_slug }}</div>
              </template>
            </el-table-column>
            <el-table-column label="问题摘要" min-width="200" show-overflow-tooltip><template #default="{row}">{{ row.question }}</template></el-table-column>
            <el-table-column label="状态" width="90" align="center"><template #default="{row}"><el-tag :type="chatStatusTag(row.status)" size="small">{{ chatStatusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="耗时" width="90" align="center"><template #default="{row}">{{ fmtLatency(row.latency_ms) }}</template></el-table-column>
            <el-table-column label="操作" width="120" align="center"><template #default="{row}"><el-button size="small" type="primary" plain class="chat-logs-row-action" @click.stop="toggleExpand(row)">{{ expandRowKeys.includes(row.message_id) ? '收起详情' : '查看详情' }}</el-button></template></el-table-column>
          </el-table>
        </div>
        <div class="logs-pagination">
          <span>当前加载 {{ totalCount }} 条<span v-if="rows.length >= 500">（已截断至 500 条）</span></span>
          <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="totalCount" :page-sizes="[10, 20, 50]" layout="sizes, prev, pager, next" />
        </div>
      </section>

      <section class="logs-error-state" v-if="loadError && !rows.length">
        <div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>问答日志加载失败</p><el-button type="primary" size="small" @click="load(false)" :loading="loading">重新加载</el-button></div>
      </section>

      <el-dialog v-model="sourceDetail.open" title="引用来源详情" width="720px" class="logs-source-detail-dialog">
        <template v-if="sourceDetail.item">
          <h3 class="logs-source-detail-title">{{ sourceDetail.item.title || '(无标题)' }}</h3>
          <div class="logs-source-detail-meta">
            <span v-for="meta in sourceMeta(sourceDetail.item)" :key="meta.label" class="logs-source-detail-meta-item">
              {{ meta.label }}: <span :class="meta.mono ? 'mono' : ''">{{ meta.value }}</span>
            </span>
          </div>
          <div class="logs-source-detail-text">{{ sourceDetail.item.content || '—' }}</div>
        </template>
      </el-dialog>
    </div>
    `,
};
