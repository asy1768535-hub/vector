// 后端 REST 客户端。所有调用走同源 + cookie；自动处理 401 跳登录。

const BASE = '';  // 同源

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

async function request(path, options = {}) {
    const resp = await fetch(BASE + path, {
        credentials: 'include',
        ...options,
    });
    if (resp.status === 401) {
        if (onUnauthorized) onUnauthorized();
        throw new Error('Unauthorized');
    }
    if (resp.status === 204) return null;
    const ct = resp.headers.get('content-type') || '';
    const body = ct.includes('application/json') ? await resp.json() : await resp.text();
    if (!resp.ok) {
        const msg = (body && body.detail) ? body.detail : (typeof body === 'string' ? body : JSON.stringify(body));
        const err = new Error(msg || `HTTP ${resp.status}`);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
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
        throw new Error(j.detail || '用户名或密码错误');
    }
    if (!resp.ok) throw new Error(`登录失败: HTTP ${resp.status}`);
    return resp.status === 204 ? null : resp.json();
}

export async function logout() {
    await fetch(BASE + '/auth/jwt/logout', { method: 'POST', credentials: 'include' });
}

export const me = () => request('/users/me');
export const myPermissions = () => request('/me/permissions');

// ── API Keys 自助 ────────────────────────────────────────────
export const listApiKeys = () => request('/me/api-keys');
export const createApiKey = (name, expiresAt = null) =>
    request('/me/api-keys', jsonBody('POST', { name, expires_at: expiresAt }));
export const revokeApiKey = (id) => request(`/me/api-keys/${id}`, { method: 'DELETE' });

// ── Admin: Users ─────────────────────────────────────────────
export const listUsers = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request('/admin/users' + (qs ? '?' + qs : ''));
};
export const createUser = (data) => request('/admin/users', jsonBody('POST', data));
export const updateUser = (id, data) => request(`/admin/users/${id}`, jsonBody('PATCH', data));
export const disableUser = (id) => request(`/admin/users/${id}`, { method: 'DELETE' });

// ── Admin: Libraries ─────────────────────────────────────────
export const listLibraries = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request('/admin/libraries' + (qs ? '?' + qs : ''));
};
export const createLibrary = (data) => request('/admin/libraries', jsonBody('POST', data));
export const updateLibrary = (slug, data) => request(`/admin/libraries/${slug}`, jsonBody('PATCH', data));
export const deleteLibrary = (slug) => request(`/admin/libraries/${slug}`, { method: 'DELETE' });
export const rebuildLibraryCollection = (slug) =>
    request(`/admin/libraries/${slug}/rebuild-collection`, { method: 'POST' });
export const testLibraryEmbedding = (slug) =>
    request(`/admin/libraries/${slug}/test-embedding`, { method: 'POST' });

// ── Admin: Permissions ───────────────────────────────────────
export const listUserPerms = (user_id) => request(`/admin/permissions?user_id=${user_id}`);
export const grantPerms = (data) => request('/admin/permissions', jsonBody('PUT', data));
export const revokePerms = (data) => request('/admin/permissions', jsonBody('DELETE', data));

// ── Documents（library 维度） ─────────────────────────────────
export const ingestDocument = (slug, data) =>
    request(`/libraries/${slug}/documents`, jsonBody('POST', data));
export const updateDocument = (slug, id, data) =>
    request(`/libraries/${slug}/documents/${id}`, jsonBody('PUT', data));
export const listDocuments = (slug, params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/libraries/${slug}/documents` + (qs ? '?' + qs : ''));
};
export const deleteDocument = (slug, id) =>
    request(`/libraries/${slug}/documents/${id}`, { method: 'DELETE' });
export const libraryStats = (slug) => request(`/libraries/${slug}/stats`);
export const queryLibrary = (slug, data) =>
    request(`/libraries/${slug}/query`, jsonBody('POST', data));
export const importFile = (slug, file, { externalId = null, replaceDocumentId = null } = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    // 选填：带 external_id 时，库内同键文档会被覆盖更新（upsert），而非新建
    if (externalId) formData.append('external_id', externalId);
    // 选填：替换模式——按 document ID 覆盖目标文档（不依赖 external_id）
    if (replaceDocumentId) formData.append('replace_document_id', replaceDocumentId);
    return request(`/libraries/${slug}/import-file`, {
        method: 'POST',
        body: formData,
    });
};
// 任务状态（库级，普通用户可查）：单查 + 按文档列出
export const getLibraryJob = (slug, jobId) =>
    request(`/libraries/${slug}/jobs/${jobId}`);
export const listDocumentJobs = (slug, documentId) =>
    request(`/libraries/${slug}/documents/${documentId}/jobs`);

// ── Admin: Jobs ──────────────────────────────────────────────
export const listJobs = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request('/admin/jobs' + (qs ? '?' + qs : ''));
};
export const retryJob = (id) => request(`/admin/jobs/${id}/retry`, { method: 'POST' });
export const jobStats = () => request('/admin/jobs/stats');
export const resetFailedJobs = (libraryId = null) =>
    request('/admin/jobs/reset-failed' + (libraryId ? `?library_id=${libraryId}` : ''), { method: 'POST' });

// ── Admin: Audit log ─────────────────────────────────────────
export const listAudit = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request('/admin/audit-log' + (qs ? '?' + qs : ''));
};

// ── Health ───────────────────────────────────────────────────
export const health = () => request('/health');
