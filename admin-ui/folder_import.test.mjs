import assert from 'node:assert/strict';
import test from 'node:test';

import {
    createImportBatchId,
    DEFAULT_IMPORT_CONFIGURATION,
    importStageProgress,
    relativePathForFile,
    runConcurrent,
    uploadFileInChunks,
} from './src/folder_import.js';


test('folder files preserve their webkit relative path', () => {
    assert.equal(
        relativePathForFile({ webkitRelativePath: '项目甲/合同/主合同.pdf' }),
        '项目甲/合同/主合同.pdf',
    );
    assert.equal(relativePathForFile({ webkitRelativePath: '' }), null);
});

test('batch id falls back to getRandomValues outside secure contexts', () => {
    const cryptoWithoutRandomUuid = {
        getRandomValues(bytes) {
            bytes.fill(0xab);
            return bytes;
        },
    };

    assert.equal(
        createImportBatchId(cryptoWithoutRandomUuid),
        'abababab-abab-4bab-abab-abababababab',
    );
});

test('large file upload uses ordered configured chunks and completes session', async () => {
    const bytes = new Uint8Array(20);
    const file = {
        name: 'nested.txt',
        type: 'text/plain',
        size: bytes.length,
        lastModified: 10,
        webkitRelativePath: 'root/sub/nested.txt',
        slice(start, end) {
            return new Blob([bytes.slice(start, end)]);
        },
    };
    const offsets = [];
    const api = {
        async createImportSession(_slug, payload) {
            assert.equal(payload.relative_path, 'root/sub/nested.txt');
            return { id: 'job-1', upload_offset: 0 };
        },
        async uploadImportChunk(_slug, _jobId, chunk, offset) {
            offsets.push([offset, chunk.size]);
            return offset + chunk.size;
        },
        async completeImportSession() {
            return { id: 'job-1', status: 'queued', current_stage: 'queued' };
        },
    };

    const job = await uploadFileInChunks({
        api,
        slug: 'public',
        file,
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 8 },
    });

    assert.deepEqual(offsets, [[0, 8], [8, 8], [16, 4]]);
    assert.equal(job.current_stage, 'queued');
});

test('concurrency pool never exceeds two workers', async () => {
    let active = 0;
    let peak = 0;
    await runConcurrent([1, 2, 3, 4, 5], 2, async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
    });
    assert.equal(peak, 2);
});

test('unified progress reserves stages for parsing embedding and graph', () => {
    assert.equal(importStageProgress({ current_stage: 'uploading' }, 50), 23);
    assert.equal(importStageProgress({ current_stage: 'parsing' }, 100), 56);
    assert.equal(importStageProgress({ current_stage: 'embedding' }, 100), 78);
    assert.equal(importStageProgress({ current_stage: 'graph' }, 100), 90);
    assert.equal(importStageProgress({ current_stage: 'completed' }, 100), 100);
});
