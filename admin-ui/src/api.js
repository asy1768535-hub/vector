// 后端 REST 客户端。所有调用走同源 + cookie；自动处理 401 跳登录。

import { humanizeApiError } from './api_errors.js';
import { cachedRequest, clearCache, invalidateByPrefix } from './request_cache.js';

const BASE = '';  // 同源

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

function attachRetryMetadata(error, response) {
    const uploadOffset = response.headers.get('Upload-Offset');
    const retryAfter = response.headers.get('Retry-After');
    if (uploadOffset !== null && uploadOffset.trim() !== '') {
        const parsed = Number(uploadOffset);
        if (Number.isInteger(parsed) && parsed >= 0) error.uploadOffset = parsed;
    }
    if (retryAfter !== null && retryAfter.trim() !== '') {
        const parsed = Number(retryAfter);
        if (Number.isFinite(parsed) && parsed >= 0) error.retryAfterSeconds = parsed;
    }
    return error;
}

async function request(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const resp = await fetch(BASE + path, {
        credentials: 'include',
        ...options,
    });
    if (resp.status === 401) {
        clearCache();
        if (onUnauthorized) onUnauthorized();
        const err = new Error('登录已过期，请重新登录');
        err.status = 401;
        throw err;
    }
    if (resp.status === 204) {
        if (method !== 'GET') clearCache();
        return null;
    }
    const ct = resp.headers.get('content-type') || '';
    const body = ct.includes('application/json') ? await resp.json() : await resp.text();
    if (!resp.ok) {
        const msg = humanizeApiError(body, resp.status, `请求失败（HTTP ${resp.status}）`);
        const err = new Error(msg);
        err.status = resp.status;
        err.body = body;
        throw attachRetryMetadata(err, resp);
    }
    if (method !== 'GET') clearCache();
    return body;
}

function jsonBody(method, body) {
    return {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    };
}

// ── 认证 / 当前用户 ──────────────────────────────────────────
export async function login(email, password) {
    const body = new URLSearchParams();
    body.set('username', email);
    body.set('password', password);
    const resp = await fetch(BASE + '/auth/jwt/login', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
    });
    if (resp.status === 400 || resp.status === 401) {
        const j = await resp.json().catch(() => ({}));
        // fastapi-users 返回的是英文错误码（如 LOGIN_BAD_CREDENTIALS），统一映射成中文；
        // 未知码也回退中文，避免把英文码直接抛给用户。
        const code = typeof j.detail === 'string' ? j.detail : '';
        const CN = {
            LOGIN_BAD_CREDENTIALS: '邮箱或密码错误',
            LOGIN_USER_NOT_VERIFIED: '账号未激活，请联系管理员',
        };
        throw new Error(CN[code] || '邮箱或密码错误');
    }
    if (!resp.ok) throw new Error(`登录失败: HTTP ${resp.status}`);
    clearCache();
    return resp.status === 204 ? null : resp.json();
}

export async function logout() {
    try { await fetch(BASE + '/auth/jwt/logout', { method: 'POST', credentials: 'include' }); }
    finally { clearCache(); }
}

// 修改自己的资料/密码（复用 fastapi-users PATCH /users/me，不新建更新逻辑）
export const updateMe = (data) => request('/users/me', jsonBody('PATCH', data));
// 管理员重置某用户密码（专用端点；password 不混入普通 PATCH）
export const adminResetUserPassword = (userId, password) =>
    request(`/admin/users/${userId}/reset-password`, jsonBody('POST', { password }));

// ── API Keys 自助 ────────────────────────────────────────────
export const listApiKeys = (forceRefresh) => cachedRequest('listApiKeys', () => request('/me/api-keys'), 15000, forceRefresh);
export const createApiKey = (name, organizationId = null, expiresAt = null) =>
    request('/me/api-keys', jsonBody('POST', {
        name,
        organization_id: organizationId,
        expires_at: expiresAt,
    }));
export const revokeApiKey = (id) => request(`/me/api-keys/${id}`, { method: 'DELETE' });

// ── Admin: Users ─────────────────────────────────────────────
const _listUsersKey = (params) => 'listUsers:' + new URLSearchParams(params).toString();
export const listUsers = (params = {}, forceRefresh) => cachedRequest(_listUsersKey(params), () => request('/admin/users?' + new URLSearchParams(params).toString()), 30000, forceRefresh);
export const createUser = (data) => request('/admin/users', jsonBody('POST', data));
export const updateUser = (id, data) => request(`/admin/users/${id}`, jsonBody('PATCH', data));
export const disableUser = (id) => request(`/admin/users/${id}`, { method: 'DELETE' });

