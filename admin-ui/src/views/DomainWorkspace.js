import { computed } from 'vue';
import { useRoute } from 'vue-router';

export default {
    setup() {
        const route = useRoute();
        const isChat = computed(() => route.meta.workspace === 'chat');
        return { isChat };
    },
    template: `
    <section class="domain-workspace" :class="{ 'domain-workspace--chat': isChat }">
        <router-view />
    </section>
    `,
};
