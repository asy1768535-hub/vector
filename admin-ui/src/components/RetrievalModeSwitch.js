import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { APP_PATHS, retrievalModeTarget } from '../domain_navigation.js';
import { menuAccess } from '../menu_access.js';
import { store } from '../store.js';

export default {
    props: {
        queryText: { type: String, default: '' },
        librarySlugs: { type: Array, default: () => [] },
    },
    setup(props) {
        const route = useRoute();
        const router = useRouter();
        const access = computed(() => menuAccess(store.user, store.permissions, store.organizations));
        const views = computed(() => [
            access.value.search ? { value: APP_PATHS.search, label: '单库检索' } : null,
            access.value.retrievalTest ? { value: APP_PATHS.retrievalTest, label: '多库检索' } : null,
        ].filter(Boolean));

        function switchMode(path) {
            if (!path || path === route.path) return;
            router.push(retrievalModeTarget(path, route.query, {
                queryText: props.queryText,
                librarySlugs: props.librarySlugs,
            }));
        }

        return { activeMode: computed(() => route.path), switchMode, views };
    },
    template: `
      <nav v-if="views.length" class="retrieval-mode-switch" aria-label="检索方式">
        <el-radio-group :model-value="activeMode" size="small" @change="switchMode">
          <el-radio-button v-for="view in views" :key="view.value" :value="view.value">
            {{ view.label }}
          </el-radio-button>
        </el-radio-group>
      </nav>
    `,
};
