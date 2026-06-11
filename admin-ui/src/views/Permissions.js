import { onMounted, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';

const ACTIONS = ['read', 'insert', 'delete'];

export default {
    setup() {
        const users = ref([]);
        const libs = ref([]);
        const selectedUser = ref(null);
        const matrix = reactive({});  // {slug: {read:bool, insert:bool, delete:bool}}
        const initial = reactive({}); // 加载时的快照，用于 diff
        const loading = ref(false);

        async function load() {
            loading.value = true;
            try {
                const [u, l] = await Promise.all([
                    api.listUsers({ limit: 500 }),
                    api.listLibraries({ limit: 500 }),
                ]);
                users.value = u;
                libs.value = l.filter((x) => !x.deleted_at);
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        async function reloadUserPerms() {
            if (!selectedUser.value) return;
            try {
                const perms = await api.listUserPerms(selectedUser.value);
                const next = {};
                for (const lib of libs.value) {
                    next[lib.slug] = { read: false, insert: false, delete: false };
                }
                for (const row of perms) {
                    if (!next[row.library_slug]) continue;
                    for (const a of row.actions) {
                        if (ACTIONS.includes(a)) next[row.library_slug][a] = true;
                    }
                }
                Object.assign(matrix, next);
                // 深拷贝快照
                for (const slug of Object.keys(next)) {
                    initial[slug] = { ...next[slug] };
                }
            } catch (e) { ElMessage.error(e.message); }
        }

        watch(selectedUser, reloadUserPerms);

        async function save() {
            if (!selectedUser.value) {
                ElMessage.warning('请先选择用户');
                return;
            }
            const grants = {};   // slug -> [actions]
            const revokes = {};  // slug -> [actions]
            for (const slug of Object.keys(matrix)) {
                const cur = matrix[slug];
                const old = initial[slug] || { read: false, insert: false, delete: false };
                for (const a of ACTIONS) {
                    if (cur[a] && !old[a]) { (grants[slug] ||= []).push(a); }
                    else if (!cur[a] && old[a]) { (revokes[slug] ||= []).push(a); }
                }
            }
            try {
                for (const [slug, acts] of Object.entries(grants)) {
                    await api.grantPerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
                }
                for (const [slug, acts] of Object.entries(revokes)) {
                    await api.revokePerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
                }
                ElMessage.success(`已保存 (授权 ${Object.values(grants).flat().length} 项 / 撤销 ${Object.values(revokes).flat().length} 项)`);
                reloadUserPerms();
            } catch (e) { ElMessage.error(e.message); }
        }

        onMounted(load);
        return { users, libs, selectedUser, matrix, save, loading };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>权限矩阵</h2>
            <el-button type="primary" @click="save" :disabled="!selectedUser">保存变更</el-button>
        </div>

        <el-card style="margin-bottom:16px">
            <el-form inline>
                <el-form-item label="选择用户">
                    <el-select v-model="selectedUser" filterable placeholder="点击选择" style="width:340px">
                        <el-option v-for="u in users" :key="u.id" :label="(u.username || u.email) + (u.is_superuser ? ' [超管]' : '')" :value="u.id" />
                    </el-select>
                </el-form-item>
                <el-form-item v-if="selectedUser">
                    <el-tag v-if="users.find(u => u.id === selectedUser)?.is_superuser" type="warning">
                        该用户为超管，所有库直通；下表设置仅作显式记录
                    </el-tag>
                </el-form-item>
            </el-form>
        </el-card>

        <el-table v-if="selectedUser" :data="libs" border v-loading="loading">
            <el-table-column prop="slug" label="库唯一ID" width="200" />
            <el-table-column prop="name" label="名称" />
            <el-table-column label="read 读取" width="100" align="center">
                <template #default="{row}">
                    <el-checkbox v-model="matrix[row.slug].read" />
                </template>
            </el-table-column>
            <el-table-column label="insert 写入" width="100" align="center">
                <template #default="{row}">
                    <el-checkbox v-model="matrix[row.slug].insert" />
                </template>
            </el-table-column>
            <el-table-column label="delete 删除" width="100" align="center">
                <template #default="{row}">
                    <el-checkbox v-model="matrix[row.slug].delete" />
                </template>
            </el-table-column>
        </el-table>

        <el-empty v-else description="请先选择一个用户" />
    </div>
    `,
};
