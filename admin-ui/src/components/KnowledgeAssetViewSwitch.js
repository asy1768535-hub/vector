import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { APP_PATHS, domainTabTarget } from '../domain_navigation.js';
import { menuAccess } from '../menu_access.js';
import { store } from '../store.js';

export default {
    props: {
        library: { type: String, default: '' },
    },
    setup(props) {
        const route = useRoute();
        const router = useRouter();
        const access = computed(() => menuAccess(
            store.user,
            store.permissions,
            store.organizations,
        ));
        const views = computed(() => [
            access.value.documents
                ? { value: APP_PATHS.documents, label: '文档' }
                : null,
            access.value.catalog
                ? { value: APP_PATHS.catalog, label: '知识目录' }
                : null,
        ].filter(Boolean));
        const activeView = computed(() => (
            route.path === APP_PATHS.catalog ? APP_PATHS.catalog : APP_PATHS.documents
        ));

        function switchView(path) {
            if (!path || path === route.path) return;
            router.push(domainTabTarget(path, {
                ...route.query,
                library: props.library || route.query.library || route.query.slug,
            }));
        }

        return { activeView, switchView, views };
    },
    template: `
      <nav v-if="views.length > 1" class="knowledge-asset-view-switch"
           aria-label="知识内容视图">
        <el-radio-group :model-value="activeView" size="small" @change="switchView">
          <el-radio-button v-for="view in views" :key="view.value" :value="view.value">
            {{ view.label }}
          </el-radio-button>
        </el-radio-group>
      </nav>
    `,
};
