import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import * as folderImport from './src/folder_import.js';
import { ST_LABEL } from './src/import_ui.js';
import { createPreviewFetch } from './src/preview_mode.js';

const {
    DEFAULT_IMPORT_CONFIGURATION,
    importDisplayStatus,
    importStageLabel,
    nextImportProgressBatch,
    runConcurrent,
} = folderImport;

const PREVIEW_URL = 'http://127.0.0.1:5599/?preview=1#/login';
const importViewSource = readFileSync(new URL('./src/views/Import.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');

test('upload inbox defaults each user to one active file upload', async () => {
    assert.equal(DEFAULT_IMPORT_CONFIGURATION.upload_concurrency, 1);

    const previewFetch = createPreviewFetch(
        () => { throw new Error('preview request escaped to the network'); },
        PREVIEW_URL,
    );
    const configuration = await (await previewFetch(
        '/libraries/preview-library/import-configuration',
    )).json();
    assert.equal(configuration.upload_concurrency, 1);
});

test('an explicit server concurrency remains the scheduler authority', async () => {
    const serverConfiguration = {
        ...DEFAULT_IMPORT_CONFIGURATION,
        upload_concurrency: 3,
    };
    let active = 0;
    let peak = 0;
    await runConcurrent([1, 2, 3, 4, 5, 6], serverConfiguration, async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
    });
    assert.equal(peak, 3);
});

test('queued and processing jobs keep distinct visible states', () => {
    const queued = importDisplayStatus({ status: 'queued', current_stage: 'queued' });
    const processing = importDisplayStatus({ status: 'processing', current_stage: 'parsing' });

    assert.equal(queued, 'queued');
    assert.equal(ST_LABEL[queued], '已接收/排队');
    assert.equal(processing, 'processing');
    assert.equal(ST_LABEL[processing], '处理中');
    assert.equal(importDisplayStatus({ status: 'succeeded', result_operation: 'created' }), 'submitted');
    assert.equal(importDisplayStatus({ status: 'succeeded', result_operation: 'unchanged' }), 'skipped');
    assert.equal(importDisplayStatus({ status: 'failed' }), 'failed');
});

test('upload inbox labels separate durable receipt from background stages', () => {
    assert.equal(importStageLabel({ current_stage: 'uploading' }), '上传中');
    assert.equal(importStageLabel({ current_stage: 'queued' }), '已接收/排队');
    assert.equal(importStageLabel({ current_stage: 'parsing' }), '解析中');
    assert.equal(importStageLabel({ current_stage: 'embedding' }), '向量化中');
    assert.equal(importStageLabel({ current_stage: 'graph' }), '图谱/审核中');
    assert.equal(importStageLabel({ current_stage: 'completed' }), '处理完成');
});

test('progress polling rotates bounded batches instead of requesting every active job', () => {
    const rows = [
        { importJobId: 'a', status: 'queued' },
        { importJobId: 'b', status: 'processing' },
        { importJobId: 'c', status: 'submitted' },
        { importJobId: 'd', status: 'failed' },
        { importJobId: 'e', status: 'queued' },
    ];

    const first = nextImportProgressBatch(rows, 0, 2);
    const second = nextImportProgressBatch(rows, first.nextCursor, 2);

    assert.deepEqual(first.items.map((item) => item.importJobId), ['a', 'b']);
    assert.deepEqual(second.items.map((item) => item.importJobId), ['e', 'a']);
    assert.equal(first.items.length, 2);
});

test('progress polling uses one bounded batch endpoint and cancels stale requests', () => {
    assert.ok(apiSource.includes('getImportJobProgress'));
    assert.ok(importViewSource.includes('api.getImportJobProgress'));
    assert.ok(importViewSource.includes('nextImportProgressBatch'));
    assert.ok(importViewSource.includes('importJobsPollController.abort()'));
    assert.equal(importViewSource.includes('api.listImportJobs(slug.value, { limit: 1000 })'), false);
});

test('large queues keep File objects raw and render one window at a time', () => {
    assert.ok(importViewSource.includes('Object.freeze(file)'));
    assert.ok(importViewSource.includes('queuePageSize'));
    assert.ok(importViewSource.includes('<el-pagination'));
    assert.ok(importViewSource.includes('FILE_SELECTION_CHUNK_SIZE'));
    assert.ok(importViewSource.includes('validateBatchChunk'));
    assert.equal(importViewSource.includes('Array.from(files)'), false);
});

test('progress polling backs off and pauses while the page is hidden', () => {
    assert.ok(importViewSource.includes('IMPORT_PROGRESS_POLL_DELAYS'));
    assert.ok(importViewSource.includes("document?.addEventListener('visibilitychange'"));
    assert.ok(importViewSource.includes('if (globalThis.document?.hidden)'));
});
