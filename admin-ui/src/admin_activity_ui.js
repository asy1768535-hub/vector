// ── Action labels (shared by Dashboard & Audit) ───────────
export const ACTION_LABELS = {
    'permission.grant': '授权权限',
    'permission.revoke': '撤销权限',
    'library.create': '创建知识库',
    'library.update': '修改知识库',
    'library.delete': '删除知识库',
    'library.faq.create': '新增常用问题',
    'library.faq.update': '修改常用问题',
    'library.faq.delete': '删除常用问题',
    'user.create': '创建用户',
    'user.update': '修改用户',
    'user.disable': '禁用用户',
    'job.retry': '重试任务',
};

export const PERM_LABELS = {
    read: '读取', insert: '写入', delete: '删除',
    admin: '管理', submit: '提交', review: '审核',
};

export function actionLabel(code) {
    return ACTION_LABELS[code] || code;
}

export function permActions(actions) {
    if (!actions) return '';
    const arr = Array.isArray(actions) ? actions : [actions];
    return arr.map((a) => PERM_LABELS[a] || a).join('、');
}

function fmtTarget(t) {
    if (t === null || t === undefined) return '';
    try { return JSON.stringify(t); } catch (_) { return String(t); }
}

export function targetSummary(action, t) {
    if (!t) return '';
    switch (action) {
        case 'permission.grant':
            return `授予 ${t.user_id} 在 ${t.library_slug} 的 ${permActions(t.actions)} 权限`;
        case 'permission.revoke':
            return `撤销 ${t.user_id} 在 ${t.library_slug} 的 ${permActions(t.actions)} 权限`;
        case 'library.create':
            return `创建知识库 ${t.slug}`;
        case 'library.delete':
            return `删除知识库 ${t.slug}`;
        case 'user.create':
            return `创建用户 ${t.email}`;
        default:
            return fmtTarget(t);
    }
}

// ── 审计目标辅助 ──

const ACTION_TYPE_MAP = [
    { prefix: 'library', type: '知识库', icon: 'sidebar:library' },
    { prefix: 'user', type: '用户', icon: 'sidebar:user' },
    { prefix: 'permission', type: '权限', icon: 'sidebar:permission' },
    { prefix: 'job', type: '任务', icon: 'sidebar:task' },
    { prefix: 'document', type: '文档', icon: 'sidebar:document' },
    { prefix: 'api_key', type: 'API Key', icon: 'sidebar:api-key' },
];

/** 根据 action 前缀返回目标类型元信息 */
export function targetTypeMeta(action) {
    if (!action) return { type: '其他', icon: 'sidebar:overview' };
    for (const m of ACTION_TYPE_MAP) {
        if (action.startsWith(m.prefix)) return { type: m.type, icon: m.icon };
    }
    return { type: '其他', icon: 'sidebar:overview' };
}

/** 从 target 对象中提取最佳显示名 */
export function targetDisplay(action, target) {
    if (!target || typeof target !== 'object') return '';
    const t = target;
    return t.title || t.name || t.email || t.slug || t.library_slug
        || t.user_id || t.job_id || t.document_id || t.id || '';
}

/** ISO time → YYYY-MM-DD HH:mm:ss */
export function fmtAuditTime(at) {
    if (!at) return '';
    const d = new Date(at);
    if (Number.isNaN(d.getTime())) return String(at);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
        + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** ISO time → relative (N 秒前 / N 分前 / N 小时前 / 日期) */
export function relAuditTime(at) {
    if (!at) return '';
    const d = new Date(at);
    if (Number.isNaN(d.getTime())) return String(at);
    const diff = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    return fmtAuditTime(at).slice(0, 10);
}

export function prettyTarget(t) {
    if (t === null || t === undefined) return '';
    try { return JSON.stringify(t, null, 2); } catch (_) { return String(t); }
}

/** 从 action 提取目标类型 key（用于筛选），如 library/user/permission */
export function targetTypeKey(action) {
    if (!action) return 'other';
    for (const m of ACTION_TYPE_MAP) {
        if (action.startsWith(m.prefix)) return m.prefix;
    }
    return 'other';
}

/** 返回 action 对应的语义色调 CSS 类名（用于标签颜色） */
export function actionTone(action) {
    if (!action) return 'action-tone--other';
    const verb = action.split('.').pop() || '';
    if (verb === 'create' || verb === 'grant') return 'action-tone--create';
    if (verb === 'update' || verb === 'retry') return 'action-tone--update';
    if (verb === 'delete' || verb === 'revoke' || verb === 'disable') return 'action-tone--delete';
    return 'action-tone--other';
}
