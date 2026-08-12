export const APP_PATHS = Object.freeze({
    login: '/login',
    knowledgeUse: '/knowledge-use',
    chat: '/knowledge-use/chat',
    search: '/knowledge-use/search',
    retrievalTest: '/knowledge-use/retrieval-test',
    knowledgeAssets: '/knowledge-assets',
    documents: '/knowledge-assets/documents',
    catalog: '/knowledge-assets/catalog',
    importData: '/knowledge-assets/import',
    knowledgeGovernance: '/knowledge-governance',
    knowledgeGraph: '/knowledge-governance/graph',
    schemaLifecycle: '/knowledge-governance/schema',
    classificationReview: '/knowledge-governance/classification',
    usersPermissions: '/users-permissions',
    users: '/users-permissions/users',
    permissions: '/users-permissions/permissions',
    libraries: '/libraries',
    operationsCenter: '/operations-center',
    dashboard: '/operations-center/overview',
    operations: '/operations-center/status',
    jobs: '/operations-center/jobs',
    auditCenter: '/audit-center',
    audit: '/audit-center/operations',
    chatLogs: '/audit-center/chat',
    account: '/account',
    profile: '/account/profile',
    apiKeys: '/account/api-keys',
});

export const LEGACY_REDIRECTS = Object.freeze({
    '/chat': APP_PATHS.chat,
    '/search': APP_PATHS.search,
    '/retrieval-test': APP_PATHS.retrievalTest,
    '/documents': APP_PATHS.catalog,
    '/catalog': APP_PATHS.catalog,
    '/import': APP_PATHS.importData,
    '/knowledge-graph': APP_PATHS.knowledgeGraph,
    '/schema-lifecycle': APP_PATHS.schemaLifecycle,
    '/classification-review': APP_PATHS.catalog,
    '/users': APP_PATHS.users,
    '/permissions': APP_PATHS.permissions,
    '/dashboard': APP_PATHS.dashboard,
    '/operations': APP_PATHS.operations,
    '/jobs': APP_PATHS.jobs,
    '/audit': APP_PATHS.audit,
    '/chat-logs': APP_PATHS.chatLogs,
    '/api-keys': APP_PATHS.apiKeys,
});

export const DOMAIN_TABS = Object.freeze({
    knowledgeUse: Object.freeze([
        {
            key: 'chat', label: '智能问答', path: APP_PATHS.chat,
            access: 'chat', icon: 'sidebar:chat',
        },
        {
            key: 'search', label: '单库检索', path: APP_PATHS.search,
            access: 'search', icon: 'sidebar:search',
        },
        {
            key: 'retrievalTest',
            label: '联邦检索诊断',
            path: APP_PATHS.retrievalTest,
            access: 'retrievalTest',
            icon: 'mdi:text-search',
        },
    ]),
    knowledgeAssets: Object.freeze([
        {
            key: 'catalog', label: '知识资产', path: APP_PATHS.catalog,
            access: 'catalog', icon: 'mdi:bookshelf',
        },
        {
            key: 'import', label: '导入与替换', path: APP_PATHS.importData,
            access: 'import', icon: 'sidebar:import',
        },
    ]),
    knowledgeGovernance: Object.freeze([
        {
            key: 'knowledgeGraph',
            label: '知识图谱',
            path: APP_PATHS.knowledgeGraph,
            access: 'knowledgeGraph',
            icon: 'carbon:chart-relationship',
        },
    ]),
    usersPermissions: Object.freeze([
        {
            key: 'users', label: '用户管理', path: APP_PATHS.users,
            access: 'usersPermissions', icon: 'sidebar:user',
        },
        {
            key: 'permissions',
            label: '权限矩阵',
            path: APP_PATHS.permissions,
            access: 'usersPermissions',
            icon: 'sidebar:permission',
        },
    ]),
    libraries: Object.freeze([
        {
            key: 'libraries', label: '库配置', path: APP_PATHS.libraries,
            access: 'libraryConfiguration', icon: 'sidebar:library',
        },
        {
            key: 'schemaLifecycle',
            label: 'Schema 管理',
            path: APP_PATHS.schemaLifecycle,
            access: 'schemaLifecycle',
            icon: 'mdi:shield-key-outline',
        },
    ]),
    operationsCenter: Object.freeze([
        {
            key: 'dashboard',
            label: '概览',
            path: APP_PATHS.dashboard,
            access: 'operationsCenter',
            icon: 'sidebar:overview',
        },
        {
            key: 'operations',
            label: '服务与队列',
            path: APP_PATHS.operations,
            access: 'operationsCenter',
            icon: 'sidebar:runtime',
        },
        {
            key: 'jobs', label: '任务', path: APP_PATHS.jobs,
            access: 'operationsCenter', icon: 'sidebar:task',
        },
    ]),
    auditCenter: Object.freeze([
        {
            key: 'audit', label: '操作审计', path: APP_PATHS.audit,
            access: 'auditCenter', icon: 'sidebar:audit',
        },
        {
            key: 'chatLogs',
            label: '问答审计',
            path: APP_PATHS.chatLogs,
            access: 'auditCenter',
            icon: 'sidebar:qa-log',
        },
    ]),
    account: Object.freeze([
        {
            key: 'profile', label: '个人资料', path: APP_PATHS.profile,
            access: 'account', icon: 'sidebar:user',
        },
        {
            key: 'apiKeys', label: 'API Key', path: APP_PATHS.apiKeys,
            access: 'apiKeys', icon: 'sidebar:api-key',
        },
    ]),
});

