// 极简全局状态：当前用户 + 权限缓存。
import { reactive } from 'vue';
import * as api from './api.js';

export const store = reactive({
    user: null,            // { id, email, is_superuser, username, ... } 或 null
    permissions: [],       // [{ library_slug, actions: [] }]
    ready: false,          // 首次加载完成？
});

export async function refreshAuth() {
    // 直接 fetch，避免 401 触发全局 onUnauthorized 跳路由（初次启动时 router 守卫已经在处理导航）
    try {
        const resp = await fetch('/users/me', { credentials: 'include' });
        if (resp.ok) {
            store.user = await resp.json();
            try {
                const pResp = await fetch('/me/permissions', { credentials: 'include' });
                store.permissions = pResp.ok ? await pResp.json() : [];
            } catch (_) {
                store.permissions = [];
            }
        } else {
            store.user = null;
            store.permissions = [];
        }
    } catch (_) {
        store.user = null;
        store.permissions = [];
    } finally {
        store.ready = true;
    }
}

export function hasPermission(slug, action) {
    if (!store.user) return false;
    if (store.user.is_superuser) return true;
    const row = store.permissions.find((p) => p.library_slug === slug);
    return !!row && row.actions.includes(action);
}
