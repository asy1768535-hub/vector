import { computed, reactive, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';

import * as api from '../api.js';
import { clearAuthState, refreshAuth, store } from '../store.js';

export default {
    setup() {
        const router = useRouter();
        const profile = reactive({
            username: '',
            displayName: '',
            saving: false,
        });
        const password = reactive({
            value: '',
            confirm: '',
            saving: false,
        });

        const organizations = computed(() => store.organizations || []);
        const permissionRows = computed(() => store.permissions || []);
        const permissionActions = computed(() => Array.from(new Set(
            permissionRows.value.flatMap((item) => item.actions || []),
        )));

        function syncProfile(user) {
            profile.username = user?.username || '';
            profile.displayName = user?.display_name || '';
        }

        watch(() => store.user, syncProfile, { immediate: true });

        async function saveProfile() {
            const username = profile.username.trim();
            const displayName = profile.displayName.trim();
            if (username.length > 64 || displayName.length > 100) {
                ElMessage.warning('用户名或显示名称过长');
                return;
            }
            profile.saving = true;
            try {
                await api.updateMe({
                    username: username || null,
                    display_name: displayName || null,
                });
                await refreshAuth();
                ElMessage.success('个人资料已更新');
            } catch (error) {
                ElMessage.error(error.message || '个人资料更新失败');
            } finally {
                profile.saving = false;
            }
        }

        async function changePassword() {
            if (password.value.length < 8) {
                ElMessage.warning('新密码至少 8 位');
                return;
            }
            if (password.value !== password.confirm) {
                ElMessage.warning('两次输入的密码不一致');
                return;
            }
            password.saving = true;
            try {
                await api.updateMe({ password: password.value });
                try {
                    await api.logout();
                } finally {
                    clearAuthState();
                    await router.replace('/login');
                }
                ElMessage.success('密码已修改，请重新登录');
            } catch (error) {
                ElMessage.error(error.message || '密码修改失败');
            } finally {
                password.value = '';
                password.confirm = '';
                password.saving = false;
            }
        }

        return {
            organizations,
            password,
            permissionActions,
            permissionRows,
            profile,
            saveProfile,
            changePassword,
            store,
        };
    },
    template: `
    <div class="account-profile">
        <section class="account-profile-section">
            <div class="account-profile-heading">
                <h2>个人资料</h2>
            </div>
            <el-form class="account-profile-form" label-position="top" @submit.prevent="saveProfile">
                <el-form-item label="邮箱">
                    <el-input :model-value="store.user?.email || ''" disabled />
                </el-form-item>
                <el-form-item label="用户名">
                    <el-input v-model="profile.username" maxlength="64" />
                </el-form-item>
                <el-form-item label="显示名称">
                    <el-input v-model="profile.displayName" maxlength="100" />
                </el-form-item>
                <el-button type="primary" :loading="profile.saving" @click="saveProfile">
                    保存资料
                </el-button>
            </el-form>
        </section>

        <section class="account-profile-section">
            <div class="account-profile-heading">
                <h2>访问范围</h2>
            </div>
            <dl class="account-profile-summary">
                <div>
                    <dt>组织</dt>
                    <dd>{{ organizations.length }}</dd>
                </div>
                <div>
                    <dt>知识库</dt>
                    <dd>{{ permissionRows.length }}</dd>
                </div>
                <div>
                    <dt>权限</dt>
                    <dd>{{ permissionActions.join('、') || '无' }}</dd>
                </div>
            </dl>
        </section>

        <section class="account-profile-section">
            <div class="account-profile-heading">
                <h2>修改密码</h2>
            </div>
            <el-form class="account-profile-form" label-position="top" @submit.prevent="changePassword">
                <el-form-item label="新密码">
                    <el-input
                        v-model="password.value"
                        type="password"
                        show-password
                        autocomplete="new-password"
                    />
                </el-form-item>
                <el-form-item label="确认新密码">
                    <el-input
                        v-model="password.confirm"
                        type="password"
                        show-password
                        autocomplete="new-password"
                        @keyup.enter="changePassword"
                    />
                </el-form-item>
                <el-button type="primary" :loading="password.saving" @click="changePassword">
                    修改密码
                </el-button>
            </el-form>
        </section>
    </div>
    `,
};
