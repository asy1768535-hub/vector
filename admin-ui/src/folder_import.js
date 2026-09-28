export const DEFAULT_IMPORT_CONFIGURATION = Object.freeze({
    max_file_bytes: 500 * 1024 * 1024,
    chunk_bytes: 32 * 1024 * 1024,
    max_files_per_selection: 1000,
    max_configurable_file_bytes: 50 * 1024 * 1024 * 1024,
    max_configurable_files_per_selection: 100_000,
    upload_concurrency: 1,
    doc_max_file_bytes: 200 * 1024 * 1024,
    accept_all_file_types: true,
    allowed_extensions: [
        '.txt', '.md', '.markdown', '.rst', '.log', '.ini', '.cfg', '.conf',
        '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.csv', '.tsv',
        '.doc', '.docx', '.pptx', '.xls', '.xlsx', '.pdf',
        '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp', '.zip',
        '.mp4', '.mov', '.mkv', '.avi', '.webm',
        '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.opus',
    ],
});

export const IMPORT_PROFILE_DAILY = Object.freeze({
    max_file_bytes: 500 * 1024 * 1024,
    max_files_per_selection: 1000,
});

export const IMPORT_PROFILE_INITIAL = Object.freeze({
    max_file_bytes: 50 * 1024 * 1024 * 1024,
    max_files_per_selection: 100_000,
});

const UPLOAD_MAX_ATTEMPTS = 4;
const RETRYABLE_UPLOAD_CONFLICTS = new Set([
    'upload_busy',
    'upload_claim_lost',
    'upload_offset_mismatch',
]);

function abortReason(signal) {
    if (!signal?.aborted) return null;
    return signal.reason || new DOMException('Upload cancelled', 'AbortError');
}

function retryableUploadError(error) {
    if (error instanceof TypeError) return true;
    if (error?.status === 429) return true;
    if (error?.status >= 500 && error.status <= 599) return true;
    const code = error?.body?.detail?.code || error?.body?.code || error?.code;
    return error?.status === 409 && RETRYABLE_UPLOAD_CONFLICTS.has(code);
}

function retryDelayMs(error, failedAttempt) {
    const retryAfter = Number(error?.retryAfterSeconds);
    if (Number.isFinite(retryAfter) && retryAfter >= 0) {
        return Math.min(retryAfter * 1000, 30_000);
    }
    return Math.min(250 * (2 ** Math.max(0, failedAttempt - 1)), 2_000);
}

async function waitForRetry(delayMs, signal) {
    const aborted = abortReason(signal);
    if (aborted) throw aborted;
    if (delayMs <= 0) return;
    await new Promise((resolve, reject) => {
        let timer;
        const onAbort = () => {
            clearTimeout(timer);
            signal?.removeEventListener('abort', onAbort);
            reject(abortReason(signal));
        };
        timer = setTimeout(() => {
            signal?.removeEventListener('abort', onAbort);
            resolve();
        }, delayMs);
        signal?.addEventListener('abort', onAbort, { once: true });
    });
}

async function withUploadRetries(operation, signal) {
    for (let attempt = 1; attempt <= UPLOAD_MAX_ATTEMPTS; attempt += 1) {
        const aborted = abortReason(signal);
        if (aborted) throw aborted;
        try {
            return await operation();
        } catch (error) {
            const cancelled = abortReason(signal);
            if (cancelled) throw cancelled;
            if (!retryableUploadError(error) || attempt === UPLOAD_MAX_ATTEMPTS) throw error;
            await waitForRetry(retryDelayMs(error, attempt), signal);
        }
    }
    throw new Error('上传重试次数已用尽');
}

function normalizedExtensions(configuration = DEFAULT_IMPORT_CONFIGURATION) {
    return [...new Set((configuration?.allowed_extensions || [])
        .map((value) => String(value || '').trim().toLowerCase())
        .filter((value) => /^\.[a-z0-9]+$/.test(value)))];
}

export function supportedExtensionsAccept(configuration) {
    if (configuration?.accept_all_file_types !== false) return '';
    return normalizedExtensions(configuration).join(',');
}

export function supportedExtensionsLabel(configuration) {
    if (configuration?.accept_all_file_types !== false) {
        return '所有格式（可识别文档解析正文，其他文件生成可检索说明）';
    }
    return normalizedExtensions(configuration).map((value) => value.slice(1)).join('、');
}

