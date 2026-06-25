// 入口：装配 Vue + Element Plus + Vue Router，并启动 app。
import { createApp } from 'vue';
import { createRouter, createWebHashHistory } from 'vue-router';
import ElementPlus from 'element-plus';
import zhCn from 'element-plus/locale/zh-cn';

import { store, refreshAuth } from './store.js';
import { setUnauthorizedHandler } from './api.js';
import './theme.js';  // 启动即应用暗色偏好

import Login from './views/Login.js';
import Layout from './views/Layout.js';
import Dashboard from './views/Dashboard.js';
import Users from './views/Users.js';
import Libraries from './views/Libraries.js';
import Permissions from './views/Permissions.js';
import Documents from './views/Documents.js';
import Search from './views/Search.js';
import Import from './views/Import.js';
import ApiKeys from './views/ApiKeys.js';
import Jobs from './views/Jobs.js';
import RuntimeStatus from './views/RuntimeStatus.js';
import Audit from './views/Audit.js';

const routes = [
    { path: '/login', component: Login, meta: { guest: true } },
    {
        path: '/',
        component: Layout,
        children: [
            { path: '', redirect: '/dashboard' },
            { path: 'dashboard', component: Dashboard, meta: { title: '概览' } },
            { path: 'users', component: Users, meta: { title: '用户管理', admin: true } },
            { path: 'libraries', component: Libraries, meta: { title: '库管理', admin: true } },
            { path: 'permissions', component: Permissions, meta: { title: '权限矩阵', admin: true } },
            { path: 'documents', component: Documents, meta: { title: '文档' } },
            { path: 'search', component: Search, meta: { title: '数据检索' } },
            { path: 'import', component: Import, meta: { title: '导入数据' } },
            { path: 'api-keys', component: ApiKeys, meta: { title: 'API Key' } },
            { path: 'jobs', component: Jobs, meta: { title: '任务监控', admin: true } },
            { path: 'operations', component: RuntimeStatus, meta: { title: '运行状态', admin: true } },
            { path: 'audit', component: Audit, meta: { title: '审计日志', admin: true } },
        ],
    },
    { path: '/:catchAll(.*)', redirect: '/dashboard' },
];

const router = createRouter({
    history: createWebHashHistory(),
    routes,
});

router.beforeEach(async (to) => {
    if (!store.ready) {
        await refreshAuth();
    }
    if (to.meta.guest) {
        if (store.user) return { path: '/dashboard' };
        return true;
    }
    if (!store.user) {
        return { path: '/login', query: { redirect: to.fullPath } };
    }
    if (to.meta.admin && !store.user.is_superuser) {
        return { path: '/dashboard' };
    }
    return true;
});

setUnauthorizedHandler(() => {
    store.user = null;
    store.permissions = [];
    if (router.currentRoute.value.path !== '/login') {
        router.replace({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } });
    }
});

const app = createApp({ template: '<router-view />' });
// 让 Vue 把 <iconify-icon> 当原生自定义元素，不去解析成组件
app.config.compilerOptions.isCustomElement = (tag) => tag === 'iconify-icon';
app.use(ElementPlus, { locale: zhCn });
app.use(router);
app.mount('#app');
