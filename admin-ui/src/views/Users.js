import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const users = ref([]);
        const loading = ref(false);
        const dialog = reactive({ open: false, form: { email: '', password: '', username: '', display_name: '', is_superuser: false } });

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
        return { users, loading, dialog, load, openCreate, submit, toggleActive, toggleSuper, disable };
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
            <el-table-column label="操作" width="280" fixed="right">
                <template #default="{row}">
                    <el-button size="small" @click="toggleActive(row)">{{ row.is_active ? '禁用' : '启用' }}</el-button>
                    <el-button size="small" type="warning" @click="toggleSuper(row)">{{ row.is_superuser ? '取消超管' : '设为超管' }}</el-button>
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
    </div>
    `,
};