// ── Admin: Libraries ─────────────────────────────────────────
const _listLibsKey = (params) => 'listLibraries:' + new URLSearchParams(params).toString();
export const listLibraries = (params = {}, forceRefresh) => cachedRequest(_listLibsKey(params), () => request('/admin/libraries?' + new URLSearchParams(params).toString()), 30000, forceRefresh);
export const createLibrary = (data) => request('/admin/libraries', jsonBody('POST', data));
export const updateLibrary = (slug, data) => request(`/admin/libraries/${slug}`, jsonBody('PATCH', data));
export const deleteLibrary = (slug) => request(`/admin/libraries/${slug}`, { method: 'DELETE' });
export const rebuildLibraryCollection = (slug) =>
    request(`/admin/libraries/${slug}/rebuild-collection`, { method: 'POST' });
export const testLibraryEmbedding = (slug) =>
    request(`/admin/libraries/${slug}/test-embedding`, { method: 'POST' });
export const verifyLibraryEmbedding = (slug) =>
    request(`/admin/libraries/${slug}/verify-embedding-profile`, { method: 'POST' });

// ── Admin: Library FAQ（常用问题） ───────────────────────────
// options.includeInactive=true 时附带停用项（仅 admin/superuser 生效）
const _faqKey = (slug, opts) => `listLibraryFaqs:${slug}:${opts.includeInactive ? '1' : '0'}`;
export const listLibraryFaqs = (slug, options = {}, forceRefresh) => {
    const qs = options.includeInactive ? '?include_inactive=true' : '';
    return cachedRequest(_faqKey(slug, options), () => request(`/admin/libraries/${slug}/faqs${qs}`), 30000, forceRefresh);
};
export const createLibraryFaq = (slug, payload) =>
    request(`/admin/libraries/${slug}/faqs`, jsonBody('POST', payload));
export const updateLibraryFaq = (slug, faqId, payload) =>
    request(`/admin/libraries/${slug}/faqs/${faqId}`, jsonBody('PATCH', payload));
export const deleteLibraryFaq = (slug, faqId) =>
    request(`/admin/libraries/${slug}/faqs/${faqId}`, { method: 'DELETE' });

// ── Admin: Permissions ───────────────────────────────────────
export const listUserPerms = (user_id, forceRefresh) => cachedRequest(`listUserPerms:${user_id}`, () => request(`/admin/permissions?user_id=${user_id}`), 30000, forceRefresh);
export const grantPerms = (data) => request('/admin/permissions', jsonBody('PUT', data));
export const revokePerms = (data) => request('/admin/permissions', jsonBody('DELETE', data));
export const listUserOrganizationRoles = (userId) =>
    request(`/admin/permissions/users/${encodeURIComponent(userId)}/organizations`);
export const updateOrganizationMember = (organizationId, membershipId, data) =>
    request(`/organizations/${encodeURIComponent(organizationId)}/members/${encodeURIComponent(membershipId)}`, jsonBody('PATCH', data));

// ── Documents（library 维度） ─────────────────────────────────
export const ingestDocument = (slug, data) =>
    request(`/libraries/${slug}/documents`, jsonBody('POST', data));
export const updateDocument = (slug, id, data) =>
    request(`/libraries/${slug}/documents/${id}`, jsonBody('PUT', data));
const _docsKey = (slug, params) => `listDocuments:${slug}:${new URLSearchParams(params).toString()}`;
export const listDocuments = (slug, params = {}, forceRefresh) => cachedRequest(_docsKey(slug, params), () => request(`/libraries/${slug}/documents?${new URLSearchParams(params).toString()}`), 15000, forceRefresh);
export const deleteDocument = (slug, id) =>
    request(`/libraries/${slug}/documents/${id}`, { method: 'DELETE' });
export const bulkDeleteDocuments = (slug) =>
    request(`/libraries/${slug}/documents/bulk-delete`, { method: 'POST' });
export const libraryStats = (slug, forceRefresh) => cachedRequest(`libraryStats:${slug}`, () => request(`/libraries/${slug}/stats`), 15000, forceRefresh);
export const listFolders = (slug, forceRefresh) => cachedRequest(
    `listFolders:${slug}`,
    () => request(`/libraries/${slug}/folders`),
    15000,
    forceRefresh,
);
export const queryLibrary = (slug, data) =>
    request(`/libraries/${slug}/query`, jsonBody('POST', data));
export const getImportConfiguration = (slug) =>
    request(`/libraries/${slug}/import-configuration`);
export const updateImportConfiguration = (slug, data) =>
    request(`/libraries/${slug}/import-configuration`, jsonBody('PUT', data));
