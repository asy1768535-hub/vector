import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';

import * as api from '../api.js';
import { domainTabTarget, visibleSidebarGroups } from '../domain_navigation.js';
import { menuAccess } from '../menu_access.js';
import { clearAuthState, store } from '../store.js';

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

        const isSuper = computed(() => !!store.user?.is_superuser);
        const access = computed(() => menuAccess(
            store.user,
            store.permissions,
            store.organizations,
        ));
        const sidebarGroups = computed(() => visibleSidebarGroups(access.value));
        const sidebarEntries = computed(() => sidebarGroups.value.flatMap((group) => (
            group.items.map((item, index) => ({
                ...item,
                type: 'item',
                key: item.path,
                section: group.section,
                sectionStart: Boolean(group.sectionStart && index === 0),
            }))
        )));
        const activePath = computed(() => (
            sidebarEntries.value.find((entry) => (
                entry.type === 'item'
                && Array.isArray(entry.activePaths)
                && entry.activePaths.includes(route.path)
            ))?.path || route.path
        ));
        const sidebarMenuKey = computed(() => [
            effectiveCollapsed.value ? 'collapsed' : 'expanded',
            sidebarEntries.value.map((entry) => entry.key).join('|'),
        ].join(':'));
        const pageTitle = computed(() => route.meta.domainTitle || route.meta.title || '');
        const pageSection = computed(() => (
            route.meta.domainTitle ? route.meta.title : '向量知识库'
        ));
        const isChatWorkspace = computed(() => route.meta.workspace === 'chat');
        const userLabel = computed(() => (
            store.user ? (store.user.display_name || store.user.username || store.user.email) : ''
        ));
        const avatarText = computed(() => (userLabel.value || '?').charAt(0).toUpperCase());

        function navigatePage(path) {
            if (!path || path === route.path) return;
            const item = sidebarEntries.value
                .filter((entry) => entry.type === 'item')
                .find((entry) => entry.path === path);
            if (item) router.push(domainTabTarget(item.path, route.query));
        }

        function onUserCommand(command) {
            if (command === 'account') router.push('/account/profile');
            else if (command === 'logout') logout();
        }

        async function logout() {
            try {
                await api.logout();
            } finally {
                clearAuthState();
                ElMessage.success('已注销');
                router.push('/login');
            }
        }

        function toggleSidebar() { collapsed.value = !collapsed.value; }

        return {
            activePath,
            avatarText,
            collapsed,
            effectiveCollapsed,
            isChatWorkspace,
            isNarrow,
            isSuper,
            navigatePage,
            onUserCommand,
            pageSection,
            pageTitle,
            sidebarEntries,
            sidebarGroups,
            sidebarMenuKey,
            toggleSidebar,
            userLabel,
        };
    },
    template: `
    <el-container>
        <el-aside
            :width="effectiveCollapsed ? '64px' : '220px'"
            class="layout-aside"
            :class="{ 'is-collapsed': effectiveCollapsed }"
        >
            <div class="logo">
                <img class="app-brand-mark" src="./assets/app-brand-mark.png" alt="" />
                <span v-show="!effectiveCollapsed">向量知识库</span>
            </div>
            <el-menu
                :key="sidebarMenuKey"
                class="sidebar-page-menu"
                :default-active="activePath"
                :collapse="effectiveCollapsed"
                :collapse-transition="false"
                @select="navigatePage"
            >
                <template
                    v-for="entry in sidebarEntries"
                    :key="entry.key"
                >
                    <el-menu-item
                        v-if="entry.type === 'item'"
                        :index="entry.path"
                        class="sidebar-flat-item"
                        :class="{ 'is-section-start': entry.sectionStart }"
                    >
                        <el-icon><local-icon :icon="entry.icon"></local-icon></el-icon>
                        <template #title>{{ entry.label }}</template>
                    </el-menu-item>
                </template>
            </el-menu>
            <div class="sidebar-collapse-btn">
                <el-button
                    :icon="effectiveCollapsed ? 'mdi:chevron-right' : 'mdi:chevron-left'"
                    :aria-label="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                    :title="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                    text
                    @click="toggleSidebar"
                    @keydown.enter="toggleSidebar"
                />
            </div>
        </el-aside>

        <el-container>
            <el-header class="layout-header">
                <div class="header-left">
                    <el-button
                        class="header-icon-btn"
                        :aria-label="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                        :title="effectiveCollapsed ? '展开侧栏' : '收起侧栏'"
                        text
                        @click="toggleSidebar"
                        @keydown.enter="toggleSidebar"
                    >
                        <local-icon
                            :icon="effectiveCollapsed ? 'mdi:menu' : 'mdi:backburger'"
                        ></local-icon>
                    </el-button>
                    <div class="header-breadcrumb">
                        <span class="header-title">{{ pageTitle }}</span>
                        <span class="header-breadcrumb-separator">/</span>
                        <span class="header-product-name">{{ pageSection }}</span>
                    </div>
                </div>
                <div class="header-right">
                    <el-dropdown @command="onUserCommand">
                        <span class="header-user">
                            <span class="header-avatar">{{ avatarText }}</span>
                            <span class="header-user-label">{{ userLabel }}</span>
                            <el-tag v-if="isSuper" type="danger" size="small" effect="light">
                                超管
                            </el-tag>
                            <local-icon
                                icon="mdi:chevron-down"
                                style="color:var(--el-text-color-placeholder)"
                            ></local-icon>
                        </span>
                        <template #dropdown>
                            <el-dropdown-menu>
                                <el-dropdown-item command="account">
                                    <local-icon
                                        icon="sidebar:user"
                                        style="margin-right:6px"
                                    ></local-icon>
                                    账户设置
                                </el-dropdown-item>
                                <el-dropdown-item command="logout" divided>
                                    <local-icon
                                        icon="mdi:logout"
                                        style="margin-right:6px"
                                    ></local-icon>
                                    退出登录
                                </el-dropdown-item>
                            </el-dropdown-menu>
                        </template>
                    </el-dropdown>
                </div>
            </el-header>
            <el-main
                class="layout-main"
                :class="{ 'layout-main--chat': isChatWorkspace }"
            >
                <router-view />
            </el-main>
        </el-container>
    </el-container>
    `,
};
