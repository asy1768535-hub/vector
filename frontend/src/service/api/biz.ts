import { request } from '../request';

// ── Libraries (admin) ────────────────────────────────────────
export function fetchLibraries(params?: any) {
  return request<any>({ url: '/admin/libraries', params });
}
export function createLibrary(data: any) {
  return request<any>({ url: '/admin/libraries', method: 'post', data });
}
export function updateLibrary(slug: string, data: any) {
  return request<any>({ url: `/admin/libraries/${slug}`, method: 'patch', data });
}
export function deleteLibrary(slug: string) {
  return request<any>({ url: `/admin/libraries/${slug}`, method: 'delete' });
}
export function rebuildLibrary(slug: string) {
  return request<any>({ url: `/admin/libraries/${slug}/rebuild-collection`, method: 'post' });
}

// ── Users (admin) ────────────────────────────────────────────
export function fetchUsers(params?: any) {
  return request<any>({ url: '/admin/users', params });
}
export function createUser(data: any) {
  return request<any>({ url: '/admin/users', method: 'post', data });
}
export function updateUser(id: string, data: any) {
  return request<any>({ url: `/admin/users/${id}`, method: 'patch', data });
}
export function disableUser(id: string) {
  return request<any>({ url: `/admin/users/${id}`, method: 'delete' });
}

// ── Permissions (admin) ──────────────────────────────────────
export function fetchUserPerms(userId: string) {
  return request<any>({ url: '/admin/permissions', params: { user_id: userId } });
}
export function grantPerms(data: any) {
  return request<any>({ url: '/admin/permissions', method: 'put', data });
}
export function revokePerms(data: any) {
  return request<any>({ url: '/admin/permissions', method: 'delete', data });
}

// ── Documents / query (per library) ──────────────────────────
export function fetchDocuments(slug: string, params?: any) {
  return request<any>({ url: `/libraries/${slug}/documents`, params });
}
export function ingestDocument(slug: string, data: any) {
  return request<any>({ url: `/libraries/${slug}/documents`, method: 'post', data });
}
export function deleteDocument(slug: string, id: string) {
  return request<any>({ url: `/libraries/${slug}/documents/${id}`, method: 'delete' });
}
export function fetchLibraryStats(slug: string) {
  return request<any>({ url: `/libraries/${slug}/stats` });
}
export function queryLibrary(slug: string, data: any) {
  return request<any>({ url: `/libraries/${slug}/query`, method: 'post', data });
}
export function importFile(slug: string, file: File) {
  const fd = new FormData();
  fd.append('file', file);
  return request<any>({ url: `/libraries/${slug}/import-file`, method: 'post', data: fd });
}

// ── API keys (self) ──────────────────────────────────────────
export function fetchApiKeys() {
  return request<any>({ url: '/me/api-keys' });
}
export function createApiKey(name: string, expiresAt: string | null = null) {
  return request<any>({ url: '/me/api-keys', method: 'post', data: { name, expires_at: expiresAt } });
}
export function revokeApiKey(id: string) {
  return request<any>({ url: `/me/api-keys/${id}`, method: 'delete' });
}

// ── Jobs (admin) ─────────────────────────────────────────────
export function fetchJobs(params?: any) {
  return request<any>({ url: '/admin/jobs', params });
}
export function retryJob(id: string) {
  return request<any>({ url: `/admin/jobs/${id}/retry`, method: 'post' });
}

// ── Audit (admin) ────────────────────────────────────────────
export function fetchAuditLog(params?: any) {
  return request<any>({ url: '/admin/audit-log', params });
}