export const createImportSession = (slug, data, { signal } = {}) =>
    request(`/libraries/${slug}/import-sessions`, {
        ...jsonBody('POST', data),
        signal,
    });
export async function uploadImportChunk(slug, jobId, chunk, offset, { signal } = {}) {
    const resp = await fetch(
        `${BASE}/libraries/${slug}/import-sessions/${jobId}/content`,
        {
            method: 'PUT',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/octet-stream',
                'Upload-Offset': String(offset),
            },
            body: chunk,
            signal,
        },
    );
    if (resp.status === 401) {
        clearCache();
        if (onUnauthorized) onUnauthorized();
        throw new Error('登录已过期，请重新登录');
    }
    if (!resp.ok) {
        const contentType = resp.headers.get('content-type') || '';
        const body = contentType.includes('application/json')
            ? await resp.json()
            : await resp.text();
        const error = new Error(
            humanizeApiError(body, resp.status, `上传分块失败（HTTP ${resp.status}）`),
        );
        error.status = resp.status;
        error.body = body;
        throw attachRetryMetadata(error, resp);
    }
    const rawOffset = resp.headers.get('Upload-Offset');
    if (rawOffset === null || rawOffset.trim() === '') {
        throw new Error('上传响应缺少有效偏移量，请重试');
    }
    const nextOffset = Number(rawOffset);
    if (!Number.isInteger(nextOffset) || nextOffset < 0) {
        throw new Error('上传响应缺少有效偏移量，请重试');
    }
    return nextOffset;
}
export const completeImportSession = (slug, jobId, { signal } = {}) =>
    request(`/libraries/${slug}/import-sessions/${jobId}/complete`, {
        method: 'POST',
        signal,
    });
export const retryImportJob = (slug, jobId) =>
    request(`/libraries/${slug}/import-jobs/${jobId}/retry`, { method: 'POST' });
export const listStoredFiles = (params = {}, { signal } = {}) => {
    const query = new URLSearchParams({
        library_slug: String(params.librarySlug || ''),
        page: String(Number.isInteger(params.page) && params.page > 0 ? params.page : 1),
        page_size: String(params.pageSize === 20 ? 20 : 50),
    });
    if (typeof params.path === 'string' && params.path) query.set('path', params.path);
    return request(`/me/stored-files?${query.toString()}`, { signal });
};
export const listFailedImportTaskFiles = (params = {}, { signal } = {}) => {
    const query = new URLSearchParams({
        library_slug: String(params.librarySlug || ''),
        scope: 'all',
        failed_only: 'true',
        page: String(Number.isInteger(params.page) && params.page > 0 ? params.page : 1),
        page_size: String(params.pageSize === 20 ? 20 : 50),
    });
    if (typeof params.path === 'string' && params.path) query.set('path', params.path);
    return request(`/me/import-task-files?${query.toString()}`, { signal });
};
export const retryPersonalImportTask = (jobId) =>
    request(`/me/import-tasks/${jobId}/retry`, { method: 'POST' });
export const listPersonalImportTasks = (params = {}, { signal } = {}) => {
    const query = new URLSearchParams({
        scope: params.scope === '30d' ? '30d' : 'all',
        limit: String(params.limit || 50),
    });
    if (params.cursor) query.set('cursor', String(params.cursor));
    if (params.page) query.set('page', String(params.page));
    if (params.librarySlug) query.set('library_slug', String(params.librarySlug));
    if (params.status && params.status !== 'all') query.set('status', String(params.status));
    return request(`/me/import-tasks?${query.toString()}`, { signal });
};
export const getPersonalImportTaskSummary = (params = {}, { signal } = {}) => {
    const options = typeof params === 'string' ? { scope: params } : params;
    const query = new URLSearchParams({ scope: options.scope === '30d' ? '30d' : 'all' });
    if (options.librarySlug) query.set('library_slug', String(options.librarySlug));
    if (options.status && options.status !== 'all') query.set('status', String(options.status));
    return request(`/me/import-task-summary?${query.toString()}`, { signal });
};
export const getStoredFileDownloadUrl = (librarySlug, fileResourceId) =>
    request(`/me/stored-files/${fileResourceId}/download?${new URLSearchParams({ library_slug: librarySlug }).toString()}`);
export const deleteStoredFile = (librarySlug, fileResourceId) =>
    request(`/me/stored-files/${fileResourceId}?${new URLSearchParams({ library_slug: librarySlug }).toString()}`, {
        method: 'DELETE',
    });
export const deletePersonalFileFolder = (librarySlug, path) =>
    request(`/me/files/folder?${new URLSearchParams({ library_slug: librarySlug, path }).toString()}`, {
        method: 'DELETE',
    });
