import { fileKey, validateFile } from './import_ui.js';

export const BATCH_REPLACE_MATCH_LABEL = {
    matched: '已匹配',
    unmatched: '未匹配',
    multiple: '多候选',
};

export const BATCH_REPLACE_MATCH_TAG = {
    matched: 'success',
    unmatched: 'info',
    multiple: 'warning',
};

export const BATCH_REPLACE_STATUS_LABEL = {
    pending: '待提交',
    uploading: '提交中',
    submitted: '已提交',
    failed: '失败',
    skipped: '跳过',
};

export const BATCH_REPLACE_STATUS_TAG = {
    pending: 'info',
    uploading: 'warning',
    submitted: 'success',
    failed: 'danger',
    skipped: 'info',
};

export function normalizeDocumentName(name) {
    return String(name || '')
        .replace(/　/g, ' ')
        .replace(/[（﹙]/g, '(')
        .replace(/[）﹚]/g, ')')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
}

export function matchDocumentsForFile(file, docs) {
    const fileName = file?.name || '';
    const sourceDocs = docs || [];
    const exact = sourceDocs.filter((doc) => (doc.title || '') === fileName);
    if (exact.length) {
        return {
            matchStatus: exact.length === 1 ? 'matched' : 'multiple',
            matchType: 'exact',
            candidates: exact,
            matchedDocId: exact.length === 1 ? exact[0].id : null,
        };
    }

    const normalizedFileName = normalizeDocumentName(fileName);
    const normalized = sourceDocs.filter((doc) => normalizeDocumentName(doc.title || '') === normalizedFileName);
    return {
        matchStatus: normalized.length === 1 ? 'matched' : normalized.length > 1 ? 'multiple' : 'unmatched',
        matchType: normalized.length ? 'normalized' : 'none',
        candidates: normalized,
        matchedDocId: normalized.length === 1 ? normalized[0].id : null,
    };
}

export function createBatchReplaceItems(files, docs, existingKeys = []) {
    const seen = new Set(existingKeys || []);
    const items = [];
    for (const file of Array.from(files || [])) {
        const key = fileKey(file);
        if (seen.has(key)) continue;
        seen.add(key);

        const validation = validateFile(file);
        const match = matchDocumentsForFile(file, docs);
        items.push({
            _key: key,
            file,
            name: file.name,
            size: file.size,
            validationStatus: validation.valid ? 'valid' : 'invalid',
            validationError: validation.valid ? '' : validation.reason,
            _failType: validation.failType || '',
            matchStatus: match.matchStatus,
            matchType: match.matchType,
            candidates: match.candidates,
            matchedDocId: match.matchedDocId,
            status: 'pending',
            error: '',
        });
    }
    return items;
}

export function setBatchReplaceTarget(item, documentId, docs) {
    item.matchedDocId = documentId || null;
    if (documentId) {
        item.matchStatus = 'matched';
        item.matchType = 'manual';
        item.candidates = (docs || []).filter((doc) => String(doc.id) === String(documentId));
    } else {
        const match = matchDocumentsForFile(item.file, docs);
        item.matchStatus = match.matchStatus;
        item.matchType = match.matchType;
        item.candidates = match.candidates;
        item.matchedDocId = match.matchedDocId;
    }
    item.error = '';
}

export function isBatchReplaceSubmittable(item) {
    return Boolean(
        item &&
        item.file &&
        item.validationStatus === 'valid' &&
        item.matchStatus === 'matched' &&
        item.matchedDocId &&
        (item.status === 'pending' || item.status === 'failed')
    );
}

export function submittableBatchReplaceItems(items) {
    return (items || []).filter(isBatchReplaceSubmittable);
}

export async function submitBatchReplaceItems(
    items,
    slug,
    uploadFn,
    humanize = (e) => e?.message || String(e),
    uploadOptions = {},
) {
    const eligible = submittableBatchReplaceItems(items);
    const eligibleKeys = new Set(eligible.map((item) => item._key));

    for (const item of items || []) {
        if (!eligibleKeys.has(item._key) && item.status === 'pending') {
            item.status = 'skipped';
            if (!item.error) item.error = '未唯一匹配或文件校验未通过，已跳过';
        }
    }

    for (const item of eligible) {
        item.status = 'uploading';
        item.error = '';
        try {
            item.importResponse = await uploadFn(slug, item.file, {
                ...uploadOptions,
                replaceDocumentId: item.matchedDocId,
            });
            item.status = 'submitted';
        } catch (e) {
            item.status = 'failed';
            item.error = humanize(e);
        }
    }

    return {
        submitted: (items || []).filter((item) => item.status === 'submitted').length,
        failed: (items || []).filter((item) => item.status === 'failed').length,
        skipped: (items || []).filter((item) => item.status === 'skipped').length,
    };
}