export function relativePathForFile(file) {
    const path = String(file?.webkitRelativePath || '').replace(/\\/g, '/').trim();
    if (!path) return null;

    // Browsers occasionally report a folder path whose final segment uses a
    // different filename normalization than File.name.  The server correctly
    // rejects that mismatch; retain the selected folders but make the path
    // identify the file object that will actually be uploaded.
    const fileName = String(file?.name || '').replace(/\\/g, '/').split('/').pop();
    if (!fileName) return path;
    const parts = path.split('/');
    parts[parts.length - 1] = fileName;
    return parts.join('/');
}

export function createImportBatchId(
    cryptoApi = globalThis.crypto,
    randomSource = Math.random,
) {
    if (typeof cryptoApi?.randomUUID === 'function') {
        return cryptoApi.randomUUID.call(cryptoApi);
    }

    const bytes = new Uint8Array(16);
    if (typeof cryptoApi?.getRandomValues === 'function') {
        cryptoApi.getRandomValues(bytes);
    } else {
        for (let index = 0; index < bytes.length; index += 1) {
            bytes[index] = Math.floor(randomSource() * 256);
        }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    return [
        hex.slice(0, 8),
        hex.slice(8, 12),
        hex.slice(12, 16),
        hex.slice(16, 20),
        hex.slice(20),
    ].join('-');
}

export function createImportBatchIds(items, batchId = createImportBatchId()) {
    return new Map(items.map((item) => [item, batchId]));
}

export async function createImportSessionForFile({
    api,
    slug,
    file,
    batchId,
    options = {},
    resumeState = {},
    signal,
}) {
    const stableSlug = resumeState.slug || slug;
    const stableBatchId = resumeState.batchId || batchId;
    resumeState.slug = stableSlug;
    resumeState.batchId = stableBatchId;
    const sessionPayload = {
        batch_id: stableBatchId,
        file_name: file.name,
        relative_path: options.replaceDocumentId ? null : relativePathForFile(file),
        content_type: file.type || null,
        size_bytes: file.size,
        last_modified_millis: file.lastModified || null,
        external_id: options.externalId || null,
        replace_document_id: options.replaceDocumentId || null,
        security_level: options.securityLevel || null,
        graph_extraction_requested: Boolean(options.graphExtractionRequested),
    };
    let session = resumeState.session;
    if (!session) {
        try {
            session = await withUploadRetries(
                () => api.createImportSession(stableSlug, sessionPayload, { signal }),
                signal,
            );
        } catch (error) {
            if (error && typeof error === 'object') {
                error.uploadFileName = file.name;
                error.uploadRelativePath = sessionPayload.relative_path || file.name;
            }
            throw error;
        }
        resumeState.session = session;
    }
    return session;
}

export async function uploadFileInChunks({
    api,
    slug,
    file,
    batchId,
    configuration = DEFAULT_IMPORT_CONFIGURATION,
    options = {},
    onProgress = () => {},
    resumeState = {},
    signal,
}) {
    const session = await createImportSessionForFile({
        api,
        slug,
        file,
        batchId,
        options,
        resumeState,
        signal,
    });
    const stableSlug = resumeState.slug;
    let offset = session.upload_offset;
    if (!Number.isInteger(offset) || offset < 0 || offset > file.size) {
        throw new Error('上传服务返回了无效偏移量');
    }
    let reconciliationAttempts = 0;
    const reportProgress = (phase, job = session) => {
        const progressJob = { ...job, upload_offset: offset };
        resumeState.session = progressJob;
        onProgress({ phase, offset, total: file.size, job: progressJob });
    };
    reportProgress('uploading');
    while (offset < file.size) {
        let advanced = false;
        for (let attempt = 1; attempt <= UPLOAD_MAX_ATTEMPTS; attempt += 1) {
            const aborted = abortReason(signal);
            if (aborted) throw aborted;
            const requestOffset = offset;
            const end = Math.min(requestOffset + configuration.chunk_bytes, file.size);
            try {
                const nextOffset = await api.uploadImportChunk(
                    stableSlug,
                    session.id,
                    file.slice(requestOffset, end),
                    requestOffset,
                    { signal },
                );
                if (
                    !Number.isInteger(nextOffset)
                    || nextOffset <= requestOffset
                    || nextOffset > file.size
                ) {
                    throw new Error('上传服务返回了无效偏移量');
                }
                offset = nextOffset;
                reconciliationAttempts = 0;
                advanced = true;
                break;
            } catch (error) {
                const cancelled = abortReason(signal);
                if (cancelled) throw cancelled;
                if (!retryableUploadError(error)) throw error;
                const committedOffset = Number(error?.uploadOffset);
                if (
                    Number.isInteger(committedOffset)
                    && committedOffset >= 0
                    && committedOffset <= file.size
                    && committedOffset !== requestOffset
                ) {
                    reconciliationAttempts += 1;
                    if (reconciliationAttempts >= UPLOAD_MAX_ATTEMPTS) throw error;
                    offset = committedOffset;
                    advanced = true;
                    break;
                }
                if (attempt === UPLOAD_MAX_ATTEMPTS) throw error;
                await waitForRetry(retryDelayMs(error, attempt), signal);
            }
        }
        if (!advanced) throw new Error('上传重试次数已用尽');
        reportProgress('uploading');
    }
    const job = await withUploadRetries(
        () => api.completeImportSession(stableSlug, session.id, { signal }),
        signal,
    );
    reportProgress(job.current_stage || 'queued', job);
    return job;
}

const TERMINAL_IMPORT_STATUSES = new Set(['submitted', 'skipped', 'failed']);

export function nextImportProgressBatch(items, cursor = 0, limit = 500) {
    const total = items.length;
    if (!total || limit < 1) return { items: [], nextCursor: 0 };

    const result = [];
    let index = Math.max(0, Number(cursor) || 0) % total;
    let inspected = 0;
    while (inspected < total && result.length < limit) {
        const item = items[index];
        if (item?.importJobId && !TERMINAL_IMPORT_STATUSES.has(item.status)) result.push(item);
        index = (index + 1) % total;
        inspected += 1;
    }
    return { items: result, nextCursor: index };
}

export async function runConcurrent(items, concurrency, worker, shouldRun = () => true) {
    const queue = [...items];
    let executed = 0;
    const configuredConcurrency = typeof concurrency === 'object'
        ? concurrency?.upload_concurrency
        : concurrency;
    const count = Math.max(
        1,
        Math.min(Number(configuredConcurrency) || 1, queue.length || 1),
    );
    const runners = Array.from({ length: count }, async () => {
        while (queue.length) {
            const item = queue.shift();
            if (!shouldRun(item)) continue;
            executed += 1;
            await worker(item);
        }
    });
    await Promise.all(runners);
    return executed;
}

export function importDisplayStatus(job) {
    // Source transfer owns the import screen. Downstream processing failures
    // must not override an already stored original file.
    const sourceSaved = job?.file_status === 'available'
        || (job?.file_status == null && Boolean(job?.upload_completed_at));
    if (sourceSaved) {
        return ['unchanged', 'duplicate_source'].includes(job?.result_operation) ? 'skipped' : 'submitted';
    }
    if (['failed', 'error'].includes(job?.file_status)) return 'failed';
    if (job?.status === 'succeeded') {
        return ['unchanged', 'duplicate_source'].includes(job?.result_operation) ? 'skipped' : 'submitted';
    }
    if (['failed', 'cancelled', 'superseded'].includes(job?.status)) return 'failed';
    if (job?.status === 'uploading' || job?.current_stage === 'uploading') return 'uploading';
    if (job?.status === 'queued' || job?.current_stage === 'queued') return 'queued';
    return 'processing';
}

export function importStageProgress(job, uploadPercent = 0) {
    if (job?.file_status === 'available'
        || (job?.file_status == null && job?.upload_completed_at)) return 100;
    return Math.max(0, Math.min(100, Math.round(Number(uploadPercent) || 0)));
}

export const IMPORT_STAGE_LABEL = Object.freeze({
    uploading: '上传中',
    queued: '等待上传',
    submitted: '文件已保存',
    skipped: '文件未变化',
    failed: '上传失败',
});

export function importStageLabel(job) {
    if (job?.file_status === 'available'
        || (job?.file_status == null && job?.upload_completed_at)) {
        return ['unchanged', 'duplicate_source'].includes(job?.result_operation)
            ? IMPORT_STAGE_LABEL.skipped
            : IMPORT_STAGE_LABEL.submitted;
    }
    if (['failed', 'error'].includes(job?.file_status)) return IMPORT_STAGE_LABEL.failed;
    return IMPORT_STAGE_LABEL[job?.current_stage] || IMPORT_STAGE_LABEL[job?.status] || '等待上传';
}
