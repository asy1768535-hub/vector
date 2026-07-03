import { documentTypeIcon } from './documents_ui.js';

// ── Constants ──────────────────────────────────────────────
export const ALLOWED_EXTENSIONS = new Set([
    '.txt', '.md', '.markdown', '.json', '.csv', '.docx', '.xlsx', '.pdf',
]);

export const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

export const MAX_BATCH_SIZE = 20;

export const LEGACY_SAVE_AS = {
    '.doc': '请另存为 .docx 后再上传',
    '.xls': '请另存为 .xlsx 后再上传',
};

export const OP_LABEL = { created: '新建', updated: '已更新', unchanged: '未变更' };
export const OP_TAG = { created: 'success', updated: 'warning', unchanged: 'info' };

export const ST_LABEL = {
    pending: '等待中', uploading: '上传中', submitted: '已提交',
    skipped: '跳过', failed: '失败', invalid: '无效',
};
export const ST_TAG = {
    pending: 'info', uploading: 'warning', submitted: 'success',
    skipped: 'info', failed: 'danger', invalid: 'danger',
};

// ── Pure helpers ───────────────────────────────────────────

/** Stable dedup key for a File object. */
export function fileKey(file) {
    return `${file.name}|${file.size}|${file.lastModified}`;
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
export function validateFile(file) {
    const ext = (file.name || '').slice(((file.name || '').lastIndexOf('.'))).toLowerCase();

    // 1) Legacy Office formats
    if (LEGACY_SAVE_AS[ext]) {
        return { valid: false, reason: LEGACY_SAVE_AS[ext], failType: 'format' };
    }

    // 2) Extension not allowed
    if (!ALLOWED_EXTENSIONS.has(ext)) {
        return { valid: false, reason: `不支持的文件格式 ${ext || '(无后缀)'}`, failType: 'format' };
    }

    // 3) Size check
    if (file.size > MAX_FILE_SIZE) {
        return { valid: false, reason: `文件超过 50 MB 限制 (${formatSize(file.size)})`, failType: 'size' };
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
export function validateBatch(files, existingKeys) {
    const seen = new Set(existingKeys || []);
    const accepted = [];
    const duplicates = [];
    const invalid = [];
    const totalAllowed = MAX_BATCH_SIZE - (existingKeys ? existingKeys.length : 0);

    for (const file of files) {
        const key = fileKey(file);
        if (seen.has(key)) {
            duplicates.push({ file, reason: '重复文件' });
            continue;
        }
        seen.add(key);

        const result = validateFile(file);
        if (!result.valid) {
            invalid.push({ file, reason: result.reason, failType: result.failType });
            continue;
        }
        if (accepted.length >= totalAllowed) {
            invalid.push({ file, reason: `单批最多 ${MAX_BATCH_SIZE} 个文件`, failType: 'batch' });
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
