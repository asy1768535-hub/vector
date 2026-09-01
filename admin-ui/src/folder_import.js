export const DEFAULT_IMPORT_CONFIGURATION = Object.freeze({
    max_file_bytes: 500 * 1024 * 1024,
    chunk_bytes: 32 * 1024 * 1024,
    max_files_per_selection: 1000,
    max_configurable_file_bytes: 50 * 1024 * 1024 * 1024,
    max_configurable_files_per_selection: 100_000,
    upload_concurrency: 1,
    doc_max_file_bytes: 200 * 1024 * 1024,
    allowed_extensions: [
        '.txt', '.md', '.markdown', '.rst', '.log', '.ini', '.cfg', '.conf',
        '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.csv', '.tsv',
        '.doc', '.docx', '.pptx', '.xls', '.xlsx', '.pdf',
        '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp',
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
    return normalizedExtensions(configuration).join(',');
}

export function supportedExtensionsLabel(configuration) {
    return normalizedExtensions(configuration).map((value) => value.slice(1)).join('、');
}

export function relativePathForFile(file) {
    const path = String(file?.webkitRelativePath || '').replace(/\\/g, '/').trim();
    return path || null;
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
    const stableSlug = resumeState.slug || slug;
    const stableBatchId = resumeState.batchId || batchId;
    resumeState.slug = stableSlug;
    resumeState.batchId = stableBatchId;
    const sessionPayload = {
        batch_id: stableBatchId,
        file_name: file.name,
        relative_path: relativePathForFile(file),
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
        session = await withUploadRetries(
            () => api.createImportSession(stableSlug, sessionPayload, { signal }),
            signal,
        );
        resumeState.session = session;
    }
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
    if (job?.status === 'succeeded') {
        return job?.result_operation === 'unchanged' ? 'skipped' : 'submitted';
    }
    if (['failed', 'cancelled', 'superseded'].includes(job?.status)) return 'failed';
    if (job?.status === 'uploading' || job?.current_stage === 'uploading') return 'uploading';
    if (job?.status === 'queued' || job?.current_stage === 'queued') return 'queued';
    return 'processing';
}

export function importStageProgress(job, uploadPercent = 0) {
    const stage = job?.current_stage || 'uploading';
    if (stage === 'uploading') return Math.min(45, Math.round(uploadPercent * 0.45));
    if (stage === 'queued' || stage === 'validating') return 48;
    if (stage === 'converting') return 52;
    if (stage === 'conversion_ready') return 54;
    if (stage === 'parsing') return 56;
    if (stage === 'chunking') return 66;
    if (stage === 'embedding') return 78;
    if (stage === 'graph') return 90;
    return 100;
}

export const IMPORT_STAGE_LABEL = Object.freeze({
    uploading: '上传中',
    queued: '已接收/排队',
    converting: '转换旧版 Word',
    conversion_ready: '等待解析转换结果',
    validating: '校验文件',
    parsing: '解析中',
    chunking: '生成切片',
    embedding: '向量化中',
    graph: '图谱/审核中',
    completed: '处理完成',
});

export function importStageLabel(job) {
    if (
        job?.current_stage === 'graph'
        && job?.schema_discovery_state === 'waiting_schema'
    ) {
        return '等待批次 Schema';
    }
    return IMPORT_STAGE_LABEL[job?.current_stage] || '等待处理';
}
