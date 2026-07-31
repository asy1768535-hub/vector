import { documentTypeIcon } from './documents_ui.js';

// ── Constants ──────────────────────────────────────────────
export const ALLOWED_EXTENSIONS = new Set([
    '.txt', '.md', '.markdown', '.json', '.csv', '.docx', '.xlsx', '.pdf',
]);

export const MAX_FILE_SIZE = 500 * 1024 * 1024;

export const MAX_BATCH_SIZE = 1000;

export const SECURITY_LEVEL_LABEL = {
    public: '公开',
    internal: '内部',
    confidential: '机密',
    restricted: '受限',
    secret: '秘密',
};

export function securityLevelLabel(value) {
    const level = String(value || '').trim();
    if (!level) return '未设置';
    return SECURITY_LEVEL_LABEL[level] || `${level}（自定义安全级别）`;
}

export const LEGACY_SAVE_AS = {
    '.doc': '请另存为 .docx 后再上传',
    '.xls': '请另存为 .xlsx 后再上传',
};

export const OP_LABEL = { created: '新建', updated: '已更新', unchanged: '未变更' };
export const OP_TAG = { created: 'success', updated: 'warning', unchanged: 'info' };

export const ST_LABEL = {
    pending: '等待中', uploading: '上传中', processing: '处理中', submitted: '已提交',
    skipped: '跳过', failed: '失败', invalid: '无效',
};
export const ST_TAG = {
    pending: 'info', uploading: 'warning', processing: 'primary', submitted: 'success',
    skipped: 'info', failed: 'danger', invalid: 'danger',
};

// ── Pure helpers ───────────────────────────────────────────

/** Stable dedup key for a File object. */
export const GRAPH_STAGE_LABEL = {
    preparing: '准备抽取',
    building_context: '构建上下文',
    extracting: '抽取实体和关系',
    parsing: '解析模型结果',
    binding_evidence: '绑定原文证据',
    aggregating: '合并候选知识',
    validating: '校验实体和关系',
    scoring: '计算置信度',
    materializing: '生成图谱草稿',
    finalizing: '完成收尾',
};

const GRAPH_STAGE_PROGRESS = {
    preparing: 34,
    building_context: 40,
    extracting: 50,
    parsing: 62,
    binding_evidence: 70,
    aggregating: 76,
    validating: 82,
    scoring: 87,
    materializing: 93,
    finalizing: 98,
};

const GRAPH_TERMINAL_STATUSES = new Set([
    'partially_succeeded', 'succeeded', 'failed', 'cancelled', 'superseded',
]);

export function graphJobProgress(job) {
    const status = String(job?.status || 'queued');
    const stage = job?.current_stage || 'preparing';
    const materialization = job?.statistics?.materialization || {};
    const candidates = job?.statistics?.candidate_pipeline || {};
    const entityCount = materialization.entity_count ?? candidates.entity_candidate_count ?? null;
    const relationCount = materialization.relation_count ?? candidates.relation_candidate_count ?? null;

    if (status === 'succeeded') {
        return {
            status, terminal: true, progress: 100, tone: 'success',
            label: '知识图谱构建完成', entityCount, relationCount,
        };
    }
    if (status === 'partially_succeeded') {
        return {
            status, terminal: true, progress: 100, tone: 'warning',
            label: '知识图谱部分完成', entityCount, relationCount,
        };
    }
    if (GRAPH_TERMINAL_STATUSES.has(status)) {
        const label = status === 'failed'
            ? '知识图谱构建失败'
            : status === 'cancelled' ? '知识图谱构建已取消' : '知识图谱任务已被新任务替代';
        return {
            status, terminal: true, progress: 100, tone: 'exception',
            label, entityCount, relationCount,
        };
    }
    return {
        status, terminal: false, progress: GRAPH_STAGE_PROGRESS[stage] ?? 32,
        tone: 'primary', label: GRAPH_STAGE_LABEL[stage] || '知识图谱构建中',
        entityCount, relationCount,
    };
}

export function graphProgressDetail(progress) {
    if (!progress) return '';
    const counts = [];
    if (progress.entityCount !== null && progress.entityCount !== undefined) {
        counts.push(`实体 ${progress.entityCount}`);
    }
    if (progress.relationCount !== null && progress.relationCount !== undefined) {
        counts.push(`关系 ${progress.relationCount}`);
    }
    return counts.length ? `${progress.label} · ${counts.join('，')}` : progress.label;
}

export function fileKey(file) {
    const relativePath = String(file.webkitRelativePath || '').replace(/\\/g, '/');
    return `${relativePath || file.name}|${file.size}|${file.lastModified}`;
}

/** Human-readable file size (binary units). */
export function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

/**
 * Validate a single File object.
 * Returns { valid: true } or { valid: false, reason: string }.
 */
export function validateFile(file, configuration = null) {
    const ext = (file.name || '').slice(((file.name || '').lastIndexOf('.'))).toLowerCase();
    const allowedExtensions = configuration?.allowed_extensions
        ? new Set(configuration.allowed_extensions)
        : ALLOWED_EXTENSIONS;
    const maxFileBytes = configuration?.max_file_bytes || MAX_FILE_SIZE;

    // 1) Legacy Office formats
    if (LEGACY_SAVE_AS[ext]) {
        return { valid: false, reason: LEGACY_SAVE_AS[ext], failType: 'format' };
    }

    // 2) Extension not allowed
    if (!allowedExtensions.has(ext)) {
        return { valid: false, reason: `不支持的文件格式 ${ext || '(无后缀)'}`, failType: 'format' };
    }

    // 3) Size check
    if (file.size > maxFileBytes) {
        return {
            valid: false,
            reason: `文件超过 ${formatSize(maxFileBytes)} 限制 (${formatSize(file.size)})`,
            failType: 'size',
        };
    }

    return { valid: true };
}

/**
 * Validate a batch of files against existing queue keys.
 * Returns { accepted, duplicates, invalid }.
 * - accepted: files that passed validation and fit within batch limit
 * - duplicates: files already in the queue (should be skipped, not shown in list)
 * - invalid: files that failed format/size checks (may be shown in list)
 */
export function validateBatch(files, existingKeys, configuration = null) {
    const seen = new Set(existingKeys || []);
    const accepted = [];
    const duplicates = [];
    const invalid = [];
    const maxBatchSize = configuration?.max_files_per_selection || MAX_BATCH_SIZE;
    const totalAllowed = maxBatchSize - (existingKeys ? existingKeys.length : 0);

    for (const file of files) {
        const key = fileKey(file);
        if (seen.has(key)) {
            duplicates.push({ file, reason: '重复文件' });
            continue;
        }
        seen.add(key);

        const result = validateFile(file, configuration);
        if (!result.valid) {
            invalid.push({ file, reason: result.reason, failType: result.failType });
            continue;
        }
        if (accepted.length >= totalAllowed) {
            invalid.push({
                file,
                reason: `单次最多 ${maxBatchSize} 个文件`,
                failType: 'batch',
            });
            continue;
        }
        accepted.push({ file });
    }

    return { accepted, duplicates, invalid };
}

/**
 * Return the asset path for a file's type icon, or null for unknown types.
 * Delegates to documents_ui.js documentTypeIcon.
 */
export function fileTypeIcon(file) {
    return documentTypeIcon({ title: (file && file.name) || '' });
}
