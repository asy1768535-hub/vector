import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { copyTextToClipboard } from '../copy_text.js';
import { dataEmpty, apiKeySecurity } from '../illustrations.js';
import { store } from '../store.js';
import {
    formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
    computeStats, paginateKeys,
} from '../api_keys_ui.js';
import { createRequestFence, readProjection } from '../read_state_ui.js';

export default {
    setup() {
        const keys = ref([]);
        const loading = ref(false);
        const loadStarted = ref(false);
        const keysResolved = ref(false);
        const keysError = ref('');
        const keysRequestFence = createRequestFence();
        const dialog = reactive({
            open: false,
            name: '',
            organizationId: '',
            expiresAt: '',
        });
        const submitting = ref(false);
        const result = reactive({ open: false, plaintext: '' });
        const docDialog = reactive({ open: false });
        const page = ref(1);
        const pageSize = 10;

        const organizations = computed(() => store.organizations || []);
        const stats = computed(() => computeStats(keys.value));
        const pagination = computed(() => paginateKeys(keys.value, page.value, pageSize));
        const visibleKeys = computed(() => pagination.value.items);
        const showPagination = computed(() => pagination.value.total > pageSize);

        async function load(forceRefresh = false) {
            const requestToken = keysRequestFence.begin();
            loadStarted.value = true;
            loading.value = true;
            keysError.value = '';
            try {
                const response = await api.listApiKeys(forceRefresh);
                if (!keysRequestFence.isCurrent(requestToken)) return;
                keys.value = response;
                keysResolved.value = true;
                if (pagination.value.page !== page.value) page.value = pagination.value.page;
            } catch (error) {
                if (!keysRequestFence.isCurrent(requestToken)) return;
                keysError.value = error.message || 'API Key 列表加载失败';
            } finally {
                if (keysRequestFence.isCurrent(requestToken)) loading.value = false;
            }
        }

        const keysReadState = computed(() => readProjection({
            started: loadStarted.value,
            loading: loading.value,
            hasResolved: keysResolved.value,
            empty: keys.value.length === 0,
            error: keysError.value,
        }));

        function openCreate() {
            dialog.name = '';
            dialog.organizationId = organizations.value[0]?.organization_id || '';
            dialog.expiresAt = '';
            dialog.open = true;
        }

        async function submit() {
            const name = (dialog.name || '').trim();
            if (!name) { ElMessage.warning('请输入 Key 名称'); return; }
            if (organizations.value.length && !dialog.organizationId) {
                ElMessage.warning('请选择 API Key 所属组织');
                return;
            }
            const expiresAt = dialog.expiresAt ? new Date(dialog.expiresAt).toISOString() : null;
            if (expiresAt && new Date(expiresAt).getTime() <= Date.now()) {
                ElMessage.warning('过期时间必须晚于当前时间');
                return;
            }
            submitting.value = true;
            try {
                const response = await api.createApiKey(
                    name,
                    dialog.organizationId || null,
                    expiresAt,
                );
                dialog.open = false;
                result.plaintext = response.plaintext_key;
                result.open = true;
                page.value = 1;
                await load();
            } catch (error) {
                ElMessage.error(error.message || 'API Key 创建失败');
            } finally {
                submitting.value = false;
            }
        }

        function closeResult() {
            result.open = false;
            result.plaintext = '';
        }

        async function copyPlain() {
            const value = (result.plaintext || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('已复制到剪贴板');
            } catch (error) {
                ElMessage.error(error.message || '复制失败，请手动选择内容');
            }
        }

        function openApiDoc() {
            docDialog.open = true;
        }

        async function revoke(row) {
            try {
                await ElMessageBox.confirm(
                    `撤销 "${row.name}" (${row.key_prefix}...)？此操作不可恢复。`,
                    '确认撤销',
                    { type: 'warning' },
                );
            } catch (_) {
                return;
            }
            try {
                await api.revokeApiKey(row.id);
                ElMessage.success('已撤销');
                await load();
            } catch (error) {
                ElMessage.error(error.message || String(error));
            }
        }

        function organizationName(organizationId) {
            const row = organizations.value.find(
                (item) => String(item.organization_id) === String(organizationId),
            );
            return row?.name || '默认组织';
        }

        onMounted(load);

        return {
            keys, loading, keysError, keysReadState, dialog, submitting, result, docDialog,
            page, pageSize, organizations, stats, pagination, visibleKeys, showPagination,
            load, openCreate, submit, closeResult, copyPlain, openApiDoc, revoke,
            organizationName, dataEmpty, apiKeySecurity,
            formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
        };
    },
    template: `
    <div class="api-keys-workspace">
        <div class="api-keys-header">
            <div>
                <h2 class="api-keys-title">我的 API Key</h2>
                <p class="api-keys-desc">管理外部系统访问知识库时使用的密钥。</p>
            </div>
            <div class="api-keys-header-actions">
                <el-button class="app-refresh-button" @click="load(true)" :loading="loading">
                    <span class="app-refresh-icon" aria-hidden="true"></span>刷新
                </el-button>
                <el-button type="primary" @click="openCreate">新建 API Key</el-button>
            </div>
        </div>

        <el-alert type="warning" :closable="false" show-icon
                  title="安全提示"
                  description="完整密钥仅在创建时显示一次。请按接入系统分别创建，并在停用后及时撤销。" />

        <section class="api-keys-doc-card">
            <div class="api-keys-doc-main">
                <h3>API 接入说明：获取智能问答结果</h3>
                <p><code>POST /api/v1/answers</code> 返回最终 <code>answer</code>，并同时返回 <code>sources</code>、<code>chunks</code> 和 <code>graph</code> 作为可追溯依据。</p>
                <p class="api-keys-doc-note">请求必须明确选择当前 Key 所属组织内、有读取权限的知识库 slug；流式问答使用 <code>POST /api/v1/answers/stream</code>。</p>
                <ol class="api-keys-doc-steps">
                    <li>选择所属组织，创建并保存 API Key</li>
                    <li>配置 <code>VECTOR_KB_BASE_URL</code>、<code>VECTOR_KB_LIBRARY_ID</code>、<code>VECTOR_KB_API_KEY</code></li>
                    <li>调用问答接口，并展示答案、普通来源和图谱证据</li>
                </ol>
            </div>
            <div class="api-keys-doc-actions">
                <el-button class="api-keys-doc-template-button" @click="openApiDoc">快速接入模板</el-button>
            </div>
        </section>

        <section v-if="keysReadState === 'fatal'" class="app-read-state app-read-state--error" role="alert">
            <div><strong>API Key 列表加载失败</strong><p>{{ keysError }}</p></div>
            <el-button type="primary" :loading="loading" @click="load(true)">重试</el-button>
        </section>
        <section v-else-if="keysReadState === 'idle' || keysReadState === 'loading'"
                 class="app-read-state" v-loading="true">
            <span>正在加载 API Key</span>
        </section>

        <template v-else>
            <el-alert v-if="keysReadState === 'refresh-error'"
                      type="warning" :closable="false" show-icon
                      title="API Key 列表刷新失败，当前仍显示上次成功加载的数据"
                      :description="keysError" />

            <div class="api-keys-stats">
                <div class="api-keys-stat-card">
                    <div class="api-keys-stat-num">{{ stats.total }}</div>
                    <div class="api-keys-stat-label">全部</div>
                </div>
                <div class="api-keys-stat-card api-keys-stat-card--active">
                    <div class="api-keys-stat-num">{{ stats.active }}</div>
                    <div class="api-keys-stat-label">启用</div>
                </div>
                <div class="api-keys-stat-card api-keys-stat-card--revoked">
                    <div class="api-keys-stat-num">{{ stats.revoked }}</div>
                    <div class="api-keys-stat-label">已撤销</div>
                </div>
                <div class="api-keys-stat-card api-keys-stat-card--expired">
                    <div class="api-keys-stat-num">{{ stats.expired }}</div>
                    <div class="api-keys-stat-label">已过期</div>
                </div>
            </div>

            <section class="api-keys-table-card">
                <div class="api-keys-table-shell">
                    <el-table :data="visibleKeys" v-loading="loading">
                        <template #empty>
                            <div v-if="keysReadState === 'empty'" class="illustration-empty-wrapper">
                                <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                                <p>暂无 API Key</p>
                            </div>
                        </template>
                        <el-table-column label="Key 名称" min-width="140" prop="name" show-overflow-tooltip />
                        <el-table-column label="所属组织" min-width="140">
                            <template #default="{row}">{{ organizationName(row.organization_id) }}</template>
                        </el-table-column>
                        <el-table-column label="Key 前缀" min-width="160">
                            <template #default="{row}"><span class="api-keys-prefix">{{ row.key_prefix }}</span></template>
                        </el-table-column>
                        <el-table-column label="创建时间" width="170">
                            <template #default="{row}">{{ formatKeyTime(row.created_at) }}</template>
                        </el-table-column>
                        <el-table-column label="最后使用" width="170">
                            <template #default="{row}">{{ formatKeyTime(row.last_used_at) }}</template>
                        </el-table-column>
                        <el-table-column label="过期时间" width="170">
                            <template #default="{row}">{{ formatKeyTime(row.expires_at) }}</template>
                        </el-table-column>
                        <el-table-column label="状态" width="90" align="center">
                            <template #default="{row}">
                                <el-tag :type="STATUS_TAG[keyStatus(row)]" size="small">{{ STATUS_LABEL[keyStatus(row)] }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="90" align="center">
                            <template #default="{row}">
                                <el-button link type="danger" :disabled="keyStatus(row) !== 'active'" @click="revoke(row)">撤销</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>
                <div v-if="showPagination" class="api-keys-pagination">
                    <el-pagination v-model:current-page="page" :page-size="pageSize"
                                   :total="pagination.total" layout="total, prev, pager, next" />
                </div>
            </section>
        </template>

        <el-dialog v-model="docDialog.open" title="完整接入模板"
                   width="860px" class="api-keys-doc-dialog">
            <div class="api-keys-doc-dialog-body">
                <section>
                    <h4>1. 环境变量</h4>
                    <pre class="api-keys-doc-code">VECTOR_KB_BASE_URL=https://your-vector-kb.example.com
VECTOR_KB_LIBRARY_ID=your_library_slug
VECTOR_KB_API_KEY=你的完整API_KEY</pre>
                </section>
                <section>
                    <h4>2. 同步智能问答</h4>
                    <pre class="api-keys-doc-code">curl -X POST "$VECTOR_KB_BASE_URL/api/v1/answers" \\
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "scope": {"library_slugs": ["your_library_slug"]},
    "query": "你的问题",
    "top_k": 5,
    "candidate_k": 20
  }'</pre>
                    <p>流式问答使用 <code>POST /api/v1/answers/stream</code>，请求体相同，并设置 <code>Accept: text/event-stream</code>。</p>
                </section>
                <section>
                    <h4>3. 返回结果与证据</h4>
                    <ul class="api-keys-doc-list">
                        <li><code>answer</code>：最终自然语言回答。</li>
                        <li><code>sources</code>：文档、修订、切片、页码、标题路径和分数等引用位置。</li>
                        <li><code>chunks</code>：与 <code>sources</code> 同排名的证据正文。</li>
                        <li><code>graph.entities</code> / <code>graph.relations</code>：发布后的图谱事实；每条事实的 <code>evidence[].evidence_id</code> 可继续读取证据详情。</li>
                    </ul>
                    <pre class="api-keys-doc-code">GET /api/v1/libraries/{slug}/evidence/{evidence_id}
Authorization: Bearer &lt;API_KEY&gt;</pre>
                </section>
                <section>
                    <h4>4. 兼容接口</h4>
                    <p><code>POST /libraries/{slug}/query</code> 只返回检索切片。旧的 <code>POST /chat/messages</code> 可能分别返回 <code>sources</code> 或 <code>graph_evidence</code>；新接入请使用固定结构的 Public v1。</p>
                </section>
            </div>
        </el-dialog>

        <el-dialog v-model="dialog.open" title="新建 API Key" width="440px" :close-on-click-modal="false">
            <el-form @submit.prevent="submit">
                <el-form-item label="Key 名称" required>
                    <el-input v-model="dialog.name" placeholder="例如：生产环境、测试环境" maxlength="64" />
                </el-form-item>
                <el-form-item v-if="organizations.length" label="所属组织" required>
                    <el-select v-model="dialog.organizationId" placeholder="请选择组织">
                        <el-option v-for="item in organizations" :key="item.organization_id"
                                   :label="item.name" :value="String(item.organization_id)" />
                    </el-select>
                </el-form-item>
                <el-form-item label="过期时间">
                    <el-date-picker v-model="dialog.expiresAt" type="datetime"
                                    placeholder="可选，不填则永不过期"
                                    format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm"
                                    :disabled-date="(d) => d < Date.now() - 86400000" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" :loading="submitting" @click="submit">生成</el-button>
            </template>
        </el-dialog>

        <div v-if="result.open" class="api-keys-result-card">
            <div class="api-keys-result-left">
                <div class="api-keys-result-head">
                    <local-icon icon="mdi:key-variant" class="api-keys-result-icon"></local-icon>
                    <span class="api-keys-result-title">API Key 创建成功</span>
                </div>
                <p class="api-keys-result-warn">以下完整密钥仅显示一次，请立即复制并妥善保存。</p>
                <div class="api-keys-plaintext">{{ result.plaintext }}</div>
                <div class="api-keys-result-actions">
                    <el-button type="primary" @click="copyPlain">复制到剪贴板</el-button>
                    <el-button @click="closeResult">我已复制并保存</el-button>
                </div>
            </div>
            <div class="api-keys-result-right">
                <img :src="apiKeySecurity" class="illustration-api-key-security" alt="" aria-hidden="true" />
                <h4>使用方式</h4>
                <p>通过 <code>Authorization: Bearer &lt;API Key&gt;</code> 请求 <code>/api/v1/answers</code>。</p>
                <pre class="api-keys-usage-code">POST BASE_URL/api/v1/answers
Authorization: Bearer YOUR_KEY
Content-Type: application/json</pre>
            </div>
        </div>
    </div>
    `,
};
