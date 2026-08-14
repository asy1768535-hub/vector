import { reactive, ref } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { refreshAuth, store, setMockUser } from '../store.js';

export default {
    setup() {
        const router = useRouter();
        const route = useRoute();
        const form = reactive({ email: '', password: '' });
        const loading = ref(false);
        const highlights = [
            {
                icon: 'mdi:chat-question-outline',
                title: '智能问答与知识调用',
                desc: '围绕内部制度、业务知识和沉淀文档提供统一问答入口。',
            },
            {
                icon: 'mdi:file-document-outline',
                title: '文档集中治理',
                desc: '统一管理知识文档、版本变更和导入后的内容状态。',
            },
            {
                icon: 'mdi:text-search',
                title: '检索与沉淀并行',
                desc: '结合语义检索与知识沉淀，提升信息复用效率。',
            },
        ];

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
                const redirect = route.query.redirect || '/';
                router.replace(redirect);
            } catch (e) {
                const msg = e.message || '';
                if (typeof window !== 'undefined' && window.__DEV_PREVIEW__
                    && (e instanceof TypeError
                        || /Failed to fetch|NetworkError/i.test(msg)
                        || /HTTP 50[0-9]/.test(msg)
                        || /HTTP 405/.test(msg))) {
                    setMockUser();
                    ElMessage.success('已进入本地预览模式（超管）');
                    const redirect = route.query.redirect || '/';
                    router.replace(redirect);
                    loading.value = false;
                    return;
                }
                ElMessage.error(msg || '登录失败');
            } finally {
                loading.value = false;
            }
        }

        return { form, highlights, loading, submit };
    },
    template: `
    <div class="login-shell">
        <div class="login-bg-layer"></div>
        <div class="login-bg-wash"></div>
        <div class="login-content">
            <div class="login-corner-brand">
                <img class="company-logo company-logo--corner" src="./assets/company-logo.png" alt="公司标识" />
            </div>
            <section class="login-hero">
                <div class="login-hero-copy">
                    <div class="login-brand-text">
                        <h1 class="login-hero-title">向量知识库</h1>
                        <p class="login-hero-sub">
                            面向企业内部知识管理与智能问答的统一工作台，让知识沉淀、检索与问答协同发生。
                        </p>
                    </div>
                </div>

                <div class="login-hero-features">
                    <div v-for="item in highlights" :key="item.title" class="login-hero-feature">
                        <div class="login-hero-icon">
                            <local-icon :icon="item.icon"></local-icon>
                        </div>
                        <div class="login-hero-feature-text">
                            <div class="login-hero-feature-title">{{ item.title }}</div>
                            <div class="login-hero-feature-desc">{{ item.desc }}</div>
                        </div>
                    </div>
                </div>
            </section>

            <section class="login-panel">
                <div class="login-card">
                    <div class="login-card-head">
                        <h2 class="login-title">登录账号</h2>
                        <p class="login-sub">使用企业邮箱与密码进入管理后台</p>
                    </div>

                    <el-form @submit.prevent="submit" label-position="left" label-width="64px" size="large" class="login-form-row-label">
                        <el-form-item label="邮箱">
                            <el-input v-model="form.email" autocomplete="username" placeholder="请输入企业邮箱">
                                <template #prefix><local-icon icon="mdi:email-outline"></local-icon></template>
                            </el-input>
                        </el-form-item>
                        <el-form-item label="密码">
                            <el-input
                                v-model="form.password"
                                type="password"
                                autocomplete="current-password"
                                show-password
                                placeholder="请输入密码"
                                @keyup.enter="submit"
                            >
                                <template #prefix><local-icon icon="mdi:lock-outline"></local-icon></template>
                            </el-input>
                        </el-form-item>
                        <el-button type="primary" class="login-btn" :loading="loading" @click="submit">登 录</el-button>
                    </el-form>
                </div>
            </section>
        </div>
    </div>
    `,
};
