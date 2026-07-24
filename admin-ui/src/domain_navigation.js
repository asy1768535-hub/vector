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
    '/documents': APP_PATHS.documents,
    '/catalog': APP_PATHS.catalog,
    '/import': APP_PATHS.importData,
    '/knowledge-graph': APP_PATHS.knowledgeGraph,
    '/schema-lifecycle': APP_PATHS.schemaLifecycle,
    '/classification-review': APP_PATHS.classificationReview,
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
        { key: 'chat', label: '智能问答', path: APP_PATHS.chat, access: 'chat' },
        { key: 'search', label: '单库检索', path: APP_PATHS.search, access: 'search' },
        {
            key: 'retrievalTest',
            label: '联邦检索诊断',
            path: APP_PATHS.retrievalTest,
            access: 'retrievalTest',
        },
    ]),
    knowledgeAssets: Object.freeze([
        { key: 'documents', label: '文档', path: APP_PATHS.documents, access: 'documents' },
        { key: 'catalog', label: '知识目录', path: APP_PATHS.catalog, access: 'catalog' },
        { key: 'import', label: '导入与替换', path: APP_PATHS.importData, access: 'import' },
    ]),
    knowledgeGovernance: Object.freeze([
        {
            key: 'knowledgeGraph',
            label: '知识图谱',
            path: APP_PATHS.knowledgeGraph,
            access: 'knowledgeGraph',
        },
        {
            key: 'schemaLifecycle',
            label: 'Schema 管理',
            path: APP_PATHS.schemaLifecycle,
            access: 'schemaLifecycle',
        },
        {
            key: 'classificationReview',
            label: '分类审核',
            path: APP_PATHS.classificationReview,
            access: 'classificationReview',
        },
    ]),
    usersPermissions: Object.freeze([
        { key: 'users', label: '用户管理', path: APP_PATHS.users, access: 'usersPermissions' },
        {
            key: 'permissions',
            label: '权限矩阵',
            path: APP_PATHS.permissions,
            access: 'usersPermissions',
        },
    ]),
    operationsCenter: Object.freeze([
        {
            key: 'dashboard',
            label: '概览',
            path: APP_PATHS.dashboard,
            access: 'operationsCenter',
        },
        {
            key: 'operations',
            label: '服务与队列',
            path: APP_PATHS.operations,
            access: 'operationsCenter',
        },
        { key: 'jobs', label: '任务', path: APP_PATHS.jobs, access: 'operationsCenter' },
    ]),
    auditCenter: Object.freeze([
        { key: 'audit', label: '操作审计', path: APP_PATHS.audit, access: 'auditCenter' },
        {
            key: 'chatLogs',
            label: '问答审计',
            path: APP_PATHS.chatLogs,
            access: 'auditCenter',
        },
    ]),
    account: Object.freeze([
        { key: 'profile', label: '个人资料', path: APP_PATHS.profile, access: 'account' },
        { key: 'apiKeys', label: 'API Key', path: APP_PATHS.apiKeys, access: 'apiKeys' },
    ]),
});

const SIDEBAR_DOMAINS = Object.freeze([
    {
        key: 'knowledgeUse',
        label: '知识使用',
        icon: 'sidebar:chat',
        path: APP_PATHS.knowledgeUse,
    },
    {
        key: 'knowledgeAssets',
        label: '知识资产',
        icon: 'sidebar:document',
        path: APP_PATHS.knowledgeAssets,
    },
    {
        key: 'knowledgeGovernance',
        label: '知识治理',
        icon: 'carbon:chart-relationship',
        path: APP_PATHS.knowledgeGovernance,
    },
    {
        key: 'usersPermissions',
        label: '用户与权限',
        icon: 'sidebar:user',
        path: APP_PATHS.usersPermissions,
    },
    {
        key: 'libraries',
        label: '库管理',
        icon: 'sidebar:library',
        path: APP_PATHS.libraries,
    },
    {
        key: 'operationsCenter',
        label: '运维中心',
        icon: 'sidebar:runtime',
        path: APP_PATHS.operationsCenter,
    },
    {
        key: 'auditCenter',
        label: '审计中心',
        icon: 'sidebar:audit',
        path: APP_PATHS.auditCenter,
    },
]);

export function domainTabs(domain, access) {
    return (DOMAIN_TABS[domain] || []).filter((item) => access?.[item.access]);
}

export function firstDomainPath(domain, access) {
    if (domain === 'libraries') return access?.libraries ? APP_PATHS.libraries : null;
    return domainTabs(domain, access)[0]?.path || null;
}

export function visibleSidebarDomains(access) {
    return SIDEBAR_DOMAINS
        .filter((item) => access?.[item.key])
        .map((item) => ({
            ...item,
            path: firstDomainPath(item.key, access) || item.path,
        }));
}

export function defaultRouteForAccess(access) {
    if (access?.operationsCenter) return APP_PATHS.dashboard;
    for (const domain of ['knowledgeUse', 'knowledgeAssets', 'knowledgeGovernance']) {
        const path = firstDomainPath(domain, access);
        if (path) return path;
    }
    return APP_PATHS.profile;
}

export function domainTabTarget(path, currentQuery = {}) {
    const target = { path };
    if (!path.startsWith(`${APP_PATHS.knowledgeAssets}/`)) return target;

    const library = String(currentQuery.library || currentQuery.slug || '');
    if (!library) return target;
    target.query = path === APP_PATHS.documents ? { slug: library } : { library };
    return target;
}

export function legacyRedirectTarget(path, query = {}, hash = '') {
    const target = LEGACY_REDIRECTS[path];
    return target ? { path: target, query, hash } : null;
}