const SIDEBAR_SECTIONS = Object.freeze([
    {
        key: 'member',
        label: '普通成员',
        hint: '知识使用与个人设置',
    },
    {
        key: 'admin',
        label: '管理员',
        hint: '系统配置与审计运维',
    },
]);

const SIDEBAR_DOMAINS = Object.freeze([
    {
        key: 'knowledgeUse',
        section: 'member',
        label: '知识使用',
        icon: 'sidebar:chat',
        path: APP_PATHS.knowledgeUse,
    },
    {
        key: 'knowledgeAssets',
        section: 'member',
        label: '知识资产',
        icon: 'sidebar:document',
        path: APP_PATHS.knowledgeAssets,
    },
    {
        key: 'knowledgeGovernance',
        section: 'member',
        label: '知识治理',
        icon: 'carbon:chart-relationship',
        path: APP_PATHS.knowledgeGovernance,
    },
    {
        key: 'usersPermissions',
        section: 'admin',
        label: '用户与权限',
        icon: 'sidebar:user',
        path: APP_PATHS.usersPermissions,
    },
    {
        key: 'libraries',
        section: 'admin',
        label: '库管理',
        icon: 'sidebar:library',
        path: APP_PATHS.libraries,
    },
    {
        key: 'operationsCenter',
        section: 'admin',
        label: '运维中心',
        icon: 'sidebar:runtime',
        path: APP_PATHS.operationsCenter,
    },
    {
        key: 'auditCenter',
        section: 'admin',
        label: '审计中心',
        icon: 'sidebar:audit',
        path: APP_PATHS.auditCenter,
    },
]);

const SIDEBAR_GROUPS = Object.freeze([
    ...SIDEBAR_DOMAINS.filter((group) => group.section === 'member'),
    {
        key: 'account',
        section: 'member',
        label: '账户设置',
        icon: 'sidebar:user',
        path: APP_PATHS.account,
    },
    ...SIDEBAR_DOMAINS.filter((group) => group.section === 'admin'),
]);

export function domainTabs(domain, access) {
    return (DOMAIN_TABS[domain] || []).filter((item) => access?.[item.access]);
}

export function sidebarItems(domain, access) {
    if (domain === 'knowledgeAssets') {
        const items = [];
        const canReadContent = access?.documents || access?.catalog;
        if (canReadContent) {
            items.push({
                key: 'knowledgeContent',
                label: '知识资产',
                path: APP_PATHS.catalog,
                activePaths: [APP_PATHS.catalog, APP_PATHS.documents],
                access: 'knowledgeContent',
                icon: 'sidebar:document',
            });
        }
        const importItem = DOMAIN_TABS.knowledgeAssets.find((item) => item.key === 'import');
        if (access?.import && importItem) items.push(importItem);
        return items;
    }
    if (domain !== 'knowledgeUse') return domainTabs(domain, access);

    const items = [];
    if (access?.chat) {
        items.push(DOMAIN_TABS.knowledgeUse.find((item) => item.key === 'chat'));
    }
    if (access?.search || access?.retrievalTest) {
        items.push({
            key: 'searchDiagnostics',
            label: '检索诊断',
            path: access?.search ? APP_PATHS.search : APP_PATHS.retrievalTest,
            activePaths: [APP_PATHS.search, APP_PATHS.retrievalTest],
            access: 'searchDiagnostics',
            icon: 'sidebar:search',
        });
    }
    return items.filter(Boolean);
}

export function firstDomainPath(domain, access) {
    return domainTabs(domain, access)[0]?.path || null;
}

export function visibleSidebarGroups(access) {
    const visible = SIDEBAR_GROUPS
        .map((group) => ({
            ...group,
            items: sidebarItems(group.key, access),
        }))
        .filter((group) => group.items.length > 0);
    return visible.map((group, index) => {
        const section = SIDEBAR_SECTIONS.find((item) => item.key === group.section);
        const previous = visible[index - 1];
        return {
            ...group,
            sectionLabel: section?.label || '',
            sectionHint: section?.hint || '',
            sectionStart: !previous || previous.section !== group.section,
        };
    });
}

export function defaultRouteForAccess(access) {
    for (const domain of ['knowledgeUse', 'knowledgeAssets', 'knowledgeGovernance']) {
        const path = firstDomainPath(domain, access);
        if (path) return path;
    }
    if (access?.operationsCenter) return APP_PATHS.dashboard;
    return APP_PATHS.profile;
}

export function domainTabTarget(path, currentQuery = {}) {
    const target = { path };
    if (!path.startsWith(`${APP_PATHS.knowledgeAssets}/`)) return target;

    const library = String(currentQuery.library || currentQuery.slug || '');
    if (!library) return target;
    target.query = { library };
    return target;
}

export function retrievalModeTarget(path, currentQuery = {}, state = {}) {
    const queryText = String(state.queryText ?? currentQuery.q ?? currentQuery.query ?? '').trim();
    const values = Array.isArray(state.librarySlugs)
        ? state.librarySlugs
        : String(currentQuery.libraries || currentQuery.library || '').split(',');
    const librarySlugs = values.map((value) => String(value || '').trim()).filter(Boolean);
    const query = {};
    if (queryText) query.q = queryText;
    if (path === APP_PATHS.search && librarySlugs[0]) query.library = librarySlugs[0];
    if (path === APP_PATHS.retrievalTest && librarySlugs.length) query.libraries = librarySlugs.join(',');
    return Object.keys(query).length ? { path, query } : { path };
}

export function legacyRedirectTarget(path, query = {}, hash = '') {
    const target = LEGACY_REDIRECTS[path];
    return target ? { path: target, query, hash } : null;
}
