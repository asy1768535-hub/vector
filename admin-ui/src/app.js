import { createApp } from 'vue';
import { createRouter, createWebHashHistory } from 'vue-router';
import ElementPlus from 'element-plus';
import { ElMessage } from 'element-plus';
import zhCn from 'element-plus/locale/zh-cn';

import { clearAuthState, refreshAuth, store } from './store.js';
import { setUnauthorizedHandler } from './api.js';
import {
    canAccessEffectiveRoute,
    canAccessLibraryManagementRoute,
    canAccessOrganizationRoute,
    canAccessRoute,
    menuAccess,
} from './menu_access.js';
import {
    APP_PATHS,
    LEGACY_REDIRECTS,
    defaultRouteForAccess,
    firstDomainPath,
    legacyRedirectTarget,
} from './domain_navigation.js';

import './preview_mode.js';
import './icons.js';

import Login from './views/Login.js';
import Layout from './views/Layout.js';
import DomainWorkspace from './views/DomainWorkspace.js';
import AccountProfile from './views/AccountProfile.js';
import Dashboard from './views/Dashboard.js';
import Users from './views/Users.js';
import Libraries from './views/Libraries.js';
import Permissions from './views/Permissions.js';
import KnowledgeCatalog from './views/KnowledgeCatalog.js';
import GraphGovernance from './views/GraphGovernance.js';
import SchemaLifecycle from './views/SchemaLifecycle.js';
import RetrievalTest from './views/RetrievalTest.js';
import Search from './views/Search.js';
import Chat from './views/Chat.js';
import ChatLogs from './views/ChatLogs.js';
import Import from './views/Import.js';
import ApiKeys from './views/ApiKeys.js';
import Jobs from './views/Jobs.js';
import RuntimeStatus from './views/RuntimeStatus.js';
import Audit from './views/Audit.js';

function accessSnapshot() {
    return menuAccess(store.user, store.permissions, store.organizations);
}

function defaultRoute() {
    if (!store.user) return APP_PATHS.login;
    return defaultRouteForAccess(accessSnapshot());
}

function domainEntryGuard(rootPath, domain) {
    return (to) => {
        if (to.path !== rootPath) return true;
        return { path: firstDomainPath(domain, accessSnapshot()) || defaultRoute() };
    };
}

function legacyRoute(path) {
    return {
        path: path.slice(1),
        redirect: (to) => legacyRedirectTarget(to.path, to.query, to.hash),
    };
}

