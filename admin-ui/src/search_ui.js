function metadataOf(row) {
    return row?.metadata && typeof row.metadata === 'object' ? row.metadata : {};
}

function positiveInteger(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const number = Number(value);
    return Number.isInteger(number) && number > 0 ? number : null;
}

function firstText(...values) {
    return values.find((value) => typeof value === 'string' && value.trim())?.trim() || '';
}

export function shortSearchId(value, length = 12) {
    const text = String(value || '');
    return text.length > length ? `${text.slice(0, length)}...` : text;
}

export function resultSourceLabel(row) {
    const metadata = metadataOf(row);
    const page = metadata.page && typeof metadata.page === 'object' ? metadata.page : {};
    const pageStart = positiveInteger(metadata.page_start ?? page.start ?? metadata.page);
    const pageEnd = positiveInteger(metadata.page_end ?? page.end ?? pageStart);
    if (pageStart) return pageEnd && pageEnd !== pageStart
        ? `第${pageStart}-${pageEnd}页`
        : `第${pageStart}页`;

    const sequence = positiveInteger(row?.seq ?? metadata.seq ?? metadata.chunk_seq);
    if (sequence) return `片段 ${sequence}`;

    const revision = positiveInteger(
        row?.document_revision ?? metadata.document_revision ?? metadata.document_revision_no,
    );
    if (revision) return `版本 v${revision}`;

    const path = firstText(metadata.path, metadata.source_path, metadata.file_path, metadata.document_path);
    if (path) return path;

    const externalId = firstText(row?.external_id, metadata.external_id);
    if (externalId) return `外部 ID ${externalId}`;

    const documentId = firstText(row?.document_id, metadata.document_id);
    if (documentId) return `文档 ${shortSearchId(documentId)}`;

    const chunkId = firstText(row?.chunk_id, metadata.chunk_id);
    if (chunkId) return `片段 ${shortSearchId(chunkId)}`;
    return '来源未标注';
}

export function resultDocumentRef(row) {
    const metadata = metadataOf(row);
    const externalId = firstText(row?.external_id, metadata.external_id);
    const documentId = firstText(row?.document_id, metadata.document_id);
    if (externalId && documentId) {
        return `外部 ID ${externalId} · 文档 ${shortSearchId(documentId)}`;
    }
    if (externalId) return `外部 ID ${externalId}`;
    if (documentId) return `文档 ${shortSearchId(documentId)}`;
    const chunkId = firstText(row?.chunk_id, metadata.chunk_id);
    return chunkId ? `片段 ${shortSearchId(chunkId)}` : '';
}
