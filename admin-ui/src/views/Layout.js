import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { store } from '../store.js';
import { menuAccess } from '../menu_access.js';
import * as api from '../api.js';

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const collapsed = ref(false);
        const isNarrow = ref(window.innerWidth < 1200);
        const effectiveCollapsed = computed(() => collapsed.value || isNarrow.value);

        function onResize() { isNarrow.value = window.innerWidth < 1200; }
        onMounted(() => window.addEventListener('resize', onResize));
        onBeforeUnmount(() => window.removeEventListener('resize', onResize));

        const isSuper = computed(() => !!store.user && store.user.is_superuser);
        // 按权限决定普通用户可见的菜单（superuser 全可见）。前端隐藏≠鉴权，后端仍校验。
        const access = computed(() => menuAccess(store.user, store.permissions));

        // 修改密码弹窗（复用 PATCH /users/me）
        const pwDialog = reactive({ open: false, pwd: '', confirm: '', loading: false });
        function openChangePassword() {
            pwDialog.pwd = ''; pwDialog.confirm = ''; pwDialog.loading = false; pwDialog.open = true;
        }
        async function submitChangePassword() {
            if ((pwDialog.pwd || '').length < 8) { ElMessage.warning('新密码至少 8 位'); return; }
            if (pwDialog.pwd !== pwDialog.confirm) { ElMessage.warning('两次输入的密码不一致'); return; }
            pwDialog.loading = true;
            try {
                await api.updateMe({ password: pwDialog.pwd });   // 密码只走请求体，不写日志/console
                pwDialog.open = false;
                ElMessage.success('密码已修改，请重新登录');
                await logout();
            } catch (e) {
                ElMessage.error(e.message || '修改失败');
            } finally {
                pwDialog.loading = false;
            }
        }
        function onUserCommand(cmd) {
            if (cmd === 'logout') logout();
            else if (cmd === 'changepw') openChangePassword();
        }
        const currentPath = computed(() => route.path);
        const pageTitle = computed(() => route.meta.title || '');
        const userLabel = computed(() =>
            store.user ? (store.user.username || store.user.email) : ''
        );
        const avatarText = computed(() => (userLabel.value || '?').charAt(0).toUpperCase());

        async function logout() {
            try {
                await api.logout();
            } finally {
                store.user = null;
                store.permissions = [];
                ElMessage.success('已注销');
                router.push('/login');
            }
        }

        function toggleSidebar() { collapsed.value = !collapsed.value; }

        return {
            store, isSuper, access, collapsed, isNarrow, effectiveCollapsed,
            currentPath, pageTitle, userLabel, avatarText,
            logout,
            pwDialog, openChangePassword, submitChangePassword, onUserCommand,
            toggleSidebar,
        };
    },
    template: `
    <el-container>
        <el-aside :width="effectiveCollapsed ? '64px' : '224px'" class="layout-aside" :class="{ 'is-collapsed': effectiveCollapsed }">
            <div class="logo">
                <img class="app-brand-mark" src="./assets/app-brand-mark.png" alt="" />
                <span v-show="!effectiveCollapsed">向量知识库</span>
            </div>
            <el-menu :default-active="currentPath" :collapse="effectiveCollapsed" :collapse-transition="false" router>
                <el-menu-item v-if="access.chat" index="/chat">
                    <el-icon><local-icon icon="sidebar:chat"></local-icon></el-icon>
                    <template #title>智能问答</template>
                </el-menu-item>
                <el-menu-item v-if="access.documents" index="/documents">
                    <el-icon><local-icon icon="sidebar:document"></local-icon></el-icon>
                    <template #title>文档</template>
                </el-menu-item>
                <el-menu-item v-if="access.catalog" index="/catalog">
                    <el-icon><local-icon icon="mdi:bookshelf"></local-icon></el-icon>
                    <template #title>知识目录</template>
                </el-menu-item>
                <el-menu-item v-if="access.search" index="/search">
                    <el-icon><local-icon icon="sidebar:search"></local-icon></el-icon>
                    <template #title>数据检索</template>
                </el-menu-item>
                <el-menu-item v-if="access.import" index="/import">
                    <el-icon><local-icon icon="sidebar:import"></local-icon></el-icon>
                    <template #title>导入数据</template>
                </el-menu-item>
                <el-menu-item index="/api-keys">
                    <el-icon><local-icon icon="sidebar:api-key"></local-icon></el-icon>
                    <template #title>我的 API Key</template>
                </el-menu-item>

                <el-menu-item-group v-if="isSuper" title="管理员">
                    <el-menu-item index="/dashboard">
                        <el-icon><local-icon icon="sidebar:overview"></local-icon></el-icon>
                        <template #title>概览</template>
                    </el-menu-item>
                    <el-menu-item index="/users">
                        <el-icon><local-icon icon="sidebar:user"></local-icon></el-icon>
                        <template #title>用户管理</template>
                    </el-menu-item>
                    <el-menu-item index="/libraries">
                        <el-icon><local-icon icon="sidebar:library"></local-icon></el-icon>
                        <template #title>库管理</template>
                    </el-menu-item>
                    <el-menu-item index="/permissions">
                        <el-icon><local-icon icon="sidebar:permission"></local-icon></el-icon>
                        <template #title>权限矩阵</template>
                    </el-menu-item>
                    <el-menu-item index="/jobs">
                        <el-icon><local-icon icon="sidebar:task"></local-icon></el-icon>
                        <template #title>任务监控</template>
                    </el-menu-item>
                    <el-menu-item index="/operations">
                        <el-icon><local-icon icon="sidebar:runtime"></local-icon></el-icon>
                        <template #title>运行状态</template>
                    </el-menu-item>
                    <el-menu-item index="/audit">
                        <el-icon><local-icon icon="sidebar:audit"></local-icon></el-icon>
                        <template #title>审计日志</template>
                    </el-menu-item>
                    <el-menu-item index="/chat-logs">
                        <el-icon><local-icon icon="sidebar:qa-log"></local-icon></el-icon>
                        <template #title>问答日志</template>
                    </el-menu-item>
                </el-menu-item-group>
            </el-menu>
            <div class="sidebar-collapse-btn">
                <el-button :icon="effectiveCollapsed ? 'mdi:chevron-right' : 'mdi:chevron-left'"
                           :aria-label="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                           :title="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                           text @click="toggleSidebar" @keydown.enter="toggleSidebar" />
            </div>
        </el-aside>

        <el-container>
            <el-header class="layout-header">
                <div class="header-left">
                    <el-button class="header-icon-btn"
                               :aria-label="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                               :title="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                               text @click="toggleSidebar" @keydown.enter="toggleSidebar">
                        <local-icon :icon="effectiveCollapsed ? 'mdi:menu' : 'mdi:backburger'"></local-icon>
                    </el-button>
                    <div class="header-breadcrumb">
                        <span class="header-title">{{ pageTitle }}</span>
                        <span class="header-breadcrumb-separator">/</span>
                        <span class="header-product-name">向量知识库</span>
                    </div>
                </div>
                <div class="header-right">
                    <el-dropdown @command="onUserCommand">
                        <span class="header-user">
                            <span class="header-avatar">{{ avatarText }}</span>
                            <span>{{ userLabel }}</span>
                            <el-tag v-if="isSuper" type="danger" size="small" effect="light">超管</el-tag>
                            <local-icon icon="mdi:chevron-down" style="color:var(--el-text-color-placeholder)"></local-icon>
                        </span>
                        <template #dropdown>
                            <el-dropdown-menu>
                                <el-dropdown-item command="changepw">
                                    <local-icon icon="mdi:lock-reset" style="margin-right:6px"></local-icon>修改密码
                                </el-dropdown-item>
                                <el-dropdown-item command="logout" divided>
                                    <local-icon icon="mdi:logout" style="margin-right:6px"></local-icon>退出登录
                                </el-dropdown-item>
                            </el-dropdown-menu>
                        </template>
                    </el-dropdown>
                </div>
            </el-header>
            <el-main class="layout-main" :class="{ 'layout-main--chat': currentPath === '/chat' }">
                <router-view />
            </el-main>
        </el-container>

        <el-dialog v-model="pwDialog.open" title="修改密码" width="420px">
            <el-form label-position="top" @submit.prevent="submitChangePassword">
                <el-form-item label="新密码（至少 8 位）">
                    <el-input v-model="pwDialog.pwd" type="password" show-password autocomplete="new-password"
                              placeholder="请输入新密码" />
                </el-form-item>
                <el-form-item label="确认新密码">
                    <el-input v-model="pwDialog.confirm" type="password" show-password autocomplete="new-password"
                              placeholder="再次输入新密码" @keyup.enter="submitChangePassword" />
                </el-form-item>
                <div style="font-size:12px;color:var(--el-text-color-secondary)">修改成功后将自动退出，请用新密码重新登录。</div>
            </el-form>
            <template #footer>
                <el-button @click="pwDialog.open = false">取消</el-button>
                <el-button type="primary" :loading="pwDialog.loading" @click="submitChangePassword">确认修改</el-button>
            </template>
        </el-dialog>
    </el-container>
    `,
};
