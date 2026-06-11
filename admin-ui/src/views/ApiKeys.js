import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const keys = ref([]);
        const loading = ref(false);
        const dialog = reactive({ open: false, name: '' });
        const reveal = reactive({ open: false, plaintext: '', prefix: '' });

        async function load() {
            loading.value = true;
            try { keys.value = await api.listApiKeys(); }
            catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function openCreate() { dialog.name = ''; dialog.open = true; }

        async function submit() {
            if (!dialog.name.trim()) { ElMessage.warning('请输入名称'); return; }
            try {
                const resp = await api.createApiKey(dialog.name);
                dialog.open = false;
                reveal.plaintext = resp.plaintext_key;
                reveal.prefix = resp.key_prefix;
                reveal.open = true;
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function revoke(row) {
            try {
                await ElMessageBox.confirm(`撤销 ${row.name} (${row.key_prefix}…)?`, '确认', { type: 'warning' });
                await api.revokeApiKey(row.id);
                ElMessage.success('已撤销');
                load();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        function copyPlain() {
            navigator.clipboard.writeText(reveal.plaintext);
            ElMessage.success('已复制到剪贴板');
        }

        onMounted(load);
        return { keys, loading, dialog, reveal, openCreate, submit, revoke, copyPlain, load };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>我的 API Key</h2>
            <div>
                <el-button @click="load" :loading="loading">刷新</el-button>
                <el-button type="primary" @click="openCreate">生成新 Key</el-button>
            </div>
        </div>

        <el-alert title="API Key 仅在创建时返回明文一次，请妥善保存。撤销后立即失效。" type="info" :closable="false" style="margin-bottom:16px" />

        <el-table :data="keys" border v-loading="loading">
            <el-table-column prop="name" label="名称" width="200" />
            <el-table-column label="前缀" width="180">
                <template #default="{row}"><span class="mono">{{ row.key_prefix }}…</span></template>
            </el-table-column>
            <el-table-column label="状态" width="100">
                <template #default="{row}">
                    <el-tag :type="row.revoked_at ? 'info' : 'success'" size="small">
                        {{ row.revoked_at ? '已撤销' : '有效' }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column prop="last_used_at" label="最近使用" width="180" />
            <el-table-column prop="expires_at" label="过期时间" width="180" />
            <el-table-column prop="created_at" label="创建时间" width="180" />
            <el-table-column label="操作" width="100" fixed="right">
                <template #default="{row}">
                    <el-button size="small" type="danger" :disabled="!!row.revoked_at" @click="revoke(row)">撤销</el-button>
                </template>
            </el-table-column>
        </el-table>

        <el-dialog v-model="dialog.open" title="生成新 API Key" width="400px">
            <el-form label-width="80px">
                <el-form-item label="名称">
                    <el-input v-model="dialog.name" placeholder="例如：dify-prod / my-app" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" @click="submit">生成</el-button>
            </template>
        </el-dialog>

        <el-dialog v-model="reveal.open" title="新 Key（明文仅本次显示）" width="560px">
            <el-alert type="warning" :closable="false" style="margin-bottom:12px">
                请立即复制并妥善保存。关闭对话框后将无法再次查看明文。
            </el-alert>
            <div class="api-key-box">{{ reveal.plaintext }}</div>
            <template #footer>
                <el-button @click="copyPlain">复制到剪贴板</el-button>
                <el-button type="primary" @click="reveal.open = false">我已保存</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
