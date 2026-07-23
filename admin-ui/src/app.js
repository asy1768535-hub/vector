// 入口：装配 Vue + Element Plus + Vue Router，并启动 app。
import { createApp } from 'vue';
import { createRouter, createWebHashHistory } from 'vue-router';
import ElementPlus from 'element-plus';
import { ElMessage } from 'element-plus';
import zhCn from 'element-plus/locale/zh-cn';

import { store, refreshAuth } from './store.js';
import { setUnauthorizedHandler } from './api.js';
import {
    canAccessEffectiveRoute,
    canAccessLibraryManagementRoute,
    canAccessOrganizationRoute,
    canAccessRoute,
    menuAccess,
} from './menu_access.js';

function defaultRoute(user, permissions, organizations = []) {
    if (!user) return '/login';
    if (user.is_superuser) return '/dashboard';
    const access = menuAccess(user, permissions, organizations);
    if (access.chat || access.documents || access.search) return '/chat';
    if (access.import) return '/import';
    if (access.classificationReview) return '/classification-review';
    if (access.retrievalTest) return '/retrieval-test';
    return '/api-keys';
}
import './preview_mode.js';
import './icons.js';   // 注册 <local-icon> 自定义元素（本地 SVG，不访问公网）

import Login from './views/Login.js';
import Layout from './views/Layout.js';
import Dashboard from './views/Dashboard.js';
import Users from './views/Users.js';
import Libraries from './views/Libraries.js';
import Permissions from './views/Permissions.js';
import Documents from './views/Documents.js';
import KnowledgeCatalog from './views/KnowledgeCatalog.js';
import GraphGovernance from './views/GraphGovernance.js';
import SchemaLifecycle from './views/SchemaLifecycle.js';
import RetrievalTest from './views/RetrievalTest.js';
import ClassificationReview from './views/ClassificationReview.js';
import Search from './views/Search.js';
import Chat from './views/Chat.js';
import ChatLogs from './views/ChatLogs.js';
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
            { path: 'knowledge-graph', component: GraphGovernance, meta: { title: '知识图谱', perm: 'read', effectivePerm: 'read' } },
            { path: 'schema-lifecycle', component: SchemaLifecycle, meta: { title: 'Schema 管理', libraryManagement: true } },
            { path: '', redirect: (to) => defaultRoute(store.user, store.permissions, store.organizations) },
            { path: 'dashboard', component: Dashboard, meta: { title: '概览', admin: true } },
            { path: 'users', component: Users, meta: { title: '用户管理', admin: true } },
            { path: 'libraries', component: Libraries, meta: { title: '库管理', admin: true } },
            { path: 'permissions', component: Permissions, meta: { title: '权限矩阵', admin: true } },
            { path: 'documents', component: Documents, meta: { title: '文档', perm: 'read' } },
            { path: 'catalog', component: KnowledgeCatalog, meta: { title: '知识目录', perm: 'read', effectivePerm: 'read' } },
            { path: 'retrieval-test', component: RetrievalTest, meta: { title: '检索诊断', organizationAdmin: true } },
            { path: 'classification-review', component: ClassificationReview, meta: { title: '分类审核', libraryManagement: true } },
            { path: 'search', component: Search, meta: { title: '数据检索', perm: 'read' } },
            { path: 'chat', component: Chat, meta: { title: '智能问答', perm: 'read' } },
            { path: 'chat-logs', component: ChatLogs, meta: { title: '问答日志', admin: true } },
            { path: 'import', component: Import, meta: { title: '导入数据', perm: 'insert' } },
            { path: 'api-keys', component: ApiKeys, meta: { title: 'API Key' } },
            { path: 'jobs', component: Jobs, meta: { title: '任务监控', admin: true } },
            { path: 'operations', component: RuntimeStatus, meta: { title: '运行状态', admin: true } },
            { path: 'audit', component: Audit, meta: { title: '审计日志', admin: true } },
        ],
    },
    { path: '/:catchAll(.*)', redirect: (to) => defaultRoute(store.user, store.permissions, store.organizations) },
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
        if (store.user) return { path: defaultRoute(store.user, store.permissions, store.organizations) };
        return true;
    }
    if (!store.user) {
        return { path: '/login', query: { redirect: to.fullPath } };
    }
    if (to.meta.admin && !store.user.is_superuser) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute(store.user, store.permissions, store.organizations) };
    }
    if (to.meta.organizationAdmin && !canAccessOrganizationRoute(store.organizations)) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute(store.user, store.permissions, store.organizations) };
    }
    if (to.meta.libraryManagement && !canAccessLibraryManagementRoute(
        store.permissions,
        store.organizations,
    )) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute(store.user, store.permissions, store.organizations) };
    }
    if (to.meta.effectivePerm && !canAccessEffectiveRoute(store.permissions, to.meta.effectivePerm)) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute(store.user, store.permissions, store.organizations) };
    }
    // 直接输入无权限页面 URL → 跳首页并中文提示（前端隐藏不替代后端鉴权）
    if (to.meta.perm && !canAccessRoute(store.user, store.permissions, to.meta.perm)) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute(store.user, store.permissions, store.organizations) };
    }
    return true;
});

// 多个请求同时 401 时只跳转一次，避免连续触发路由替换 / 多次提示。
let lastUnauthorizedAt = 0;
setUnauthorizedHandler(() => {
    store.user = null;
    store.permissions = [];
    store.organizations = [];
    const now = Date.now();
    if (now - lastUnauthorizedAt < 3000) return;   // 3s 抖动窗口内只处理一次
    lastUnauthorizedAt = now;
    if (router.currentRoute.value.path !== '/login') {
        router.replace({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } });
    }
});

const app = createApp({ template: '<router-view />' });
// 让 Vue 把 <iconify-icon> 当原生自定义元素，不去解析成组件
app.config.compilerOptions.isCustomElement = (tag) => tag === 'local-icon';
app.use(ElementPlus, { locale: zhCn });
app.use(router);
app.mount('#app');
