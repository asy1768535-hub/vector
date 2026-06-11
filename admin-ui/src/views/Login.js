import { reactive, ref } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { refreshAuth, store } from '../store.js';

export default {
    setup() {
        const router = useRouter();
        const route = useRoute();
        const form = reactive({ email: '', password: '' });
        const loading = ref(false);

        async function submit() {
            if (!form.email || !form.password) {
                ElMessage.warning('请输入邮箱和密码');
                return;
            }
            loading.value = true;
            try {
                await api.login(form.email, form.password);
                await refreshAuth();
                if (!store.user) {
                    ElMessage.error('登录态未建立');
                    return;
                }
                ElMessage.success('登录成功');
                const redirect = route.query.redirect || '/dashboard';
                router.replace(redirect);
            } catch (e) {
                ElMessage.error(e.message || '登录失败');
            } finally {
                loading.value = false;
            }
        }

        return { form, loading, submit };
    },
    template: `
    <div class="login-wrap">
        <div class="login-brand">
            <div class="brand-inner">
                <iconify-icon class="brand-logo" icon="carbon:chart-relationship"></iconify-icon>
                <h1>向量知识库</h1>
                <p>多租户向量检索平台<br/>Dify 外部知识库 · 语义检索 · 全文补全</p>
            </div>
        </div>
        <div class="login-form-side">
            <div class="login-card">
                <div class="login-logo-sm"><iconify-icon icon="carbon:chart-relationship"></iconify-icon></div>
                <h2 class="login-title">欢迎登录</h2>
                <p class="login-sub">向量知识库 · 管理后台</p>
                <el-form @submit.prevent="submit" label-position="top" size="large">
                    <el-form-item label="邮箱">
                        <el-input v-model="form.email" autocomplete="username" placeholder="admin@example.com">
                            <template #prefix><iconify-icon icon="mdi:email-outline"></iconify-icon></template>
                        </el-input>
                    </el-form-item>
                    <el-form-item label="密码">
                        <el-input v-model="form.password" type="password" autocomplete="current-password"
                                  show-password placeholder="请输入密码" @keyup.enter="submit">
                            <template #prefix><iconify-icon icon="mdi:lock-outline"></iconify-icon></template>
                        </el-input>
                    </el-form-item>
                    <el-button type="primary" class="login-btn" :loading="loading" @click="submit">登 录</el-button>
                </el-form>
                <div class="login-foot">© 向量知识库 · Vector Knowledge Base</div>
            </div>
        </div>
    </div>
    `,
};
