export const DEFAULT_IMPORT_CONFIGURATION = Object.freeze({
    max_file_bytes: 500 * 1024 * 1024,
    chunk_bytes: 8 * 1024 * 1024,
    max_files_per_selection: 1000,
    upload_concurrency: 2,
    allowed_extensions: ['.txt', '.md', '.markdown', '.json', '.csv', '.docx', '.xlsx', '.pdf'],
});

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
}) {
    const session = await api.createImportSession(slug, {
        batch_id: batchId,
        file_name: file.name,
        relative_path: relativePathForFile(file),
        content_type: file.type || null,
        size_bytes: file.size,
        last_modified_millis: file.lastModified || null,
        external_id: options.externalId || null,
        replace_document_id: options.replaceDocumentId || null,
        security_level: options.securityLevel || null,
        graph_extraction_requested: Boolean(options.graphExtractionRequested),
    });
    let offset = Number(session.upload_offset || 0);
    onProgress({ phase: 'uploading', offset, total: file.size, job: session });
    while (offset < file.size) {
        const end = Math.min(offset + configuration.chunk_bytes, file.size);
        offset = await api.uploadImportChunk(
            slug,
            session.id,
            file.slice(offset, end),
            offset,
        );
        onProgress({ phase: 'uploading', offset, total: file.size, job: session });
    }
    const job = await api.completeImportSession(slug, session.id);
    onProgress({ phase: job.current_stage || 'queued', offset, total: file.size, job });
    return job;
}

export async function runConcurrent(items, concurrency, worker) {
    const queue = [...items];
    const count = Math.max(1, Math.min(Number(concurrency) || 1, queue.length || 1));
    const runners = Array.from({ length: count }, async () => {
        while (queue.length) {
            const item = queue.shift();
            await worker(item);
        }
    });
    await Promise.all(runners);
}

export function importStageProgress(job, uploadPercent = 0) {
    const stage = job?.current_stage || 'uploading';
    if (stage === 'uploading') return Math.min(45, Math.round(uploadPercent * 0.45));
    if (stage === 'queued' || stage === 'validating') return 48;
    if (stage === 'parsing') return 56;
    if (stage === 'chunking') return 66;
    if (stage === 'embedding') return 78;
    if (stage === 'graph') return 90;
    return 100;
}

export const IMPORT_STAGE_LABEL = Object.freeze({
    uploading: '上传中',
    queued: '等待处理',
    validating: '校验文件',
    parsing: '解析文档',
    chunking: '生成切片',
    embedding: '向量化',
    graph: '构建知识图谱',
    completed: '处理完成',
});