const routes = [
    { path: APP_PATHS.login, component: Login, meta: { guest: true } },
    {
        path: '/',
        component: Layout,
        children: [
            { path: '', redirect: () => ({ path: defaultRoute() }) },
            {
                path: 'knowledge-use',
                component: DomainWorkspace,
                meta: { domain: 'knowledgeUse', domainTitle: '知识使用' },
                beforeEnter: domainEntryGuard(APP_PATHS.knowledgeUse, 'knowledgeUse'),
                children: [
                    {
                        path: 'chat',
                        component: Chat,
                        meta: {
                            domain: 'knowledgeUse',
                            domainTitle: '知识使用',
                            title: '智能问答',
                            perm: 'read',
                            workspace: 'chat',
                        },
                    },
                    {
                        path: 'search',
                        component: Search,
                        meta: {
                            domain: 'knowledgeUse',
                            domainTitle: '知识使用',
                            title: '单库检索',
                            perm: 'read',
                        },
                    },
                    {
                        path: 'retrieval-test',
                        component: RetrievalTest,
                        meta: {
                            domain: 'knowledgeUse',
                            domainTitle: '知识使用',
                            title: '多库检索',
                            organizationAdmin: true,
                        },
                    },
                ],
            },
            {
                path: 'knowledge-assets',
                component: DomainWorkspace,
                meta: { domain: 'knowledgeAssets', domainTitle: '知识资产' },
                beforeEnter: domainEntryGuard(APP_PATHS.knowledgeAssets, 'knowledgeAssets'),
                children: [
                    {
                        path: 'documents',
                        redirect: (to) => ({
                            path: APP_PATHS.catalog,
                            query: {
                                ...(to.query.slug ? { library: to.query.slug } : {}),
                                ...(to.query.open ? { document: to.query.open } : {}),
                            },
                        }),
                        meta: {
                            domain: 'knowledgeAssets',
                            domainTitle: '知识资产',
                            title: '文档目录',
                            perm: 'read',
                        },
                    },
                    {
                        path: 'catalog',
                        component: KnowledgeCatalog,
                        meta: {
                            domain: 'knowledgeAssets',
                            domainTitle: '知识资产',
                            title: '知识资产',
                            perm: 'read',
                            effectivePerm: 'read',
                        },
                    },
                    {
                        path: 'import',
                        component: Import,
                        meta: {
                            domain: 'knowledgeAssets',
                            domainTitle: '知识资产',
                            title: '导入与替换',
                            perm: 'insert',
                        },
                    },
                ],
            },
            {
                path: 'knowledge-governance',
                component: DomainWorkspace,
                meta: { domain: 'knowledgeGovernance', domainTitle: '知识治理' },
                beforeEnter: domainEntryGuard(
                    APP_PATHS.knowledgeGovernance,
                    'knowledgeGovernance',
                ),
                children: [
                    {
                        path: 'graph',
                        component: GraphGovernance,
                        meta: {
                            domain: 'knowledgeGovernance',
                            domainTitle: '知识治理',
                            title: '知识图谱',
                            perm: 'read',
                            effectivePerm: 'read',
                        },
                    },
                    {
                        path: 'schema',
                        component: SchemaLifecycle,
                        meta: {
                            domain: 'libraries',
                            domainTitle: '库管理',
                            title: 'Schema 管理',
                            libraryManagement: true,
                        },
                    },
                    {
                        path: 'classification',
                        redirect: (to) => ({
                            path: APP_PATHS.catalog,
                            query: to.query,
                            hash: to.hash,
                        }),
                        meta: {
                            domain: 'knowledgeGovernance',
                            domainTitle: '知识治理',
                            title: '分类审核',
                            libraryManagement: true,
                        },
                    },
                ],
            },
            {
                path: 'users-permissions',
                component: DomainWorkspace,
                meta: {
                    domain: 'usersPermissions',
                    domainTitle: '用户与权限',
                    admin: true,
                },
                beforeEnter: domainEntryGuard(APP_PATHS.usersPermissions, 'usersPermissions'),
                children: [
                    {
                        path: 'users',
                        component: Users,
                        meta: {
                            domain: 'usersPermissions',
                            domainTitle: '用户与权限',
                            title: '用户管理',
                            admin: true,
                        },
                    },
                    {
                        path: 'permissions',
                        component: Permissions,
                        meta: {
                            domain: 'usersPermissions',
                            domainTitle: '用户与权限',
                            title: '权限矩阵',
                            admin: true,
                        },
                    },
                ],
            },
            {
                path: 'libraries',
                component: Libraries,
                meta: {
                    domain: 'libraries',
                    domainTitle: '库管理',
                    title: '库管理',
                    admin: true,
                },
            },
            {
                path: 'operations-center',
                component: DomainWorkspace,
                meta: {
                    domain: 'operationsCenter',
                    domainTitle: '运维中心',
                    admin: true,
                },
                beforeEnter: domainEntryGuard(APP_PATHS.operationsCenter, 'operationsCenter'),
                children: [
                    {
                        path: 'overview',
                        component: Dashboard,
                        meta: {
                            domain: 'operationsCenter',
                            domainTitle: '运维中心',
                            title: '概览',
                            admin: true,
                        },
                    },
                    {
                        path: 'status',
                        component: RuntimeStatus,
                        meta: {
                            domain: 'operationsCenter',
                            domainTitle: '运维中心',
                            title: '服务与队列',
                            admin: true,
                        },
                    },
                    {
                        path: 'jobs',
                        component: Jobs,
                        meta: {
                            domain: 'operationsCenter',
                            domainTitle: '运维中心',
                            title: '任务',
                            admin: true,
                        },
                    },
                ],
            },
            {
                path: 'audit-center',
                component: DomainWorkspace,
                meta: { domain: 'auditCenter', domainTitle: '审计中心', admin: true },
                beforeEnter: domainEntryGuard(APP_PATHS.auditCenter, 'auditCenter'),
                children: [
                    {
                        path: 'operations',
                        component: Audit,
                        meta: {
                            domain: 'auditCenter',
                            domainTitle: '审计中心',
                            title: '操作审计',
                            admin: true,
                        },
                    },
                    {
                        path: 'chat',
                        component: ChatLogs,
                        meta: {
                            domain: 'auditCenter',
                            domainTitle: '审计中心',
                            title: '问答审计',
                            admin: true,
                        },
                    },
                ],
            },
            {
                path: 'account',
                component: DomainWorkspace,
                meta: { domain: 'account', domainTitle: '账户设置' },
                beforeEnter: domainEntryGuard(APP_PATHS.account, 'account'),
                children: [
                    {
                        path: 'profile',
                        component: AccountProfile,
                        meta: {
                            domain: 'account',
                            domainTitle: '账户设置',
                            title: '个人资料',
                        },
                    },
                    {
                        path: 'api-keys',
                        component: ApiKeys,
                        meta: {
                            domain: 'account',
                            domainTitle: '账户设置',
                            title: 'API Key',
                        },
                    },
                ],
            },

            ...Object.keys(LEGACY_REDIRECTS).map(legacyRoute),
        ],
    },
    { path: '/:catchAll(.*)', redirect: () => ({ path: defaultRoute() }) },
];

const router = createRouter({
    history: createWebHashHistory(),
    routes,
});

router.beforeEach(async (to) => {
    if (!store.ready) await refreshAuth();

    if (to.meta.guest) {
        if (store.user) return { path: defaultRoute() };
        return true;
    }
    if (!store.user) {
        return { path: APP_PATHS.login, query: { redirect: to.fullPath } };
    }
    if (to.meta.admin && !store.user.is_superuser) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute() };
    }
    if (to.meta.organizationAdmin && !canAccessOrganizationRoute(store.organizations)) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute() };
    }
    if (to.meta.libraryManagement && !canAccessLibraryManagementRoute(
        store.permissions,
        store.organizations,
    )) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute() };
    }
    if (to.meta.effectivePerm && !canAccessEffectiveRoute(
        store.permissions,
        to.meta.effectivePerm,
    )) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute() };
    }
    if (to.meta.perm && !canAccessRoute(store.user, store.permissions, to.meta.perm)) {
        ElMessage.warning('你没有访问该页面的权限');
        return { path: defaultRoute() };
    }
    return true;
});

let lastUnauthorizedAt = 0;
setUnauthorizedHandler(() => {
    clearAuthState();
    const now = Date.now();
    if (now - lastUnauthorizedAt < 3000) return;
    lastUnauthorizedAt = now;
    if (router.currentRoute.value.path !== APP_PATHS.login) {
        router.replace({
            path: APP_PATHS.login,
            query: { redirect: router.currentRoute.value.fullPath },
        });
    }
});

const app = createApp({ template: '<router-view />' });
app.config.compilerOptions.isCustomElement = (tag) => tag === 'local-icon';
app.use(ElementPlus, { locale: zhCn });
app.use(router);
app.mount('#app');