export const getUploadGraphExtractionConfiguration = (slug) =>
    request(`/libraries/${slug}/v04/graph-extractions/upload-configuration`);
export const listGraphExtractions = (slug, params = {}) =>
    request(`/libraries/${slug}/v04/graph-extractions/?${new URLSearchParams(params).toString()}`);
export const retryGraphExtraction = (slug, jobId) =>
    request(`/libraries/${slug}/v04/graph-extractions/${jobId}/retry`, { method: 'POST' });
export const listSchemaDiscoveryRuns = (slug, params = {}) =>
    request(`/libraries/${slug}/v04/graph-extractions/schema-discovery-runs?${new URLSearchParams(params).toString()}`);
export const getDocumentSource = (slug, documentId, chunkId) =>
    request(`/libraries/${slug}/documents/${documentId}/source?` + new URLSearchParams({ chunk_id: chunkId }).toString());
export const getDocumentFullSource = (slug, documentId, params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request(`/libraries/${slug}/documents/${documentId}/source/full${query ? `?${query}` : ''}`);
};
export const getChatGraphContext = (slug, chunkId) =>
    request(`/libraries/${slug}/chat/graph-context?` + new URLSearchParams({ chunk_id: chunkId }).toString());
export async function downloadDocumentFile(slug, documentId) {
    const resp = await fetch(BASE + `/libraries/${slug}/documents/${documentId}/file`, {
        credentials: 'include',
    });
    if (resp.status === 401) {
        clearCache();
        if (onUnauthorized) onUnauthorized();
        const err = new Error('登录已过期，请重新登录');
        err.status = 401;
        throw err;
    }
    if (!resp.ok) {
        const ct = resp.headers.get('content-type') || '';
        const body = ct.includes('application/json') ? await resp.json().catch(() => null) : await resp.text().catch(() => '');
        const msg = humanizeApiError(body, resp.status, `请求失败（HTTP ${resp.status}）`);
        const err = new Error(msg);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
    const disposition = resp.headers.get('content-disposition') || '';
    let filename = '';
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const asciiMatch = disposition.match(/filename="?([^";]+)"?/i);
    if (utf8Match) filename = decodeURIComponent(utf8Match[1]);
    else if (asciiMatch) filename = asciiMatch[1];
    return { blob: await resp.blob(), filename: filename || 'document-file' };
}

// ── v0.8 Knowledge Catalog（严格 current Revision 只读投影） ─────
const CATALOG_QUERY_KEYS = [
    'title',
    'uploader_id',
    'include_system_uploader',
    'uploaded_from',
    'uploaded_to',
    'limit',
    'cursor',
];

function catalogQuery(params = {}) {
    return new URLSearchParams(
        CATALOG_QUERY_KEYS
            .map((key) => [key, params[key]])
            .filter(([, value]) => (
                value !== null && value !== undefined && value !== ''
            )),
    ).toString();
}

export const listCatalogDocuments = (slug, params = {}, forceRefresh) => {
    const query = catalogQuery(params);
    const key = `listCatalogDocuments:${slug}:${query}`;
    return cachedRequest(
        key,
        () => request(`/libraries/${slug}/catalog/documents${query ? `?${query}` : ''}`),
        10000,
        forceRefresh,
    );
};
export const listCatalogUploaderOptions = (slug, forceRefresh) => cachedRequest(
    `listCatalogUploaderOptions:${slug}`,
    () => request(`/libraries/${slug}/catalog/uploader-options`),
    10000,
    forceRefresh,
);
export const getCatalogDocument = (slug, documentId, forceRefresh) => cachedRequest(
    `getCatalogDocument:${slug}:${documentId}`,
    () => request(`/libraries/${slug}/catalog/documents/${documentId}`),
    10000,
    forceRefresh,
);
export const getCatalogEvidence = (slug, evidenceId, forceRefresh) => cachedRequest(
    `getCatalogEvidence:${slug}:${evidenceId}`,
    () => request(`/libraries/${slug}/catalog/evidence/${evidenceId}`),
    10000,
    forceRefresh,
);
export const getCatalogFileAccess = (slug, revisionFileId) =>
    request(`/libraries/${slug}/catalog/files/${revisionFileId}/access`);
export const getCatalogDocumentProcessing = (slug, documentId) =>
    request(`/libraries/${slug}/catalog/documents/${documentId}/processing`);
export const retryCatalogDocumentProcessing = (slug, documentId, stage, body) =>
    request(
        `/libraries/${slug}/catalog/documents/${documentId}/processing/${stage}/retry`,
        jsonBody('POST', body),
    );

