import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { store } from '../store.js';
import { isDark, toggleDark } from '../theme.js';
import * as api from '../api.js';

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const collapsed = ref(false);
        const isSuper = computed(() => !!store.user && store.user.is_superuser);
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

        return {
            store, isSuper, collapsed, currentPath, pageTitle, userLabel, avatarText,
            isDark, toggleDark, logout,
        };
    },
    template: `
    <el-container>
        <el-aside :width="collapsed ? '64px' : '220px'" class="layout-aside" :class="{ 'is-collapsed': collapsed }">
            <div class="logo">
                <iconify-icon icon="carbon:chart-relationship"></iconify-icon>
                <span v-show="!collapsed">向量知识库</span>
            </div>
            <el-menu :default-active="currentPath" :collapse="collapsed" :collapse-transition="false" router>
                <el-menu-item index="/dashboard">
                    <el-icon><iconify-icon icon="mdi:view-dashboard-outline"></iconify-icon></el-icon>
                    <template #title>概览</template>
                </el-menu-item>
                <el-menu-item index="/documents">
                    <el-icon><iconify-icon icon="mdi:file-document-outline"></iconify-icon></el-icon>
                    <template #title>文档</template>
                </el-menu-item>
                <el-menu-item index="/search">
                    <el-icon><iconify-icon icon="mdi:text-search"></iconify-icon></el-icon>
                    <template #title>数据检索</template>
                </el-menu-item>
                <el-menu-item index="/chat">
                    <el-icon><iconify-icon icon="mdi:chat-question-outline"></iconify-icon></el-icon>
                    <template #title>智能问答</template>
                </el-menu-item>
                <el-menu-item index="/import">
                    <el-icon><iconify-icon icon="mdi:database-import-outline"></iconify-icon></el-icon>
                    <template #title>导入数据</template>
                </el-menu-item>
                <el-menu-item index="/api-keys">
                    <el-icon><iconify-icon icon="mdi:key-variant"></iconify-icon></el-icon>
                    <template #title>我的 API Key</template>
                </el-menu-item>

                <el-menu-item-group v-if="isSuper" title="管理员">
                    <el-menu-item index="/users">
                        <el-icon><iconify-icon icon="mdi:account-group-outline"></iconify-icon></el-icon>
                        <template #title>用户管理</template>
                    </el-menu-item>
                    <el-menu-item index="/libraries">
                        <el-icon><iconify-icon icon="mdi:bookshelf"></iconify-icon></el-icon>
                        <template #title>库管理</template>
                    </el-menu-item>
                    <el-menu-item index="/permissions">
                        <el-icon><iconify-icon icon="mdi:shield-key-outline"></iconify-icon></el-icon>
                        <template #title>权限矩阵</template>
                    </el-menu-item>
                    <el-menu-item index="/jobs">
                        <el-icon><iconify-icon icon="mdi:cog-sync-outline"></iconify-icon></el-icon>
                        <template #title>任务监控</template>
                    </el-menu-item>
                    <el-menu-item index="/operations">
                        <el-icon><iconify-icon icon="mdi:heart-pulse"></iconify-icon></el-icon>
                        <template #title>运行状态</template>
                    </el-menu-item>
                    <el-menu-item index="/audit">
                        <el-icon><iconify-icon icon="mdi:history"></iconify-icon></el-icon>
                        <template #title>审计日志</template>
                    </el-menu-item>
                    <el-menu-item index="/chat-logs">
                        <el-icon><iconify-icon icon="mdi:comment-text-multiple-outline"></iconify-icon></el-icon>
                        <template #title>问答日志</template>
                    </el-menu-item>
                </el-menu-item-group>
            </el-menu>
        </el-aside>

        <el-container>
            <el-header class="layout-header">
                <div class="header-left">
                    <span class="header-icon-btn" @click="collapsed = !collapsed">
                        <iconify-icon :icon="collapsed ? 'mdi:menu' : 'mdi:backburger'"></iconify-icon>
                    </span>
                    <span class="header-title">{{ pageTitle }}</span>
                </div>
                <div class="header-right">
                    <span class="header-icon-btn" @click="toggleDark" :title="isDark ? '切换亮色' : '切换暗色'">
                        <iconify-icon :icon="isDark ? 'mdi:weather-night' : 'mdi:white-balance-sunny'"></iconify-icon>
                    </span>
                    <el-dropdown @command="cmd => cmd === 'logout' && logout()">
                        <span class="header-user">
                            <span class="header-avatar">{{ avatarText }}</span>
                            <span>{{ userLabel }}</span>
                            <el-tag v-if="isSuper" type="danger" size="small" effect="light">超管</el-tag>
                            <iconify-icon icon="mdi:chevron-down" style="color:var(--el-text-color-placeholder)"></iconify-icon>
                        </span>
                        <template #dropdown>
                            <el-dropdown-menu>
                                <el-dropdown-item command="logout">
                                    <iconify-icon icon="mdi:logout" style="margin-right:6px"></iconify-icon>退出登录
                                </el-dropdown-item>
                            </el-dropdown-menu>
                        </template>
                    </el-dropdown>
                </div>
            </el-header>
            <el-main class="layout-main">
                <router-view />
            </el-main>
        </el-container>
    </el-container>
    `,
};
