import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';
import { validateCreate } from '../validate.js';
import { dataEmpty } from '../illustrations.js';
import {
    filterUsers, paginateUsers, formatUserTime, userInitial, userRoleLabel, userStatusLabel,
} from '../users_ui.js';
import { createRequestFence, readProjection } from '../read_state_ui.js';

export default {
    setup() {
        const users = ref([]);
        const loading = ref(false);
        const loadStarted = ref(false);
        const usersResolved = ref(false);
        const usersError = ref('');
        const allLibs = ref([]);
        const libsLoadFailed = ref(false);
        const dialog = reactive({ open: false, form: { email: '', password: '', username: '', display_name: '', is_superuser: false }, grantLibs: [] });
        const resetDlg = reactive({ open: false, user: null, pwd: '', confirm: '', loading: false });
        const editDlg = reactive({ open: false, user: null, username: '', display_name: '', loading: false });
        const filters = reactive({ keyword: '', role: '', status: '' });
        const page = ref(1);
        const pageSize = ref(10);
        const submitting = ref(false);
        const usersRequestFence = createRequestFence();

        const selfId = computed(() => store.user?.id);

        // ── Data ─────────────────────────────────────────────
        async function loadUsers(forceRefresh = false) {
            const requestToken = usersRequestFence.begin();
            loadStarted.value = true;
            loading.value = true;
            usersError.value = '';
            try {
                const result = await api.listUsers({ include_deleted: 'false', limit: 500 }, forceRefresh);
                if (!usersRequestFence.isCurrent(requestToken)) return;
                users.value = result;
                usersResolved.value = true;
            } catch (e) {
                if (!usersRequestFence.isCurrent(requestToken)) return;
                usersError.value = e.message || '用户列表加载失败';
            } finally {
                if (usersRequestFence.isCurrent(requestToken)) loading.value = false;
            }
        }

        async function loadLibsForGrant() {
            try { allLibs.value = (await api.listLibraries({ include_deleted: 'false' })).filter((l) => !l.deleted_at); libsLoadFailed.value = false; }
            catch (_) { allLibs.value = []; libsLoadFailed.value = true; }
        }

        const filtered = computed(() => filterUsers(users.value, filters));
        const pagination = computed(() => paginateUsers(filtered.value, page.value, pageSize.value));
        const usersReadState = computed(() => readProjection({
            started: loadStarted.value,
            loading: loading.value,
            hasResolved: usersResolved.value,
            empty: users.value.length === 0,
            error: usersError.value,
        }));

        // ── Filters ──────────────────────────────────────────
        function onFilterChange() { page.value = 1; }

        // ── Create ───────────────────────────────────────────
        function openCreate() { Object.assign(dialog.form, { email: '', password: '', username: '', display_name: '', is_superuser: false }); dialog.grantLibs = []; dialog.open = true; loadLibsForGrant(); }
        async function submitCreate() {
            const errors = validateCreate(dialog.form);
            if (errors.length) { ElMessage.warning(errors.join('；')); return; }
            submitting.value = true;
            try {
                const payload = { email: dialog.form.email, password: dialog.form.password };
                if (dialog.form.username) payload.username = dialog.form.username;
                if (dialog.form.display_name) payload.display_name = dialog.form.display_name;
                payload.is_superuser = dialog.form.is_superuser;
                const created = await api.createUser(payload);
                const slugs = dialog.grantLibs || [];
                let ok = 0;
                for (const slug of slugs) {
                    try { await api.grantPerms({ user_id: created.id, library_slug: slug, actions: ['read', 'insert'] }); ok++; }
                    catch (_) { /* skip */ }
                }
                dialog.open = false;
                if (slugs.length) ElMessage.success(`用户已创建${ok === slugs.length ? `，已授权 ${ok} 个知识库` : ok > 0 ? `，${ok}/${slugs.length} 个知识库授权成功` : '，知识库授权失败'}`);
                else ElMessage.success('用户已创建');
                await loadUsers();
            } catch (e) { ElMessage.error(e.message); }
            finally { submitting.value = false; }
        }

        // ── Edit ─────────────────────────────────────────────
        function openEdit(u) {
            editDlg.user = u;
            editDlg.username = u.username || '';
            editDlg.display_name = u.display_name || '';
            editDlg.loading = false;
            editDlg.open = true;
        }
        async function submitEdit() {
            if (!editDlg.user) return;
            editDlg.loading = true;
            try {
                await api.updateUser(editDlg.user.id, { username: editDlg.username, display_name: editDlg.display_name });
                editDlg.open = false;
                ElMessage.success('已更新');
                await loadUsers();
            } catch (e) { ElMessage.error(e.message); }
            finally { editDlg.loading = false; }
        }

        // ── Reset password ───────────────────────────────────
        function openReset(u) { resetDlg.user = u; resetDlg.pwd = ''; resetDlg.confirm = ''; resetDlg.loading = false; resetDlg.open = true; }
        async function submitReset() {
            if (!resetDlg.pwd || resetDlg.pwd.length < 8) { ElMessage.warning('密码至少 8 个字符'); return; }
            if (resetDlg.pwd !== resetDlg.confirm) { ElMessage.warning('两次输入的密码不一致'); return; }
            resetDlg.loading = true;
            try { await api.adminResetUserPassword(resetDlg.user.id, resetDlg.pwd); resetDlg.open = false; ElMessage.success('密码已重置'); }
            catch (e) { ElMessage.error(e.message); }
            finally { resetDlg.loading = false; }
        }

        // ── Toggle active ────────────────────────────────────
        async function toggleActive(u) {
            if (u.id === selfId.value) { ElMessage.warning('不能操作当前登录用户'); return; }
            try { await api.updateUser(u.id, { is_active: !u.is_active }); u.is_active = !u.is_active; ElMessage.success(u.is_active ? '已启用' : '已停用'); }
            catch (e) { ElMessage.error(e.message); }
        }

        // ── Toggle superuser ─────────────────────────────────
        async function toggleSuper(u) {
            if (u.id === selfId.value) { ElMessage.warning('不能操作当前登录用户'); return; }
            try {
                await ElMessageBox.confirm(u.is_superuser ? `确认取消 ${u.email} 的超级管理员权限？` : `确认将 ${u.email} 设为超级管理员？`, '确认', { type: 'warning' });
            } catch (_) { return; }
            try { await api.updateUser(u.id, { is_superuser: !u.is_superuser }); u.is_superuser = !u.is_superuser; ElMessage.success(u.is_superuser ? '已设为超级管理员' : '已取消超级管理员'); }
            catch (e) { ElMessage.error(e.message); }
        }

        // ── Soft delete ──────────────────────────────────────
        async function disableUser(u) {
            if (u.id === selfId.value) { ElMessage.warning('不能操作当前登录用户'); return; }
            try { await ElMessageBox.confirm(`确认禁用用户 ${u.email}？此操作不可恢复。`, '确认', { type: 'warning' }); }
            catch (_) { return; }
            try { await api.disableUser(u.id); ElMessage.success(`已禁用 ${u.email}`); await loadUsers(); }
            catch (e) { ElMessage.error(e.message); }
        }

        onMounted(loadUsers);

        return {
            users, loading, usersError, usersReadState, allLibs, libsLoadFailed, dialog, resetDlg, editDlg, filters, page, pageSize, submitting,
            pagination, filtered, selfId,
            loadUsers, openCreate, submitCreate, openEdit, submitEdit, openReset, submitReset,
            toggleActive, toggleSuper, disableUser, onFilterChange,
            formatUserTime, userInitial, userRoleLabel, userStatusLabel, dataEmpty,
        };
    },
    template: `
    <div class="users-workspace">
        <div class="users-header">
            <div>
                <h2 class="users-title">用户管理</h2>
                <p class="users-desc">管理系统用户及其权限</p>
            </div>
            <el-button type="primary" @click="openCreate"><local-icon icon="mdi:plus"></local-icon>新建用户</el-button>
        </div>

        <div class="users-toolbar">
            <el-input v-model="filters.keyword" placeholder="搜索邮箱、用户名或显示名" clearable class="users-search" @input="onFilterChange" />
            <el-select v-model="filters.role" placeholder="全部角色" clearable class="users-filter-select" @change="onFilterChange">
                <el-option label="超级管理员" value="superuser" />
                <el-option label="普通用户" value="normal" />
            </el-select>
            <el-select v-model="filters.status" placeholder="全部状态" clearable class="users-filter-select" @change="onFilterChange">
                <el-option label="启用" value="active" />
                <el-option label="停用" value="disabled" />
            </el-select>
            <el-button :loading="loading" @click="loadUsers(true)">刷新</el-button>
        </div>

        <section v-if="usersReadState === 'fatal'" class="users-read-state" role="alert">
            <div>
                <strong>用户列表加载失败</strong>
                <p>{{ usersError }}</p>
            </div>
            <el-button type="primary" :loading="loading" @click="loadUsers(true)">重试</el-button>
        </section>

        <el-alert v-else-if="usersReadState === 'refresh-error'"
                  type="warning" :closable="false" show-icon
                  title="用户列表刷新失败，当前仍显示上次成功加载的数据">
            <template #default>{{ usersError }}</template>
        </el-alert>

        <section v-if="usersReadState !== 'fatal'" class="users-table-card">
            <div class="users-table-shell">
                <el-table :data="pagination.items" v-loading="loading">
                    <template #empty>
                        <div v-if="usersReadState === 'empty'" class="illustration-empty-wrapper">
                            <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                            <p>暂无用户</p>
                        </div>
                    </template>
                    <el-table-column label="用户信息" min-width="180">
                        <template #default="{row}">
                            <div class="users-avatar-cell">
                                <span class="users-avatar">{{ userInitial(row) }}</span>
                                <div class="users-avatar-text">
                                    <div class="users-avatar-name">{{ row.display_name || row.username || '—' }}</div>
                                    <div class="users-avatar-user">{{ row.username || '—' }}</div>
                                </div>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="邮箱" prop="email" min-width="180" show-overflow-tooltip />
                    <el-table-column label="角色" width="100" align="center">
                        <template #default="{row}">
                            <el-tag :type="row.is_superuser ? 'danger' : 'info'" size="small">{{ userRoleLabel(row) }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="状态" width="90" align="center">
                        <template #default="{row}">
                            <span class="users-status-dot" :class="row.is_active ? 'is-active' : 'is-disabled'"></span>
                            {{ userStatusLabel(row) }}
                        </template>
                    </el-table-column>
                    <el-table-column label="验证状态" width="90" align="center">
                        <template #default="{row}">
                            <el-tag :type="row.is_verified ? 'success' : 'info'" size="small">{{ row.is_verified ? '已验证' : '未验证' }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="创建时间" width="160">
                        <template #default="{row}">{{ formatUserTime(row.created_at) }}</template>
                    </el-table-column>
                    <el-table-column label="操作" width="200" fixed="right">
                        <template #default="{row}">
                            <el-button link class="users-action-btn" @click="openReset(row)">重置密码</el-button>
                            <el-switch :model-value="row.is_active" size="small" :disabled="row.id === selfId" @change="toggleActive(row)" />
                            <el-dropdown trigger="click">
                                <el-button link class="users-action-btn">更多<el-icon><local-icon icon="mdi:chevron-down"></local-icon></el-icon></el-button>
                                <template #dropdown>
                                    <el-dropdown-menu>
                                        <el-dropdown-item @click="openEdit(row)">编辑资料</el-dropdown-item>
                                        <el-dropdown-item :disabled="row.id === selfId" @click="toggleSuper(row)">{{ row.is_superuser ? '取消超级管理员' : '设为超级管理员' }}</el-dropdown-item>
                                        <el-dropdown-item :disabled="row.id === selfId" @click="disableUser(row)">软删除</el-dropdown-item>
                                    </el-dropdown-menu>
                                </template>
                            </el-dropdown>
                        </template>
                    </el-table-column>
                </el-table>
            </div>
            <div class="users-pagination">
                <span class="users-pagination-total">共 {{ pagination.total }} 条</span>
                <el-pagination v-model:current-page="page" v-model:page-size="pageSize"
                               :page-sizes="[10, 20, 50]" :total="pagination.total"
                               layout="sizes, prev, pager, next" />
            </div>
        </section>

        <!-- Create dialog -->
        <el-dialog v-model="dialog.open" title="新建用户" width="520px" :close-on-click-modal="false">
            <el-form label-width="80px">
                <el-form-item label="邮箱" required><el-input v-model="dialog.form.email" placeholder="user@example.com" /></el-form-item>
                <el-form-item label="初始密码" required><el-input v-model="dialog.form.password" type="password" show-password placeholder="至少8位" /></el-form-item>
                <el-form-item label="用户名"><el-input v-model="dialog.form.username" placeholder="可选" /></el-form-item>
                <el-form-item label="显示名"><el-input v-model="dialog.form.display_name" placeholder="可选" /></el-form-item>
                <el-form-item label="角色"><el-switch v-model="dialog.form.is_superuser" active-text="超级管理员" inactive-text="普通用户" /></el-form-item>
                <el-form-item label="知识库授权">
                    <el-select v-model="dialog.grantLibs" multiple placeholder="选择知识库（可多选）" class="users-grant-select">
                        <el-option v-for="l in allLibs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                    </el-select>
                    <el-alert v-if="libsLoadFailed" type="warning" title="知识库列表加载失败，可稍后单独授权" :closable="false" show-icon class="users-grant-alert" />
                    <div class="users-grant-hint">授予 read + insert 权限</div>
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
            </template>
        </el-dialog>

        <!-- Edit dialog -->
        <el-dialog v-model="editDlg.open" title="编辑资料" width="440px">
            <el-form label-width="80px">
                <el-form-item label="用户名"><el-input v-model="editDlg.username" /></el-form-item>
                <el-form-item label="显示名"><el-input v-model="editDlg.display_name" /></el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="editDlg.open = false">取消</el-button>
                <el-button type="primary" :loading="editDlg.loading" @click="submitEdit">保存</el-button>
            </template>
        </el-dialog>

        <!-- Reset password dialog -->
        <el-dialog v-model="resetDlg.open" title="重置密码" width="420px">
            <el-alert v-if="resetDlg.user" type="info" :closable="false" show-icon :title="'目标用户：' + resetDlg.user.email" class="users-reset-alert" />
            <el-form label-width="80px">
                <el-form-item label="新密码" required><el-input v-model="resetDlg.pwd" type="password" show-password placeholder="至少8位" /></el-form-item>
                <el-form-item label="确认密码" required><el-input v-model="resetDlg.confirm" type="password" show-password placeholder="再次输入" /></el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="resetDlg.open = false">取消</el-button>
                <el-button type="primary" :loading="resetDlg.loading" @click="submitReset">重置</el-button>
            </template>
        </el-dialog>
    </div>
    `,
};