// ── v0.8 Classification review console ──────────────────────────────────────
export const listClassificationReviews = (slug, { limit = 20, offset = 0 } = {}) => {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return request(`/libraries/${slug}/classifications/reviews?${query.toString()}`);
};
export const reviewClassificationRun = (slug, runId, body) =>
    request(
        `/libraries/${slug}/classifications/runs/${runId}/review`,
        jsonBody('POST', body),
    );
export const setDocumentClassification = (slug, documentId, body) =>
    request(
        `/libraries/${slug}/classifications/documents/${documentId}`,
        jsonBody('PUT', body),
    );
// ── v0.8 Organization retrieval diagnostics ────────────────
export const checkLibraryCompatibility = (body) =>
    request('/me/library-compatibility/check', jsonBody('POST', body));
export const runOrganizationRetrievalTest = (organizationId, body) =>
    request(`/organizations/${organizationId}/retrieval-tests`, jsonBody('POST', body));

const GRAPH_ENTITY_SEARCH_KEYS = [
    'library_slugs',
    'scope_id',
    'query',
    'ontology_version_ids',
    'type_keys',
    'statuses',
    'source_types',
    'publication_state',
    'cursor',
    'limit',
];
const GRAPH_RELATION_SEARCH_KEYS = [...GRAPH_ENTITY_SEARCH_KEYS, 'review_statuses'];

function graphBody(source, keys) {
    const body = {};
    for (const key of keys) {
        if (Object.hasOwn(source || {}, key) && source[key] !== undefined) body[key] = source[key];
    }
    return body;
}

function graphCommand(path, body, keys) {
    return request(path, jsonBody('POST', graphBody(body, keys)));
}

export const searchGraphEntities = (organizationId, body = {}) => graphCommand(
    `/organizations/${organizationId}/graph-catalog/entities:search`,
    body,
    GRAPH_ENTITY_SEARCH_KEYS,
);
export const searchGraphRelations = (organizationId, body = {}) => graphCommand(
    `/organizations/${organizationId}/graph-catalog/relations:search`,
    body,
    GRAPH_RELATION_SEARCH_KEYS,
);
export const getGraphEntity = (organizationId, librarySlug, entityId) => request(
    `/organizations/${organizationId}/graph-catalog/libraries/${librarySlug}/entities/${entityId}`,
);
export const getGraphRelation = (organizationId, librarySlug, relationId) => request(
    `/organizations/${organizationId}/graph-catalog/libraries/${librarySlug}/relations/${relationId}`,
);

function boundedInteger(value, minimum, maximum, fallback) {
    if (!Number.isInteger(value)) return fallback;
    return Math.min(maximum, Math.max(minimum, value));
}

function graphTraversalBody(source = {}) {
    const direction = ['outbound', 'inbound', 'both'].includes(source.direction)
        ? source.direction
        : 'both';
    const relationTypeKeys = [];
    for (const value of Array.isArray(source.relation_type_keys) ? source.relation_type_keys : []) {
        if (typeof value !== 'string') continue;
        const key = value.trim();
        if (!key || key.length > 128 || relationTypeKeys.includes(key)) continue;
        relationTypeKeys.push(key);
        if (relationTypeKeys.length === 8) break;
    }
    const seed = Array.isArray(source.seeds) ? source.seeds[0] : null;
    return {
        ontology_version_id: source.ontology_version_id,
        expected_publication_id: source.expected_publication_id,
        seeds: seed?.entity_id ? [{ entity_id: seed.entity_id }] : [],
        direction,
        relation_type_keys: relationTypeKeys,
        max_hops: boundedInteger(source.max_hops, 1, 2, 1),
        max_nodes: boundedInteger(source.max_nodes, 1, 100, 60),
        max_relations: boundedInteger(source.max_relations, 1, 200, 100),
        include_evidence_locators: true,
    };
}

export const queryPublishedGraph = (librarySlug, body = {}) => request(
    `/libraries/${librarySlug}/v06/graph/query`,
    jsonBody('POST', graphTraversalBody(body)),
);

export const getGraphGovernanceContext = (slug) =>
    request(`/libraries/${slug}/graph-governance/context`);
export const listGraphGovernanceActions = (
    slug,
    { statuses = [], limit = 50, offset = 0 } = {},
) => {
    const query = new URLSearchParams();
    for (const value of statuses || []) query.append('status', value);
    query.set('limit', String(limit));
    query.set('offset', String(offset));
    return request(`/libraries/${slug}/graph-governance/actions?${query.toString()}`);
};
export const decideGraphGovernanceAction = (slug, actionId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/actions/${actionId}/decision`,
    body,
    ['expected_status', 'decision', 'reason_code'],
);
export const cancelGraphGovernanceAction = (slug, actionId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/actions/${actionId}/cancel`,
    body,
    ['expected_status', 'reason_code'],
);

