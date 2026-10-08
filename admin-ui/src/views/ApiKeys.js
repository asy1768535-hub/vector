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

import ApiAccessGuide from '../components/ApiAccessGuide.js';
import { createAccessGuide, accessGuideText } from '../api_access_guide.js';

const guideBaseUrl = globalThis.location?.origin;
const httpGuide = createAccessGuide('http', guideBaseUrl);
const mcpGuide = createAccessGuide('mcp', guideBaseUrl);
const MCP_SETUP_INSTRUCTIONS = accessGuideText(mcpGuide);

export default {
    components: { ApiAccessGuide },
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
        const result = reactive({ open: false, plaintext: '', keyId: '' });
        const maskedPlaintext = computed(() => result.plaintext
            ? `${result.plaintext.slice(0, 8)}…${result.plaintext.slice(-4)}` : '');
        const docDialog = reactive({ open: false });
        const mcpGuideOpen = ref(false);
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
                if (response.some(row => row.id === result.keyId && row.revoked_at)) closeResult();
                keys.value = response.filter(row => !row.revoked_at);
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
                result.keyId = response.id;
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
            result.keyId = '';
        }

        function canCopyKey(row) {
            return Boolean(result.plaintext && row.id === result.keyId && keyStatus(row) === 'active');
        }

        function displayKey(row) {
            return row.id === result.keyId && result.plaintext
                ? maskedPlaintext.value : `${row.key_prefix}…`;
        }

        async function copyPlain() {
            const value = (result.plaintext || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('已复制到剪贴板');
            } catch (error) {
                ElMessage.error('复制失败，请重试复制');
            }
        }

        async function copyMcpInstructions() {
            try {
                await copyTextToClipboard(MCP_SETUP_INSTRUCTIONS);
                ElMessage.success('已复制不含密钥的完整 MCP 接入说明');
            } catch (error) {
                ElMessage.error(error.message || '复制失败，请手动选择说明文字');
            }
        }

        async function copyApiInstructions() {
            try {
                await copyTextToClipboard(accessGuideText(httpGuide));
                ElMessage.success('已复制不含密钥的完整 HTTP API 接入说明');
            } catch (error) {
                ElMessage.error('复制失败，请手动选择说明文字');
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
                keys.value = keys.value.filter(key => key.id !== row.id);
                page.value = pagination.value.page;
                if (row.id === result.keyId) closeResult();
                ElMessage.success('已撤销');
                await load(true);
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
            keys, loading, keysError, keysReadState, dialog, submitting, result, docDialog, mcpGuideOpen,
            page, pageSize, organizations, stats, pagination, visibleKeys, showPagination,
            load, openCreate, submit, closeResult, copyPlain, copyMcpInstructions,
            maskedPlaintext, canCopyKey, displayKey,
            httpGuide, mcpGuide, copyApiInstructions, openApiDoc, revoke,
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
                  description="完整密钥仅在创建后可复制，请立即保存。页面刷新或关闭创建结果后无法再次复制；已撤销的密钥不再显示。" />

        <section class="api-keys-doc-card">
            <div class="api-keys-doc-main">
                <h3>API 接入说明：获取智能问答结果</h3>
                <p><code>POST /api/v1/answers</code> 返回最终 <code>answer</code>，并同时返回 <code>sources</code>、<code>chunks</code> 和 <code>graph</code> 作为可追溯依据。</p>
                <p class="api-keys-doc-note">请求必须明确选择当前 Key 所属组织内、有读取权限的知识库 slug；流式问答使用 <code>POST /api/v1/answers/stream</code>。</p>
                <ol class="api-keys-doc-steps">
                    <li>选择所属组织，创建并保存 API Key</li>
                    <li>配置 <code>VECTOR_KB_BASE_URL</code>、<code>VECTOR_KB_LIBRARY_ID</code>、<code>VECTOR_KB_API_KEY</code></li>
                    <li>按指南发现知识库、检索或问答，并追溯图谱 evidence_id</li>
                </ol>
            </div>
            <div class="api-keys-doc-actions">
                <el-button class="api-keys-doc-template-button" @click="openApiDoc">查看 API 完整指南</el-button>
            </div>
        </section>

        <section class="api-keys-doc-card" aria-labelledby="api-keys-mcp-title">
            <div class="api-keys-doc-main">
                <h3 id="api-keys-mcp-title">MCP 接入：让 AI 使用你的知识库</h3>
                <p>每个账号用自己的 API Key 连接同一个 MCP 地址；AI 只能访问该账号有权限的知识库。</p>
                <ol class="api-keys-doc-steps">
                    <li>在本页新建并保存自己的 API Key</li>
                    <li>在 MCP 客户端添加 Streamable HTTP 地址 <code>https://vkb.gshbzw.com/mcp</code></li>
                    <li>填写 Bearer 请求头，连接后调用 <code>list_libraries</code></li>
                </ol>
                <p class="api-keys-doc-note">完整 Key 只填在客户端的密钥设置中，不要发给 AI 聊天或其他人。</p>
            </div>
            <div class="api-keys-doc-actions">
                <el-button type="primary" @click="mcpGuideOpen = true">查看 MCP 完整教程</el-button>
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
                        <el-table-column label="API 密钥" min-width="200">
                            <template #default="{row}">
                                <span class="api-keys-prefix">{{ displayKey(row) }}</span>
                                <el-tooltip :content="canCopyKey(row) ? '复制完整密钥' : '完整密钥仅在创建后可复制，请使用已保存的密钥'">
                                    <span>
                                        <el-button link :disabled="!canCopyKey(row)" aria-label="复制完整密钥" @click="copyPlain">
                                            <local-icon icon="mdi:content-copy" aria-hidden="true"></local-icon>
                                        </el-button>
                                    </span>
                                </el-tooltip>
                            </template>
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

        <el-dialog v-model="docDialog.open" title="HTTP API 完整接入模板与指南"
                   width="860px" class="api-keys-doc-dialog">
            <div class="api-keys-doc-dialog-body">
                <api-access-guide :guide="httpGuide" />
            </div>
            <template #footer>
                <div class="api-access-guide-footer">
                    <span>复制内容包含完整说明与示例，不含实际密钥。</span>
                    <el-button type="primary" @click="copyApiInstructions">复制完整 API 说明给智能体</el-button>
                </div>
            </template>
        </el-dialog>

        <el-dialog v-model="mcpGuideOpen" title="MCP 完整接入指南"
                   width="860px" class="api-keys-doc-dialog">
            <div class="api-keys-doc-dialog-body">
                <api-access-guide :guide="mcpGuide" />
            </div>
            <template #footer>
                <div class="api-access-guide-footer">
                    <span>复制内容包含完整说明与示例，不含实际密钥。</span>
                    <el-button type="primary" @click="copyMcpInstructions">复制完整 MCP 说明给智能体</el-button>
                </div>
            </template>
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
                <p class="api-keys-result-warn">密钥已隐藏部分字符，复制时会获得完整值。请立即保存，页面刷新或关闭后无法再次复制。</p>
                <div class="api-keys-plaintext">{{ maskedPlaintext }}</div>
                <div class="api-keys-result-actions">
                    <el-button type="primary" @click="copyPlain">复制完整密钥</el-button>
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
