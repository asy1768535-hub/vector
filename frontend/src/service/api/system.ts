import { request } from '../request';

/** 健康探活：DB / Qdrant / Embedding */
export function fetchHealth() {
  return request<any>({ url: '/health' });
}

/** 当前用户在各库上的权限（普通用户用于推导可见库） */
export function fetchMyPermissions() {
  return request<any>({ url: '/me/permissions' });
}