export const submitGraphEntity = (slug, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities`,
    body,
    ['ontology_version_id', 'entity_type_id', 'canonical_name', 'properties', 'idempotency_key'],
);
export const submitGraphRelation = (slug, body) => graphCommand(
    `/libraries/${slug}/graph-governance/relations`,
    body,
    [
        'ontology_version_id',
        'relation_type_id',
        'source_entity_id',
        'target_entity_id',
        'properties',
        'idempotency_key',
    ],
);
export const correctGraphEntity = (slug, entityId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities/${entityId}/corrections`,
    body,
    ['expected_state_hash', 'canonical_name', 'properties', 'idempotency_key'],
);
export const correctGraphRelation = (slug, relationId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/relations/${relationId}/corrections`,
    body,
    ['expected_state_hash', 'source_entity_id', 'target_entity_id', 'properties', 'idempotency_key'],
);
export const addGraphEntityAlias = (slug, entityId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities/${entityId}/aliases`,
    body,
    ['expected_entity_state_hash', 'alias', 'idempotency_key'],
);
export const reviewGraphRelation = (slug, relationId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/relations/${relationId}/review`,
    body,
    ['expected_state_hash', 'decision', 'reason_code', 'idempotency_key'],
);

const GRAPH_STATE_KEYS = ['expected_state_hash', 'reason_code', 'idempotency_key'];
export const disableGraphEntity = (slug, entityId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities/${entityId}/disable`,
    body,
    GRAPH_STATE_KEYS,
);
export const restoreGraphEntity = (slug, entityId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities/${entityId}/restore`,
    body,
    GRAPH_STATE_KEYS,
);
export const disableGraphRelation = (slug, relationId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/relations/${relationId}/disable`,
    body,
    GRAPH_STATE_KEYS,
);
export const restoreGraphRelation = (slug, relationId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/relations/${relationId}/restore`,
    body,
    GRAPH_STATE_KEYS,
);
export const disableGraphAlias = (slug, aliasId, body) => graphCommand(
    `/libraries/${slug}/graph-governance/aliases/${aliasId}/disable`,
    body,
    GRAPH_STATE_KEYS,
);
export const mergeGraphEntities = (slug, body) => graphCommand(
    `/libraries/${slug}/graph-governance/entities/merge`,
    body,
    [
        'ontology_version_id',
        'survivor_entity_id',
        'loser_entity_id',
        'expected_survivor_state_hash',
        'expected_loser_state_hash',
        'reason_code',
        'resolutions',
        'idempotency_key',
    ],
);
export const planGraphGovernancePublication = (slug, body) => graphCommand(
    `/libraries/${slug}/graph-governance/publications/plan`,
    body,
    [
        'ontology_version_id',
        'action_ids',
        'expected_parent_publication_id',
        'dry_run',
        'idempotency_key',
    ],
);

export const listGraphPublications = (slug, params = {}) => {
    const query = new URLSearchParams();
    for (const key of ['status', 'source_mode', 'ontology_version_id', 'page', 'page_size']) {
        const value = params?.[key];
        if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
    }
    const suffix = query.toString();
    return request(`/libraries/${slug}/v05/graph-publications/${suffix ? `?${suffix}` : ''}`);
};
export const getActiveGraphPublication = (slug, ontologyVersionId = '') => {
    const query = ontologyVersionId
        ? `?${new URLSearchParams({ ontology_version_id: ontologyVersionId }).toString()}`
        : '';
    return request(`/libraries/${slug}/v05/graph-publications/active${query}`);
};
export const listGraphPublicationItems = (slug, publicationId, params = {}) => {
    const query = new URLSearchParams();
    for (const key of ['item_kind', 'status', 'page', 'page_size']) {
        const value = params?.[key];
        if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
    }
    const suffix = query.toString();
    return request(
        `/libraries/${slug}/v05/graph-publications/${publicationId}/items${suffix ? `?${suffix}` : ''}`,
    );
};
export const activateGraphPublication = (slug, publicationId, body) => graphCommand(
    `/libraries/${slug}/v05/graph-publications/${publicationId}/activate`,
    body,
    ['idempotency_key', 'expected_manifest_hash'],
);
export const cancelGraphPublication = (slug, publicationId, body) => graphCommand(
    `/libraries/${slug}/v05/graph-publications/${publicationId}/cancel`,
    body,
    ['idempotency_key', 'reason_code'],
);
export const rollbackGraphPublication = (slug, publicationId, body) => graphCommand(
    `/libraries/${slug}/v05/graph-publications/${publicationId}/rollback`,
    body,
    ['idempotency_key', 'dry_run'],
);
export const rerunGraphExtraction = (slug, jobId, body) => graphCommand(
    `/libraries/${slug}/v04/graph-extractions/${jobId}/rerun`,
    body,
    ['client_idempotency_key'],
);

// ── Admin: Jobs ──────────────────────────────────────────────
const _monitoredTasksKey = (params) => 'listMonitoredTasks:' + new URLSearchParams(params).toString();
export const listMonitoredTasks = (params = {}, forceRefresh) => cachedRequest(
    _monitoredTasksKey(params),
    () => request('/admin/jobs/monitor?' + new URLSearchParams(params).toString()),
    8000,
    forceRefresh,
);
export const retryMonitoredTasks = (items) => request(
    '/admin/jobs/monitor/retry',
    jsonBody('POST', { items }),
);
export const monitoredTaskStats = (forceRefresh) => cachedRequest(
    'monitoredTaskStats',
    () => request('/admin/jobs/monitor/stats'),
    8000,
    forceRefresh,
);
export const resetFailedJobs = (libraryId = null) =>
    request('/admin/jobs/reset-failed' + (libraryId ? `?library_id=${libraryId}` : ''), { method: 'POST' });

// ── Admin: Operations status（运行状态监控，docs/26） ─────────
export const operationsStatus = (forceRefresh) => cachedRequest('operationsStatus', () => request('/admin/operations/status'), 10000, forceRefresh);

// ── Admin: Audit log ─────────────────────────────────────────
const _auditKey = (params) => 'listAudit:' + new URLSearchParams(params).toString();
export const listAudit = (params = {}, forceRefresh) => cachedRequest(_auditKey(params), () => request('/admin/audit-log?' + new URLSearchParams(params).toString()), 15000, forceRefresh);

// ── Chat 用户端（轻量问答 v1） ───────────────────────────────
export const listChatLibraries = (forceRefresh) => cachedRequest('listChatLibraries', () => request('/chat/libraries'), 30000, forceRefresh);

// 会话历史
export const listChatConversations = (includeArchived = false, forceRefresh) => cachedRequest(`listChatConversations:${includeArchived ? '1' : '0'}`, () => request('/chat/conversations' + (includeArchived ? '?include_archived=true' : '')), 15000, forceRefresh);
export const getChatConversationMessages = (id, params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request(`/chat/conversations/${id}/messages${query ? `?${query}` : ''}`);
}; // not cached — real-time
export const archiveChatConversation = (id) =>
    request(`/chat/conversations/${id}/archive`, { method: 'POST' });
export const deleteChatConversation = (id) =>
    request(`/chat/conversations/${id}`, { method: 'DELETE' });

// 管理后台：问答日志
const _chatLogsKey = (params) => 'adminListChatLogs:' + new URLSearchParams(params).toString();
export const adminListChatLogs = (params = {}, forceRefresh) => cachedRequest(_chatLogsKey(params), () => request('/admin/chat-logs?' + new URLSearchParams(params).toString()), 15000, forceRefresh);

// 流式问答（SSE over fetch）。handlers: { onSources, onDelta, onError, onDone }
// 非 2xx（503/403/401 等）按普通错误抛出，由调用方处理；流内 error 走 onError。
export async function streamChatMessage(payload, handlers = {}, signal = null) {
    const { onSources, onDelta, onError, onDone } = handlers;
    const resp = await fetch(BASE + '/chat/stream', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal,
    });
    if (resp.status === 401) {
        clearCache();
        if (onUnauthorized) onUnauthorized();
        const err = new Error('登录已过期，请重新登录');
        err.status = 401;
        throw err;
    }
    if (!resp.ok) {
        let body = null;
        try { body = await resp.json(); } catch (_) { /* ignore */ }
        const msg = humanizeApiError(body, resp.status, `请求失败（HTTP ${resp.status}）`);
        const err = new Error(msg);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
    // Invalidate chat caches immediately, before reading stream
    invalidateByPrefix('listChatConversations');
    invalidateByPrefix('listChatLibraries');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    const handleEvent = (ev) => {
        const dataLine = ev.split('\n').find((l) => l.startsWith('data:'));
        if (!dataLine) return;
        const data = dataLine.slice(5).trim();
        if (!data) return;
        let obj;
        try { obj = JSON.parse(data); } catch (_) { return; }
        if (obj.type === 'sources') onSources && onSources(obj);
        else if (obj.type === 'delta') onDelta && onDelta(obj.text || '');
        else if (obj.type === 'error') onError && onError(obj.message || '生成失败');
        else if (obj.type === 'done') onDone && onDone();
    };
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const events = buf.split('\n\n');
        buf = events.pop();                       // 末段可能不完整，留到下一轮
        for (const ev of events) handleEvent(ev);
    }
    buf += decoder.decode();
    if (buf.trim()) handleEvent(buf);
}

// ── Health ───────────────────────────────────────────────────
const SCHEMA_COMMON_KEYS = ['expected_version_state_hash', 'idempotency_key'];
const SCHEMA_ENTITY_KEYS = [...SCHEMA_COMMON_KEYS, 'key', 'label', 'description', 'properties_schema'];
const SCHEMA_RELATION_KEYS = [
    ...SCHEMA_ENTITY_KEYS, 'direction', 'requires_evidence', 'default_review_policy',
];
const SCHEMA_ATTRIBUTE_KEYS = [
    ...SCHEMA_COMMON_KEYS, 'owner_kind', 'owner_type_id', 'key', 'label',
    'value_type', 'required', 'enum_values', 'validation_schema', 'indexed',
];
const SCHEMA_CONSTRAINT_KEYS = [
    ...SCHEMA_COMMON_KEYS, 'relation_type_id', 'source_entity_type_id',
    'target_entity_type_id', 'cardinality', 'requires_review',
];

function schemaBody(source, keys) {
    const body = {};
    for (const key of keys) {
        if (Object.hasOwn(source || {}, key) && source[key] !== undefined) body[key] = source[key];
    }
    return body;
}

const schemaPath = (slug, versionId, suffix = '') => (
    `/libraries/${slug}/schema-lifecycle/versions/${versionId}${suffix}`
);

export const listSchemaVersions = (slug) => request(`/libraries/${slug}/schema-lifecycle/versions`);
export const getSchemaVersion = (slug, versionId) => request(schemaPath(slug, versionId));
export const importSchemaFile = (slug, body) => request(
    `/libraries/${slug}/schema-lifecycle/import-file`,
    jsonBody('POST', body),
);
export const validateSchemaVersion = (slug, versionId) =>
    request(schemaPath(slug, versionId, '/validate'), { method: 'POST' });
export const getSchemaImpact = (slug, versionId) => request(schemaPath(slug, versionId, '/impact'));
export const cloneSchemaVersion = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/clone'),
    jsonBody('POST', schemaBody(body, [...SCHEMA_COMMON_KEYS, 'description'])),
);
export const activateSchemaVersion = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/activate'),
    jsonBody('POST', schemaBody(body, [
        ...SCHEMA_COMMON_KEYS, 'expected_active_version_id', 'confirmation',
    ])),
);
export const deleteSchemaDraft = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/delete-draft'),
    jsonBody('POST', schemaBody(body, [...SCHEMA_COMMON_KEYS, 'confirmation'])),
);
export const disableSchemaVersion = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/disable'),
    jsonBody('POST', schemaBody(body, [...SCHEMA_COMMON_KEYS, 'confirmation'])),
);
export const createSchemaEntityType = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/entity-types'),
    jsonBody('POST', schemaBody(body, SCHEMA_ENTITY_KEYS)),
);
export const createSchemaRelationType = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/relation-types'),
    jsonBody('POST', schemaBody(body, SCHEMA_RELATION_KEYS)),
);
export const createSchemaAttribute = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/attributes'),
    jsonBody('POST', schemaBody(body, SCHEMA_ATTRIBUTE_KEYS)),
);
export const createSchemaConstraint = (slug, versionId, body) => request(
    schemaPath(slug, versionId, '/constraints'),
    jsonBody('POST', schemaBody(body, SCHEMA_CONSTRAINT_KEYS)),
);
export const updateSchemaItem = (slug, versionId, kind, itemId, body) => {
    const keys = ({
        'entity-types': SCHEMA_ENTITY_KEYS,
        'relation-types': SCHEMA_RELATION_KEYS,
        attributes: SCHEMA_ATTRIBUTE_KEYS,
        constraints: SCHEMA_CONSTRAINT_KEYS,
    })[kind] || SCHEMA_COMMON_KEYS;
    return request(
        schemaPath(slug, versionId, `/${kind}/${itemId}`),
        jsonBody('PATCH', schemaBody(body, keys)),
    );
};
export const disableSchemaItem = (slug, versionId, kind, itemId, body) => request(
    schemaPath(slug, versionId, `/${kind}/${itemId}/disable`),
    jsonBody('POST', schemaBody(body, SCHEMA_COMMON_KEYS)),
);

export const health = (forceRefresh) => cachedRequest('health', () => request('/health'), 60000, forceRefresh);
