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

const MCP_SETUP_INSTRUCTIONS = [
    '请帮我在支持自定义请求头的 MCP 客户端中添加知识库连接。',
    '先确认设备已连接该服务的局域网或 VPN，且域名在内网解析。',
    '传输方式：Streamable HTTP',
    '服务地址：https://ashark.icu/mcp',
    '请求头名称：Authorization',
    '请求头内容：Bearer <我自己的完整 API Key>',
    'API Key 由我在客户端的密钥或请求头设置中填写；不要让我把 Key 发到聊天里，也不要写进 URL 或共享文件。',
    '连接成功后先调用 list_libraries，再使用返回的知识库 slug 调用 search_knowledge。',
].join('\n');

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

        async function copyMcpInstructions() {
            try {
                await copyTextToClipboard(MCP_SETUP_INSTRUCTIONS);
                ElMessage.success('已复制不含密钥的 MCP 接入说明');
            } catch (error) {
                ElMessage.error(error.message || '复制失败，请手动选择说明文字');
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
            keys, loading, keysError, keysReadState, dialog, submitting, result, docDialog, mcpGuideOpen,
            page, pageSize, organizations, stats, pagination, visibleKeys, showPagination,
            load, openCreate, submit, closeResult, copyPlain, copyMcpInstructions,
            mcpInstructions: MCP_SETUP_INSTRUCTIONS, openApiDoc, revoke,
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

        <section class="api-keys-doc-card" aria-labelledby="api-keys-mcp-title">
            <div class="api-keys-doc-main">
                <h3 id="api-keys-mcp-title">MCP 接入：让 AI 使用你的知识库</h3>
                <p>在局域网或 VPN 内，每个账号用自己的 API Key 连接同一个 MCP 地址；AI 只能访问该账号有权限的知识库。</p>
                <ol class="api-keys-doc-steps">
                    <li>在本页新建并保存自己的 API Key</li>
                    <li>在 MCP 客户端添加 Streamable HTTP 地址 <code>https://ashark.icu/mcp</code></li>
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

        <el-dialog v-model="mcpGuideOpen" title="MCP 使用指南"
                   width="860px" class="api-keys-doc-dialog">
            <div class="api-keys-doc-dialog-body">
                <section>
                    <h4>1. 准备账号与密钥</h4>
                    <p>在本页点击“新建 API Key”，选择所属组织，保存仅显示一次的完整密钥。Key 属于创建它的账号；要查询某个知识库，该账号还需有该库的读取权限。</p>
                    <p>不同账号各用自己的 Key。完整 Key 只填在 MCP 客户端的密钥设置里，不要发给 AI 聊天，也不要放进 URL 或共享配置文件。</p>
                </section>
                <section>
                    <h4>2. 在 MCP 客户端填写连接参数</h4>
                    <p>先连接可访问本服务的局域网或 VPN，并确保域名使用内网解析。然后选择支持自定义 HTTP 请求头的 Streamable HTTP 连接，逐项填写：</p>
                    <pre class="api-keys-doc-code">连接名称：知识库（名称可自定）
传输方式：Streamable HTTP
服务地址：https://ashark.icu/mcp
请求头名称：Authorization
请求头内容：Bearer &lt;你的完整 API Key&gt;</pre>
                    <p><code>Bearer</code> 后面有一个空格。请把完整 Key 填进占位符位置，不要保留尖括号。客户端若只提供 OAuth 登录、无法设置请求头，就不能用这种接入方式。</p>
                </section>
                <section>
                    <h4>3. 让 AI 帮你配置</h4>
                    <p>可以复制下面的非密钥说明发给 AI。真正的 Key 仍由你自己填到客户端的安全设置中。</p>
                    <pre class="api-keys-doc-code">{{ mcpInstructions }}</pre>
                    <el-button @click="copyMcpInstructions">复制给 AI 的说明</el-button>
                </section>
                <section>
                    <h4>4. 验证连接并开始使用</h4>
                    <p>保存连接后，让客户端调用 <code>list_libraries</code>。应看到自己有读取权限的知识库；如果列表为空，请检查本账号的知识库权限。</p>
                    <p>找到知识库 slug 后，可对 AI 说：“用 <code>search_knowledge</code>，<code>knowledge_id</code> 填这个 slug，<code>query</code> 填我的问题。”上传文件时使用 <code>upload_file</code>，还需要该知识库的上传权限。</p>
                </section>
                <section>
                    <h4>5. 连接失败时检查</h4>
                    <ul class="api-keys-doc-list">
                        <li><strong>401 未授权：</strong>检查是否填了完整 Key、<code>Bearer</code> 后的空格，以及 Key 是否过期或已撤销。</li>
                        <li><strong>知识库列表为空或提示无权限：</strong>确认创建 Key 的账号属于正确组织，并已获得目标知识库权限。</li>
                        <li><strong>连接超时、522 或连接时返回 403：</strong>确认设备已连接局域网或 VPN，且域名通过内网解析；VPN 使用独立网段时请联系管理员放行。</li>
                        <li><strong>404 或空响应：</strong>核对地址必须是 <code>https://ashark.icu/mcp</code>；<code>8200/mcp</code> 和 <code>8301/mcp</code> 不是给远程客户端使用的地址。</li>
                        <li><strong>无法填写请求头：</strong>换用支持自定义 HTTP 请求头的 MCP 客户端。</li>
                    </ul>
                    <p>按上面步骤仍失败，请联系管理员，并提供错误提示和发生时间；不要提供完整 API Key。</p>
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
