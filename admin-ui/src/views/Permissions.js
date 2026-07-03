import { onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import * as api from "../api.js";
import { dataEmpty } from "../illustrations.js";
import {
    computePermissionDiff,
    countUnsaved,
    permissionSummary,
    paginatePermissions,
    filterPermissions,
    userMeta,
} from "../permissions_ui.js";

const ACTIONS = ["read", "insert", "delete"];

export default {
    setup() {
        const users = ref([]);
        const libs = ref([]);
        const selectedUser = ref(null);
        const matrix = reactive({});
        const initial = reactive({});
        const loading = ref(false);
        const permissionsLoading = ref(false);
        const permissionsReady = ref(false);
        const permissionsError = ref(false);
        const saving = ref(false);
        const keyword = ref("");
        const page = ref(1);
        const pageSize = ref(10);
        let fetchSeq = 0;

        async function load() {
            loading.value = true;
            try {
                const [u, l] = await Promise.all([
                    api.listUsers({ limit: 500 }),
                    api.listLibraries({ limit: 500 }),
                ]);
                users.value = u;
                libs.value = l.filter((x) => !x.deleted_at);
            } catch (e) {
                ElMessage.error(e.message);
            } finally {
                loading.value = false;
            }
        }

        async function reloadUserPerms() {
            if (!selectedUser.value) return;
            const seq = ++fetchSeq;
            const userId = selectedUser.value;
            permissionsLoading.value = true;
            permissionsReady.value = false;
            permissionsError.value = false;
            try {
                const perms = await api.listUserPerms(userId);
                if (seq !== fetchSeq) return;
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
                if (seq === fetchSeq) {
                    for (const slug of Object.keys(matrix)) delete matrix[slug];
                    for (const slug of Object.keys(initial)) delete initial[slug];
                    Object.assign(matrix, next);
                    for (const slug of Object.keys(next)) {
                        initial[slug] = { ...next[slug] };
                    }
                    permissionsReady.value = true;
                }
            } catch (e) {
                if (seq === fetchSeq) {
                    ElMessage.error(e.message);
                    permissionsError.value = true;
                }
            } finally {
                if (seq === fetchSeq) permissionsLoading.value = false;
            }
        }

        watch(selectedUser, () => {
            page.value = 1;
            // 立即清空旧数据，防止新权限返回前短暂显示上一用户权限
            for (const slug of Object.keys(matrix)) delete matrix[slug];
            for (const slug of Object.keys(initial)) delete initial[slug];
            permissionsReady.value = false;
            permissionsError.value = false;
            reloadUserPerms();
        });

        watch(keyword, () => { page.value = 1; });

        const currentUser = ref(null);
        watch(selectedUser, (id) => {
            currentUser.value = users.value.find((u) => u.id === id) || null;
        }, { immediate: true });

        const filteredLibs = ref([]);
        watch([libs, keyword], () => {
            filteredLibs.value = filterPermissions(libs.value, keyword.value);
            page.value = 1;
        }, { immediate: true });

        const paged = ref({ rows: [], page: 1, total: 0, pageCount: 0 });
        watch([filteredLibs, page, pageSize], () => {
            paged.value = paginatePermissions(filteredLibs.value, page.value, pageSize.value);
        }, { immediate: true });

        function hasChanges() {
            return countUnsaved(matrix, initial) > 0;
        }

        async function save() {
            if (!selectedUser.value) {
                ElMessage.warning("请先选择用户");
                return;
            }
            if (saving.value) return;
            saving.value = true;
            const { grants, revokes } = computePermissionDiff(matrix, initial);
            const grantCount = Object.values(grants).flat().length;
            const revokeCount = Object.values(revokes).flat().length;
            try {
                for (const [slug, acts] of Object.entries(grants)) {
                    await api.grantPerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
                }
                for (const [slug, acts] of Object.entries(revokes)) {
                    await api.revokePerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
                }
                ElMessage.success(`已保存（授权 ${grantCount} 项 / 撤销 ${revokeCount} 项）`);
                await reloadUserPerms();
            } catch (e) {
                ElMessage.error(e.message);
                await reloadUserPerms();
            } finally {
                saving.value = false;
            }
        }

        function reset() {
            for (const slug of Object.keys(matrix)) {
                const snap = initial[slug] || { read: false, insert: false, delete: false };
                Object.assign(matrix[slug], snap);
            }
            ElMessage.success("已重置为上次保存状态");
        }

        function permissionRemark(perms) {
            const p = perms || {};
            if (p.read && p.insert && p.delete) return "全部权限";
            if (p.read && p.insert) return "允许补充文档";
            if (p.read && p.delete) return "可查看与删除";
            if (p.insert && p.delete) return "可写入与删除";
            if (p.read) return "仅查询";
            if (p.insert) return "仅写入";
            if (p.delete) return "仅删除";
            return "无访问权限";
        }

        onMounted(load);

        return {
            users, libs, selectedUser, matrix, initial,
            loading, permissionsLoading, permissionsReady, permissionsError, saving,
            keyword, page, pageSize, paged, filteredLibs,
            currentUser, hasChanges, save, reset,
            dataEmpty, permissionSummary, permissionRemark, userMeta, countUnsaved,
        };
    },
    template: `
    <div class="permissions-workspace">
      <section class="permissions-panel">
        <header class="permissions-header">
          <div class="permissions-header-copy">
            <h2 class="permissions-title"><local-icon icon="sidebar:permission" class="permissions-title-icon" />权限矩阵</h2>
            <p class="permissions-desc">按用户配置各知识库访问权限</p>
          </div>
        </header>

        <section class="permissions-toolbar">
          <div class="permissions-toolbar-row">
            <span class="permissions-toolbar-label">选择用户</span>
            <el-select v-model="selectedUser" filterable placeholder="点击选择用户"
                       class="permissions-user-select" :disabled="saving">
              <el-option
                  v-for="u in users"
                  :key="u.id"
                  :label="(u.username || u.email) + (u.is_superuser ? '（超管）' : '')"
                  :value="u.id"
              />
            </el-select>
            <template v-if="currentUser">
              <span class="permissions-user-info">
                所属角色：<el-tag :type="userMeta(currentUser).roleType" size="small">{{ userMeta(currentUser).role }}</el-tag>
              </span>
              <span class="permissions-user-info">
                状态：<el-tag :type="userMeta(currentUser).statusType" size="small">{{ userMeta(currentUser).status }}</el-tag>
              </span>
            </template>
            <span v-if="currentUser" class="permissions-config-hint">可为该用户配置各知识库的读取、写入、删除权限</span>
            <span v-if="currentUser && !currentUser.is_superuser" class="permissions-save-hint">
              <local-icon icon="sidebar:permission" />勾选后点击保存按钮生效
            </span>
            <span v-if="permissionsReady && hasChanges()" class="permissions-unsaved-badge">
              未保存变更：<el-tag type="warning" size="small">{{ countUnsaved(matrix, initial) }}</el-tag>
            </span>
            <div class="permissions-toolbar-actions">
              <el-button type="primary" @click="save"
                         :disabled="!permissionsReady || saving || !hasChanges()"
                         :loading="saving">
                {{ saving ? "保存中…" : "保存权限" }}
              </el-button>
              <el-button @click="reset" :disabled="!permissionsReady || saving">
                重置
              </el-button>
            </div>
          </div>
        </section>

        <el-card v-if="currentUser?.is_superuser" shadow="never" class="permissions-super-warning">
          <div class="permissions-super-warning-body">
            <local-icon icon="sidebar:permission" class="permissions-super-warning-icon" />
            <span>该用户为超级管理员，默认拥有全部知识库访问权限；下表设置仅作为显式记录。</span>
          </div>
        </el-card>

        <section class="permissions-matrix-tools" v-if="selectedUser">
          <div class="permissions-search-wrap">
            <el-input v-model="keyword" placeholder="搜索知识库名称"
                      clearable class="permissions-search" />
            <span class="permissions-match-info" v-if="filteredLibs.length !== libs.length">
              匹配 {{ filteredLibs.length }} / {{ libs.length }} 个库
            </span>
          </div>
        </section>

        <section class="permissions-table-card" v-if="selectedUser && permissionsReady">
          <div class="permissions-table-shell">
            <el-table :data="paged.rows" border>
              <template #empty>
                <div class="illustration-empty-wrapper">
                  <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
                  <p>暂无知识库</p>
                </div>
              </template>
              <el-table-column label="知识库名称" min-width="240">
                <template #default="{ row }">
                  <div class="permissions-library-cell">
                    <div class="permissions-library-name">{{ row.name }}</div>
                    <div class="permissions-library-id">库唯一ID：{{ row.slug }}</div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="读取权限" width="150" align="center">
                <template #default="{ row }">
                  <el-checkbox v-model="matrix[row.slug].read" :disabled="saving" />
                </template>
              </el-table-column>
              <el-table-column label="写入权限" width="150" align="center">
                <template #default="{ row }">
                  <el-checkbox v-model="matrix[row.slug].insert" :disabled="saving" />
                </template>
              </el-table-column>
              <el-table-column label="删除权限" width="150" align="center">
                <template #default="{ row }">
                  <el-checkbox v-model="matrix[row.slug].delete" :disabled="saving" />
                </template>
              </el-table-column>
              <el-table-column label="备注" aria-label="权限摘要" width="190">
                <template #default="{ row }">
                  <span class="permissions-remark" :title="permissionSummary(matrix[row.slug] || {})">
                    {{ permissionRemark(matrix[row.slug] || {}) }}
                  </span>
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="permissions-pagination" v-if="paged.total > pageSize">
            <span>共 {{ paged.total }} 条</span>
            <el-pagination
                v-model:current-page="page" v-model:page-size="pageSize"
                :total="paged.total" :page-sizes="[5, 10, 20, 50]"
                layout="total, sizes, prev, pager, next, jumper"
                small
            />
          </div>
        </section>

        <section class="permissions-table-card" v-else-if="selectedUser && permissionsLoading">
          <div class="permissions-table-shell">
            <el-table :data="[]" border v-loading="true">
              <el-table-column label="知识库名称" min-width="240" />
              <el-table-column label="读取权限" width="150" />
              <el-table-column label="写入权限" width="150" />
              <el-table-column label="删除权限" width="150" />
              <el-table-column label="备注" width="190" />
            </el-table>
          </div>
        </section>

        <section class="permissions-table-card permissions-state-card" v-else-if="selectedUser && permissionsError">
          <div class="illustration-empty-wrapper">
            <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
            <p>权限加载失败，请重新选择用户</p>
          </div>
        </section>

        <el-empty v-else-if="!selectedUser" description="请先选择一个用户" class="permissions-empty-state">
          <template #image>
            <img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" />
          </template>
        </el-empty>
      </section>
    </div>
    `,
};
