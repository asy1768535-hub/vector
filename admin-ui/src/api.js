// 后端 REST 客户端。所有调用走同源 + cookie；自动处理 401 跳登录。

import { humanizeApiError } from './api_errors.js';
import { cachedRequest, clearCache, invalidateByPrefix } from './request_cache.js';

const BASE = '';  // 同源

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

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
        throw err;
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

export const me = (forceRefresh) => cachedRequest('me', () => request('/users/me'), 30000, forceRefresh);
export const myPermissions = (forceRefresh) => cachedRequest('myPermissions', () => request('/me/permissions'), 30000, forceRefresh);
// 修改自己的资料/密码（复用 fastapi-users PATCH /users/me，不新建更新逻辑）
export const updateMe = (data) => request('/users/me', jsonBody('PATCH', data));
// 管理员重置某用户密码（专用端点；password 不混入普通 PATCH）
export const adminResetUserPassword = (userId, password) =>
    request(`/admin/users/${userId}/reset-password`, jsonBody('POST', { password }));

// ── API Keys 自助 ────────────────────────────────────────────
export const listApiKeys = (forceRefresh) => cachedRequest('listApiKeys', () => request('/me/api-keys'), 15000, forceRefresh);
export const createApiKey = (name, expiresAt = null) =>
    request('/me/api-keys', jsonBody('POST', { name, expires_at: expiresAt }));
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

// ── Documents（library 维度） ─────────────────────────────────
export const ingestDocument = (slug, data) =>
    request(`/libraries/${slug}/documents`, jsonBody('POST', data));
export const updateDocument = (slug, id, data) =>
    request(`/libraries/${slug}/documents/${id}`, jsonBody('PUT', data));
const _docsKey = (slug, params) => `listDocuments:${slug}:${new URLSearchParams(params).toString()}`;
export const listDocuments = (slug, params = {}, forceRefresh) => cachedRequest(_docsKey(slug, params), () => request(`/libraries/${slug}/documents?${new URLSearchParams(params).toString()}`), 15000, forceRefresh);
export const deleteDocument = (slug, id) =>
    request(`/libraries/${slug}/documents/${id}`, { method: 'DELETE' });
export const libraryStats = (slug, forceRefresh) => cachedRequest(`libraryStats:${slug}`, () => request(`/libraries/${slug}/stats`), 15000, forceRefresh);
export const queryLibrary = (slug, data) =>
    request(`/libraries/${slug}/query`, jsonBody('POST', data));
export const importFile = (slug, file, { externalId = null, replaceDocumentId = null } = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    if (externalId) formData.append('external_id', externalId);
    if (replaceDocumentId) formData.append('replace_document_id', replaceDocumentId);
    return request(`/libraries/${slug}/import-file`, { method: 'POST', body: formData });
};
// 任务状态（库级，普通用户可查）：按文档列出
export const listDocumentJobs = (slug, documentId, forceRefresh) => cachedRequest(`listDocumentJobs:${slug}:${documentId}`, () => request(`/libraries/${slug}/documents/${documentId}/jobs`), 15000, forceRefresh);
export const getDocumentSource = (slug, documentId, chunkId) =>
    request(`/libraries/${slug}/documents/${documentId}/source?` + new URLSearchParams({ chunk_id: chunkId }).toString());
export const getDocumentFullSource = (slug, documentId) =>
    request(`/libraries/${slug}/documents/${documentId}/source/full`);
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
    'status',
    'classification_state',
    'label_id',
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

// ── Admin: Jobs ──────────────────────────────────────────────
const _jobsKey = (params) => 'listJobs:' + new URLSearchParams(params).toString();
export const listJobs = (params = {}, forceRefresh) => cachedRequest(_jobsKey(params), () => request('/admin/jobs?' + new URLSearchParams(params).toString()), 10000, forceRefresh);
export const retryJob = (id) => request(`/admin/jobs/${id}/retry`, { method: 'POST' });
export const jobStats = (forceRefresh) => cachedRequest('jobStats', () => request('/admin/jobs/stats'), 10000, forceRefresh);
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
export const getChatConversationMessages = (id) => request(`/chat/conversations/${id}/messages`); // not cached — real-time
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
export const health = (forceRefresh) => cachedRequest('health', () => request('/health'), 60000, forceRefresh);
