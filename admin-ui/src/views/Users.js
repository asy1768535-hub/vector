import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const users = ref([]);
        const loading = ref(false);
        const dialog = reactive({ open: false, form: { email: '', password: '', username: '', display_name: '', is_superuser: false } });
        const resetDlg = reactive({ open: false, user: null, pwd: '', confirm: '', loading: false });

        function openReset(u) {
            resetDlg.user = u; resetDlg.pwd = ''; resetDlg.confirm = ''; resetDlg.loading = false; resetDlg.open = true;
        }
        async function submitReset() {
            if ((resetDlg.pwd || '').length < 8) { ElMessage.warning('新密码至少 8 位'); return; }
            if (resetDlg.pwd !== resetDlg.confirm) { ElMessage.warning('两次输入的密码不一致'); return; }
            resetDlg.loading = true;
            try {
                await api.adminResetUserPassword(resetDlg.user.id, resetDlg.pwd);  // 密码只走请求体
                resetDlg.open = false;
                ElMessage.success('密码已重置，请把新密码告知该用户');
            } catch (e) { ElMessage.error(e.message || '重置失败'); }
            finally { resetDlg.loading = false; }
        }

        async function load() {
            loading.value = true;
            try {
                users.value = await api.listUsers({ include_deleted: 'true' });
            } catch (e) {
                ElMessage.error(e.message);
            } finally { loading.value = false; }
        }

        function openCreate() {
            dialog.form = { email: '', password: '', username: '', display_name: '', is_superuser: false };
            dialog.open = true;
        }

        async function submit() {
            try {
                await api.createUser(dialog.form);
                ElMessage.success('用户已创建');
                dialog.open = false;
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function toggleActive(u) {
            try {
                await api.updateUser(u.id, { is_active: !u.is_active });
                ElMessage.success('已更新');
                load();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function toggleSuper(u) {
            try {
                await ElMessageBox.confirm(
                    `确认 ${u.is_superuser ? '取消' : '授予'} ${u.email} 的超管权限?`,
                    '确认', { type: 'warning' }
                );
                await api.updateUser(u.id, { is_superuser: !u.is_superuser });
                ElMessage.success('已更新');
                load();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        async function disable(u) {
            try {
                await ElMessageBox.confirm(`确认禁用 ${u.email}?`, '确认', { type: 'warning' });
                await api.disableUser(u.id);
                ElMessage.success('已禁用');
                load();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        onMounted(load);
        return { users, loading, dialog, resetDlg, load, openCreate, submit, toggleActive, toggleSuper, disable, openReset, submitReset };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>用户管理</h2>
            <div>
                <el-button @click="load" :loading="loading">刷新</el-button>
                <el-button type="primary" @click="openCreate">新建用户</el-button>
            </div>
        </div>
        <el-table :data="users" v-loading="loading" border>
            <el-table-column prop="email" label="邮箱" min-width="220" />
            <el-table-column prop="username" label="用户名" width="120" />
            <el-table-column prop="display_name" label="显示名" width="140" />
            <el-table-column label="角色" width="100">
                <template #default="{row}">
                    <el-tag :type="row.is_superuser ? 'danger' : 'info'" size="small">
                        {{ row.is_superuser ? '超管' : '普通' }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
                <template #default="{row}">
                    <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
                        {{ row.is_active ? '启用' : '禁用' }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="180" />
            <el-table-column label="操作" width="380" fixed="right">
                <template #default="{row}">
                    <el-button size="small" @click="toggleActive(row)">{{ row.is_active ? '禁用' : '启用' }}</el-button>
                    <el-button size="small" type="warning" @click="toggleSuper(row)">{{ row.is_superuser ? '取消超管' : '设为超管' }}</el-button>
                    <el-button size="small" @click="openReset(row)">重置密码</el-button>
                    <el-button size="small" type="danger" :disabled="!row.is_active" @click="disable(row)">软删</el-button>
                </template>
            </el-table-column>
        </el-table>

        <el-dialog v-model="dialog.open" title="新建用户" width="480px">
            <el-form label-width="100px">
                <el-form-item label="邮箱" required>
                    <el-input v-model="dialog.form.email" placeholder="user@example.com" />
                </el-form-item>
                <el-form-item label="初始密码" required>
                    <el-input v-model="dialog.form.password" type="password" show-password />
                </el-form-item>
                <el-form-item label="用户名">
                    <el-input v-model="dialog.form.username" />
                </el-form-item>
                <el-form-item label="显示名">
                    <el-input v-model="dialog.form.display_name" />
                </el-form-item>
                <el-form-item label="超级管理员">
                    <el-switch v-model="dialog.form.is_superuser" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" @click="submit">创建</el-button>
            </template>
        </el-dialog>

        <el-dialog v-model="resetDlg.open" title="重置用户密码" width="420px">
            <el-alert v-if="resetDlg.user" type="info" :closable="false" style="margin-bottom:12px"
                      :title="'为用户 ' + resetDlg.user.email + ' 设置新密码'" />
            <el-form label-position="top" @submit.prevent="submitReset">
                <el-form-item label="新密码（至少 8 位）">
                    <el-input v-model="resetDlg.pwd" type="password" show-password autocomplete="new-password"
                              placeholder="请输入新密码" />
                </el-form-item>
                <el-form-item label="确认新密码">
                    <el-input v-model="resetDlg.confirm" type="password" show-password autocomplete="new-password"
                              placeholder="再次输入新密码" @keyup.enter="submitReset" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="resetDlg.open = false">取消</el-button>
                <el-button type="primary" :loading="resetDlg.loading" @click="submitReset">确认重置</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
