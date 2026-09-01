// ── Action labels (shared by Dashboard & Audit) ───────────
export const ACTION_LABELS = {
    'permission.grant': '授权权限',
    'permission.revoke': '撤销权限',
    'library.create': '创建知识库',
    'library.update': '修改知识库',
    'library.delete': '删除知识库',
    'library.rebuild_collection': '重建向量索引',
    'library.faq.create': '新增常用问题',
    'library.faq.update': '修改常用问题',
    'library.faq.delete': '删除常用问题',
    'user.create': '创建用户',
    'user.update': '修改用户',
    'user.disable': '禁用用户',
    'user.password_reset': '重置用户密码',
    'job.retry': '重试任务',
    'job.reset_failed': '重试失败的向量任务',
    'document.create': '上传文档',
    'document.update': '更新文档',
    'document.delete': '删除文档',
    'document.restore': '恢复文档',
    'api_key.create': '创建 API 密钥',
    'api_key.revoke': '撤销 API 密钥',
    'schema_lifecycle.version_imported': '导入知识结构版本',
    'schema_lifecycle.imported': '导入知识结构版本',
    'schema_lifecycle.version_created': '创建知识结构版本',
    'schema_lifecycle.activated': '启用知识结构版本',
    'schema_lifecycle.deprecated': '停用知识结构版本',
    'graph_publication.active': '激活图谱发布版本',
    'graph_publication.superseded': '停用旧图谱版本',
    'graph_publication.cancelled': '取消图谱发布版本',
    'graph_publication.failed': '图谱发布失败',
    'graph_publication.degraded': '图谱发布版本降级',
    'graph_governance.action_staged': '提交图谱治理操作',
    'graph_governance.action_decided': '处理图谱治理操作',
    'graph_governance.action_cancelled': '取消图谱治理操作',
    'graph_governance.publication_planned': '生成图谱发布计划',
    'graph_governance.publication_applied': '应用图谱发布计划',
    'classification.run_record': '完成文档自动分类',
    'classification.run_failed': '文档自动分类失败',
    'classification.review_apply': '确认文档分类',
    'classification.review_reject': '驳回文档分类',
    'classification.manual_set': '修改文档分类',
    'classification.manual_remove': '移除文档分类',
    'classification.taxonomy_create': '创建分类结构版本',
    'classification.taxonomy_update': '修改分类结构版本',
    'classification.taxonomy_copy': '复制分类结构版本',
    'classification.taxonomy_activate': '启用分类结构版本',
    'classification.label_create': '新增分类',
    'classification.label_update': '修改分类',
    'classification.library_labels_replace': '更新知识库分类范围',
    'classification.taxonomy_bootstrap': '生成分类结构',
    'classification.taxonomy_bootstrap_publish': '发布分类结构',
    'classification.taxonomy_bootstrap_failed': '分类结构生成失败',
};

export const PERM_LABELS = {
    read: '读取', insert: '写入', delete: '删除',
    admin: '管理', submit: '提交', review: '审核',
};

export function actionLabel(code) {
    return ACTION_LABELS[code] || '系统操作';
}

export function permActions(actions) {
    if (!actions) return '';
    const arr = Array.isArray(actions) ? actions : [actions];
    return arr.map((a) => PERM_LABELS[a] || a).join('、');
}

export function shortAuditId(value) {
    const text = String(value || '');
    return /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(text)
        ? `${text.slice(0, 8)}…${text.slice(-4)}`
        : text;
}

function targetName(t) {
    return t?.library_name || t?.document_title || t?.user_name || t?.version_name
        || t?.title || t?.name || t?.email || t?.slug || t?.library_slug || '';
}

export function targetSummary(action, t) {
    if (!t) return '';
    const name = targetName(t) || shortAuditId(
        t.publication_id || t.ontology_version_id || t.document_id || t.user_id || t.job_id || t.id,
    );
    switch (action) {
        case 'permission.grant':
            return `授予 ${t.email || shortAuditId(t.user_id)} 在 ${t.library_name || t.library_slug || '知识库'}的 ${permActions(t.actions)} 权限`;
        case 'permission.revoke':
            return `撤销 ${t.email || shortAuditId(t.user_id)} 在 ${t.library_name || t.library_slug || '知识库'}的 ${permActions(t.actions)} 权限`;
        case 'library.create':
            return `创建知识库 ${name}`;
        case 'library.update':
            return `更新了知识库 ${name}`;
        case 'library.delete':
            return `删除知识库 ${name}`;
        case 'user.create':
            return `创建用户 ${t.email}`;
        case 'schema_lifecycle.version_imported':
        case 'schema_lifecycle.imported':
            return `导入了知识结构版本 ${t.version_key || t.version || name || '—'}`;
        case 'graph_publication.active':
            return `激活了图谱发布版本，包含 ${t.entity_count || 0} 个实体、${t.relation_count || 0} 条关系`;
        case 'graph_publication.superseded':
            return '停用了旧图谱版本';
        case 'graph_publication.failed':
            return '图谱发布失败，请查看技术详情';
        case 'classification.run_record':
            return `完成了文档 ${name || '—'} 的自动分类`;
        case 'classification.run_failed':
            return `文档 ${name || '—'} 自动分类失败`;
        default:
            return name ? `${actionLabel(action)}：${name}` : '记录了一次系统操作';
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
    { prefix: 'schema_lifecycle', type: '知识结构', icon: 'sidebar:ontology' },
    { prefix: 'graph_publication', type: '发布版本', icon: 'sidebar:graph' },
    { prefix: 'graph_governance', type: '图谱治理', icon: 'sidebar:graph' },
    { prefix: 'classification', type: '文档分类', icon: 'sidebar:document' },
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
    const name = targetName(t);
    if (name) return name;
    return shortAuditId(t.publication_id || t.ontology_version_id || t.user_id
        || t.job_id || t.document_id || t.id || '');
}

export function actorDisplay(log, users = [], currentUser = null) {
    const id = String(log?.actor_user_id || '');
    const direct = log?.actor_username || log?.actor_email || log?.actor_name;
    if (direct) return direct;
    const user = users.find((item) => String(item.id) === id)
        || (String(currentUser?.id || '') === id ? currentUser : null);
    return user?.username || user?.email || user?.name || shortAuditId(id) || '系统';
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
