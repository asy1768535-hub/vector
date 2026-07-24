import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { domainTabs, domainTabTarget } from '../domain_navigation.js';
import { menuAccess } from '../menu_access.js';
import { store } from '../store.js';

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const access = computed(() => menuAccess(
            store.user,
            store.permissions,
            store.organizations,
        ));
        const tabs = computed(() => domainTabs(route.meta.domain, access.value));
        const activeTab = computed(() => route.path);
        const isChat = computed(() => route.meta.workspace === 'chat');

        function changeTab(pane) {
            const path = String(pane?.props?.name || '');
            if (!path || path === route.path) return;
            const tab = tabs.value.find((item) => item.path === path);
            if (!tab) return;
            router.push(domainTabTarget(tab.path, route.query));
        }

        return { activeTab, changeTab, isChat, tabs };
    },
    template: `
    <section class="domain-workspace" :class="{ 'domain-workspace--chat': isChat }">
        <div class="domain-workspace-tabs">
            <el-tabs :model-value="activeTab" @tab-click="changeTab">
                <el-tab-pane
                    v-for="tab in tabs"
                    :key="tab.path"
                    :label="tab.label"
                    :name="tab.path"
                />
            </el-tabs>
        </div>
        <div class="domain-workspace-body">
            <router-view />
        </div>
    </section>
    `,
};
