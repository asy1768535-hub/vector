// 普通用户菜单 / 路由可见性的纯逻辑（便于单测；前端隐藏不替代后端鉴权）。
//   - 有 read：文档、数据检索、智能问答
//   - 有 insert：导入数据
//   - API Key：始终显示
//   - superuser：全部可见
// permissions 形如 [{ library_slug, actions:[...], library_name? }]，任一库具备某动作即视为有该动作。

function actionSet(user, permissions) {
    const acts = new Set();
    if (Array.isArray(permissions)) {
        for (const p of permissions) for (const a of (p && p.actions) || []) acts.add(a);
    }
    return acts;
}

export function hasOrganizationAdmin(organizations) {
    return Array.isArray(organizations)
        && organizations.some((item) => item?.role === 'organization_admin');
}

export function menuAccess(user, permissions, organizations = []) {
    const isSuper = !!(user && user.is_superuser);
    const acts = actionSet(user, permissions);
    const can = (a) => isSuper || acts.has(a);
    const organizationAdmin = hasOrganizationAdmin(organizations);
    return {
        // Catalog requires Organization authorization; platform superuser is not a
        // customer-content bypass. Organization admins receive effective read rows.
        catalog: acts.has('read'),
        documents: can('read'),
        search: can('read'),
        chat: can('read'),
        import: can('insert'),
        organizationAdmin,
        retrievalTest: organizationAdmin,
        apiKeys: true,            // 始终显示
    };
}

// 路由级权限：perm 为该路由所需动作（'read' / 'insert'）；无 perm 要求则放行。
export function canAccessRoute(user, permissions, perm) {
    if (!perm) return true;
    if (user && user.is_superuser) return true;
    return actionSet(user, permissions).has(perm);
}

export function canAccessEffectiveRoute(permissions, action) {
    return actionSet(null, permissions).has(action);
}

export function canAccessOrganizationRoute(organizations) {
    return hasOrganizationAdmin(organizations);
}

// 普通用户的可读库列表（按 read 过滤），元素 {slug, name}。superuser 走 listLibraries，不用这里。
export function readableLibraries(permissions) {
    if (!Array.isArray(permissions)) return [];
    return permissions
        .filter((p) => ((p && p.actions) || []).includes('read'))
        .map((p) => ({ slug: p.library_slug, name: p.library_name || p.library_slug }));
}

export function canManageLibrary(permissions, organizations, librarySlug) {
    const slug = String(librarySlug || '');
    if (!slug || !Array.isArray(permissions)) return false;
    const permission = permissions.find((item) => item?.library_slug === slug);
    if (!permission) return false;
    if ((permission.actions || []).includes('admin')) return true;
    const organizationId = String(permission.organization_id || '');
    return Boolean(organizationId && (organizations || []).some((item) => (
        String(item?.organization_id || '') === organizationId
        && item?.role === 'organization_admin'
    )));
}

// 选默认 / 纠正选中 slug：当前 slug 仍在可见库中则保留，否则取第一个，没有可见库则 null。
export function resolveSelectedSlug(current, libs) {
    const slugs = (libs || []).map((l) => l && l.slug);
    if (current && slugs.includes(current)) return current;
    return slugs.length ? slugs[0] : null;
}
