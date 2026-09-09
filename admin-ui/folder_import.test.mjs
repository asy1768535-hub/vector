import assert from 'node:assert/strict';
import test from 'node:test';

import {
    createImportBatchId,
    createImportBatchIds,
    DEFAULT_IMPORT_CONFIGURATION,
    IMPORT_PROFILE_DAILY,
    IMPORT_PROFILE_INITIAL,
    importStageLabel,
    importStageProgress,
    relativePathForFile,
    runConcurrent,
    supportedExtensionsAccept,
    supportedExtensionsLabel,
    uploadFileInChunks,
} from './src/folder_import.js';


test('folder files preserve their webkit relative path', () => {
    assert.equal(
        relativePathForFile({ webkitRelativePath: '项目甲/合同/主合同.pdf' }),
        '项目甲/合同/主合同.pdf',
    );
    assert.equal(relativePathForFile({ webkitRelativePath: '' }), null);
});

test('upload configuration keeps daily defaults, admin maximums, and legacy formats', () => {
    assert.equal(IMPORT_PROFILE_DAILY.max_file_bytes, 500 * 1024 * 1024);
    assert.equal(IMPORT_PROFILE_DAILY.max_files_per_selection, 1000);
    assert.equal(IMPORT_PROFILE_INITIAL.max_file_bytes, 50 * 1024 * 1024 * 1024);
    assert.equal(IMPORT_PROFILE_INITIAL.max_files_per_selection, 100_000);
    assert.equal(
        DEFAULT_IMPORT_CONFIGURATION.max_configurable_file_bytes,
        IMPORT_PROFILE_INITIAL.max_file_bytes,
    );
    assert.equal(
        DEFAULT_IMPORT_CONFIGURATION.max_configurable_files_per_selection,
        IMPORT_PROFILE_INITIAL.max_files_per_selection,
    );
    assert.equal(DEFAULT_IMPORT_CONFIGURATION.allowed_extensions.includes('.doc'), true);
    assert.equal(DEFAULT_IMPORT_CONFIGURATION.allowed_extensions.includes('.xls'), true);
    assert.equal(DEFAULT_IMPORT_CONFIGURATION.chunk_bytes, 32 * 1024 * 1024);
    assert.equal(DEFAULT_IMPORT_CONFIGURATION.upload_concurrency, 1);
});

test('supported format display is generated from server configuration', () => {
    const configuration = { allowed_extensions: ['.TXT', '.pptx', '.html'] };
    assert.equal(supportedExtensionsAccept(configuration), '.txt,.pptx,.html');
    assert.equal(supportedExtensionsLabel(configuration), 'txt、pptx、html');
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

test('all items in one graph import batch share one batch ID', () => {
    const first = { name: 'first.pdf' };
    const second = { name: 'second.pdf' };
    const batchIds = createImportBatchIds([first, second], 'batch-1');

    assert.equal(batchIds.size, 2);
    assert.equal(batchIds.get(first), 'batch-1');
    assert.equal(batchIds.get(second), 'batch-1');
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

test('concurrency pool skips a pending item removed from the live queue', async () => {
    const first = { name: 'first.txt' };
    const removed = { name: 'removed.txt' };
    const liveQueue = [first, removed];
    const started = [];
    let releaseFirst;
    let markFirstStarted;
    const firstBlocked = new Promise((resolve) => { releaseFirst = resolve; });
    const firstStarted = new Promise((resolve) => { markFirstStarted = resolve; });

    const running = runConcurrent(
        [first, removed],
        1,
        async (item) => {
            started.push(item.name);
            if (item === first) {
                markFirstStarted();
                await firstBlocked;
            }
        },
        (item) => liveQueue.includes(item),
    );

    await firstStarted;
    liveQueue.splice(liveQueue.indexOf(removed), 1);
    releaseFirst();
    const executed = await running;

    assert.deepEqual(started, ['first.txt']);
    assert.equal(executed, 1);
});

test('unified progress reserves stages for parsing embedding and graph', () => {
    assert.equal(importStageProgress({ current_stage: 'uploading' }, 50), 23);
    assert.equal(importStageProgress({ current_stage: 'converting' }, 100), 52);
    assert.equal(importStageProgress({ current_stage: 'conversion_ready' }, 100), 54);
    assert.equal(importStageProgress({ current_stage: 'parsing' }, 100), 56);
    assert.equal(importStageProgress({ current_stage: 'embedding' }, 100), 78);
    assert.equal(importStageProgress({ current_stage: 'graph' }, 100), 90);
    assert.equal(importStageProgress({ current_stage: 'completed' }, 100), 100);
});

test('legacy Word conversion stages have user-facing labels', () => {
    assert.equal(importStageLabel({ current_stage: 'converting' }), '转换旧版 Word');
    assert.equal(importStageLabel({ current_stage: 'conversion_ready' }), '等待解析转换结果');
});

test('schema coordination is not labeled as active graph extraction', () => {
    assert.equal(importStageLabel({
        current_stage: 'graph',
        schema_discovery_state: 'waiting_schema',
    }), '等待批次 Schema');
    assert.equal(importStageLabel({ current_stage: 'graph' }), '图谱/审核中');
});
