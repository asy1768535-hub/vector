import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { copyTextToClipboard } from '../copy_text.js';
import { dataEmpty, apiKeySecurity } from '../illustrations.js';
import {
    formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
    computeStats, paginateKeys,
} from '../api_keys_ui.js';

export default {
    setup() {
        const keys = ref([]);
        const loading = ref(false);
        const dialog = reactive({ open: false, name: '', expiresAt: '' });
        const submitting = ref(false);
        const result = reactive({ open: false, plaintext: '' });
        const page = ref(1);
        const pageSize = 10;

        // ── Derived ──────────────────────────────────────────
        const stats = computed(() => computeStats(keys.value));
        const pagination = computed(() => paginateKeys(keys.value, page.value, pageSize));
        const visibleKeys = computed(() => pagination.value.items);
        const showPagination = computed(() => pagination.value.total > pageSize);

        // ── Data loading ─────────────────────────────────────
        async function load(forceRefresh = false) {
            loading.value = true;
            try {
                keys.value = await api.listApiKeys(forceRefresh);
                if (pagination.value.page !== page.value) {
                    page.value = pagination.value.page;
                }
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        // ── Create ───────────────────────────────────────────
        function openCreate() {
            dialog.name = '';
            dialog.expiresAt = '';
            dialog.open = true;
        }

        async function submit() {
            const name = (dialog.name || '').trim();
            if (!name) { ElMessage.warning('请输入 Key 名称'); return; }
            const expiresAt = dialog.expiresAt ? new Date(dialog.expiresAt).toISOString() : null;
            if (expiresAt && new Date(expiresAt).getTime() <= Date.now()) {
                ElMessage.warning('过期时间必须晚于当前时间');
                return;
            }
            submitting.value = true;
            try {
                const resp = await api.createApiKey(name, expiresAt);
                dialog.open = false;
                result.plaintext = resp.plaintext_key;
                result.open = true;
                page.value = 1;
                await load();
            } catch (e) { ElMessage.error(e.message); }
            finally { submitting.value = false; }
        }

        function closeResult() {
            result.open = false;
            result.plaintext = '';
        }

        // ── Copy ─────────────────────────────────────────────
        async function copyPlain() {
            const value = (result.plaintext || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('已复制到剪贴板');
            } catch (e) { ElMessage.error(e.message || '复制失败，请手动选择内容'); }
        }

        // ── Revoke ───────────────────────────────────────────
        async function revoke(row) {
            try {
                await ElMessageBox.confirm(
                    `撤销 "${row.name}" (${row.key_prefix}…)？此操作不可恢复。`,
                    '确认撤销', { type: 'warning' }
                );
            } catch (_) { return; }
            try {
                await api.revokeApiKey(row.id);
                ElMessage.success('已撤销');
                await load();
            } catch (e) { ElMessage.error(e.message || String(e)); }
        }

        onMounted(load);

        return {
            keys, loading, dialog, submitting, result, page, pageSize,
            stats, pagination, visibleKeys, showPagination,
            load, openCreate, submit, closeResult, copyPlain, revoke, dataEmpty, apiKeySecurity,
            formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
        };
    },
    template: `
    <div class="api-keys-workspace">
        <!-- Header -->
        <div class="api-keys-header">
            <div>
                <h2 class="api-keys-title">我的 API Key</h2>
                <p class="api-keys-desc">管理您的 API 密钥，用于通过 API 访问知识库服务。</p>
            </div>
            <div class="api-keys-header-actions">
                <el-button @click="load(true)" :loading="loading">刷新</el-button>
                <el-button type="primary" @click="openCreate">新建 API Key</el-button>
            </div>
        </div>

        <!-- Security warning -->
        <el-alert type="warning" :closable="false" show-icon
                  title="安全提示"
                  description="API Key 拥有您账户的全部权限，请妥善保管。完整密钥仅在创建时显示一次，之后无法再次查看。" />

        <!-- Stats cards -->
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

        <!-- Table -->
        <section class="api-keys-table-card">
            <div class="api-keys-table-shell">
                <el-table :data="visibleKeys" v-loading="loading">
                    <template #empty><div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>暂无 API Key</p></div></template>
                    <el-table-column label="Key 名称" min-width="140" prop="name" show-overflow-tooltip />
                    <el-table-column label="Key 前缀" min-width="160">
                        <template #default="{row}">
                            <span class="api-keys-prefix">{{ row.key_prefix }}</span>
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
                            <el-button link type="danger"
                                       :disabled="keyStatus(row) !== 'active'"
                                       @click="revoke(row)">撤销</el-button>
                        </template>
                    </el-table-column>
                </el-table>
            </div>
            <div v-if="showPagination" class="api-keys-pagination">
                <el-pagination v-model:current-page="page" :page-size="pageSize"
                               :total="pagination.total" layout="total, prev, pager, next" />
            </div>
        </section>

        <!-- Create dialog -->
        <el-dialog v-model="dialog.open" title="新建 API Key" width="440px" :close-on-click-modal="false">
            <el-form @submit.prevent="submit">
                <el-form-item label="Key 名称" required>
                    <el-input v-model="dialog.name" placeholder="例如：生产环境、测试环境" maxlength="64" />
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

        <!-- Result card (replaces reveal dialog) -->
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
                <p>在 API 请求中通过 <code>Authorization: Bearer &lt;API Key&gt;</code> 头传递密钥。</p>
                <p>示例：</p>
                <pre class="api-keys-usage-code">curl -H "Authorization: Bearer YOUR_KEY" \\
     https://your-domain/api/...</pre>
            </div>
        </div>
    </div>
    `,
};
