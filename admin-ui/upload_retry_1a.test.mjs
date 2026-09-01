import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';

import {
    completeImportSession,
    uploadImportChunk,
} from './src/api.js';
import {
    DEFAULT_IMPORT_CONFIGURATION,
    uploadFileInChunks,
} from './src/folder_import.js';

const importViewSource = readFileSync(
    new URL('./src/views/Import.js', import.meta.url),
    'utf8',
);

function fileOfSize(size) {
    const bytes = Uint8Array.from({ length: size }, (_value, index) => index);
    return {
        name: 'resume.txt',
        type: 'text/plain',
        size,
        lastModified: 1,
        webkitRelativePath: 'root/resume.txt',
        slice(start, end) {
            return new Blob([bytes.slice(start, end)]);
        },
    };
}

function uploadError({ status, code, offset, retryAfter = 0 }) {
    const error = new Error(code);
    error.status = status;
    error.body = { detail: { code, message: '上传暂时繁忙，请稍后重试' } };
    error.uploadOffset = offset;
    error.retryAfterSeconds = retryAfter;
    return error;
}

function dataModule(source) {
    return `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`;
}

async function loadImportComponent(harness) {
    globalThis.__IMPORT_COMPONENT_HARNESS__ = harness;
    const stubs = new Map([
        ['vue', dataModule(`
            export const ref = (value) => ({ value });
            export const reactive = (value) => value;
            export const computed = (definition) => {
                const getter = typeof definition === 'function' ? definition : definition.get;
                return { get value() { return getter(); } };
            };
            export const watch = () => {};
            export const onMounted = () => {};
            export const onBeforeUnmount = (callback) => {
                globalThis.__IMPORT_COMPONENT_HARNESS__.beforeUnmount = callback;
            };
        `)],
        ['vue-router', dataModule(`
            export const useRoute = () => ({ query: {} });
            export const useRouter = () => ({ push: async () => {} });
        `)],
        ['element-plus', dataModule(`
            const harness = () => globalThis.__IMPORT_COMPONENT_HARNESS__;
            export const ElMessage = {
                success(message) { harness().messages.success.push(message); },
                warning(message) { harness().messages.warning.push(message); },
                error(message) { harness().messages.error.push(message); },
            };
            export const ElMessageBox = { confirm: async () => true };
        `)],
        ['../api.js', dataModule(`
            const api = () => globalThis.__IMPORT_COMPONENT_HARNESS__.api;
            export const createImportSession = (...args) => api().createImportSession(...args);
            export const uploadImportChunk = (...args) => api().uploadImportChunk(...args);
            export const completeImportSession = (...args) => api().completeImportSession(...args);
        `)],
    ]);
    const hooks = registerHooks({
        resolve(specifier, context, nextResolve) {
            const stub = stubs.get(specifier);
            if (stub) return { url: stub, shortCircuit: true };
            return nextResolve(specifier, context);
        },
    });
    try {
        const module = await import(`./src/views/Import.js?component-test=${Date.now()}`);
        return { ImportView: module.default, hooks };
    } catch (error) {
        hooks.deregister();
        delete globalThis.__IMPORT_COMPONENT_HARNESS__;
        throw error;
    }
}

test('upload adapters preserve retry headers and nested error code', async () => {
    const originalFetch = globalThis.fetch;
    const controller = new AbortController();
    let call = 0;
    globalThis.fetch = async (_url, options) => {
        call += 1;
        assert.equal(options.signal, controller.signal);
        const status = call === 1 ? 409 : 429;
        return new Response(JSON.stringify({
            detail: {
                code: call === 1 ? 'upload_offset_mismatch' : 'upload_user_limit',
                message: '上传暂时繁忙，请稍后重试',
            },
        }), {
            status,
            headers: {
                'Content-Type': 'application/json',
                'Upload-Offset': '8',
                'Retry-After': '2',
            },
        });
    };

    try {
        await assert.rejects(
            uploadImportChunk('public', 'job-1', new Blob(['data']), 4, {
                signal: controller.signal,
            }),
            (error) => {
                assert.equal(error.status, 409);
                assert.equal(error.uploadOffset, 8);
                assert.equal(error.retryAfterSeconds, 2);
                assert.equal(error.body.detail.code, 'upload_offset_mismatch');
                return true;
            },
        );
        await assert.rejects(
            completeImportSession('public', 'job-1', { signal: controller.signal }),
            (error) => {
                assert.equal(error.status, 429);
                assert.equal(error.uploadOffset, 8);
                assert.equal(error.retryAfterSeconds, 2);
                assert.equal(error.body.detail.code, 'upload_user_limit');
                return true;
            },
        );
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test('successful chunk responses must include an explicit offset header', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(null, { status: 204 });
    try {
        await assert.rejects(
            uploadImportChunk('public', 'job-1', new Blob(['data']), 0),
            /缺少有效偏移量/,
        );
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test('lost chunk response reconciles committed offset on the same session', async () => {
    const file = fileOfSize(10);
    const offsets = [];
    let sessionCreates = 0;
    const api = {
        async createImportSession() {
            sessionCreates += 1;
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk(_slug, jobId, _chunk, offset) {
            assert.equal(jobId, 'job-1');
            offsets.push(offset);
            if (offsets.length === 1) return 4;
            if (offsets.length === 2) throw new TypeError('response lost');
            if (offsets.length === 3) {
                throw uploadError({
                    status: 409,
                    code: 'upload_offset_mismatch',
                    offset: 8,
                });
            }
            return 10;
        },
        async completeImportSession(_slug, jobId) {
            assert.equal(jobId, 'job-1');
            return { id: jobId, status: 'queued', current_stage: 'queued' };
        },
    };

    const result = await uploadFileInChunks({
        api,
        slug: 'public',
        file,
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    });

    assert.equal(sessionCreates, 1);
    assert.deepEqual(offsets, [0, 4, 4, 8]);
    assert.equal(result.status, 'queued');
});

test('lost session response and 5xx reuse the same batch payload with bounded retries', async () => {
    const payloads = [];
    const api = {
        async createImportSession(_slug, payload) {
            payloads.push(structuredClone(payload));
            if (payloads.length === 1) {
                const error = new TypeError('response lost');
                error.retryAfterSeconds = 0;
                throw error;
            }
            if (payloads.length === 2) {
                throw uploadError({
                    status: 503,
                    code: 'upload_capacity_unavailable',
                    retryAfter: 0,
                });
            }
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk(_slug, _jobId, chunk, offset) {
            return offset + chunk.size;
        },
        async completeImportSession(_slug, jobId) {
            return { id: jobId, status: 'queued', current_stage: 'queued' };
        },
    };

    const result = await uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-stable',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    });

    assert.equal(result.status, 'queued');
    assert.equal(payloads.length, 3);
    assert.deepEqual(payloads, [payloads[0], payloads[0], payloads[0]]);
    assert.equal(payloads[0].batch_id, 'batch-stable');
});

test('transient 5xx statuses retry session, chunk, and Complete', async () => {
    for (const status of [500, 502, 503, 504]) {
        const calls = { session: 0, chunk: 0, complete: 0 };
        const transient = () => uploadError({
            status,
            code: 'temporary_upload_failure',
            retryAfter: 0,
        });
        const api = {
            async createImportSession() {
                calls.session += 1;
                if (calls.session === 1) throw transient();
                return { id: 'job-1', status: 'uploading', upload_offset: 0 };
            },
            async uploadImportChunk(_slug, _jobId, chunk, offset) {
                calls.chunk += 1;
                if (calls.chunk === 1) throw transient();
                return offset + chunk.size;
            },
            async completeImportSession(_slug, jobId) {
                calls.complete += 1;
                if (calls.complete === 1) throw transient();
                return { id: jobId, status: 'queued', current_stage: 'queued' };
            },
        };

        const result = await uploadFileInChunks({
            api,
            slug: 'public',
            file: fileOfSize(4),
            batchId: `batch-${status}`,
            configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
        });

        assert.equal(result.status, 'queued');
        assert.deepEqual(calls, { session: 2, chunk: 2, complete: 2 });
    }
});

test('session authorization failures are not retried', async () => {
    for (const status of [401, 403, 413, 415, 422]) {
        let calls = 0;
        const terminal = uploadError({ status, code: 'terminal_upload_error' });
        const api = {
            async createImportSession() {
                calls += 1;
                throw terminal;
            },
        };

        await assert.rejects(uploadFileInChunks({
            api,
            slug: 'public',
            file: fileOfSize(4),
            batchId: 'batch-1',
        }), (error) => error === terminal);
        assert.equal(calls, 1);
    }
});

test('invalid session offsets stop before uploading content', async () => {
    for (const uploadOffset of [undefined, null, '', '0', -1, 1.5, 5]) {
        let uploadCalls = 0;
        const api = {
            async createImportSession() {
                return { id: 'job-1', status: 'uploading', upload_offset: uploadOffset };
            },
            async uploadImportChunk() {
                uploadCalls += 1;
            },
        };

        await assert.rejects(uploadFileInChunks({
            api,
            slug: 'public',
            file: fileOfSize(4),
            batchId: 'batch-1',
        }), /无效偏移量/);
        assert.equal(uploadCalls, 0);
    }
});

test('server committed offset can move the browser backward on the same session', async () => {
    const offsets = [];
    let sessionCreates = 0;
    const api = {
        async createImportSession() {
            sessionCreates += 1;
            return { id: 'job-1', status: 'uploading', upload_offset: 8 };
        },
        async uploadImportChunk(_slug, jobId, chunk, offset) {
            assert.equal(jobId, 'job-1');
            offsets.push(offset);
            if (offsets.length === 1) {
                throw uploadError({
                    status: 409,
                    code: 'upload_offset_mismatch',
                    offset: 4,
                });
            }
            return offset + chunk.size;
        },
        async completeImportSession(_slug, jobId) {
            return { id: jobId, status: 'queued', current_stage: 'queued' };
        },
    };

    const result = await uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(10),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    });

    assert.equal(sessionCreates, 1);
    assert.deepEqual(offsets, [8, 4, 8]);
    assert.equal(result.status, 'queued');
});

test('terminal chunk errors do not reconcile their offset header', async () => {
    let uploadCalls = 0;
    let completeCalls = 0;
    const terminal = uploadError({
        status: 413,
        code: 'upload_chunk_too_large',
        offset: 2,
    });
    const api = {
        async createImportSession() {
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk() {
            uploadCalls += 1;
            throw terminal;
        },
        async completeImportSession() {
            completeCalls += 1;
        },
    };

    await assert.rejects(uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    }), (error) => error === terminal);

    assert.equal(uploadCalls, 1);
    assert.equal(completeCalls, 0);
});

test('manual retry resumes the original batch session job and offset', async () => {
    const file = fileOfSize(8);
    const resumeState = {};
    const createdPayloads = [];
    const offsets = [];
    const progress = [];
    const requestSlugs = [];
    let firstAttempt = true;
    let completeCalls = 0;
    const api = {
        async createImportSession(requestSlug, payload) {
            requestSlugs.push(requestSlug);
            createdPayloads.push(structuredClone(payload));
            return { id: `job-${createdPayloads.length}`, status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk(requestSlug, jobId, chunk, offset) {
            requestSlugs.push(requestSlug);
            offsets.push([jobId, offset]);
            if (firstAttempt && offset === 4) {
                throw uploadError({
                    status: 413,
                    code: 'terminal_upload_error',
                    offset,
                });
            }
            return offset + chunk.size;
        },
        async completeImportSession(requestSlug, jobId) {
            requestSlugs.push(requestSlug);
            completeCalls += 1;
            return { id: jobId, status: 'queued', current_stage: 'queued' };
        },
    };
    const configuration = { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 };

    await assert.rejects(uploadFileInChunks({
        api,
        slug: 'public',
        file,
        batchId: 'batch-original',
        configuration,
        resumeState,
        onProgress(update) { progress.push(update); },
    }), (error) => error.status === 413);

    assert.equal(resumeState.batchId, 'batch-original');
    assert.equal(resumeState.session.id, 'job-1');
    assert.equal(resumeState.session.upload_offset, 4);

    firstAttempt = false;
    const result = await uploadFileInChunks({
        api,
        slug: 'other-library',
        file,
        batchId: 'batch-new-fallback',
        configuration,
        resumeState,
        onProgress(update) { progress.push(update); },
    });

    assert.equal(result.id, 'job-1');
    assert.equal(createdPayloads.length, 1);
    assert.equal(createdPayloads[0].batch_id, 'batch-original');
    assert.deepEqual(offsets, [
        ['job-1', 0],
        ['job-1', 4],
        ['job-1', 4],
    ]);
    assert.equal(completeCalls, 1);
    assert.deepEqual(requestSlugs, ['public', 'public', 'public', 'public', 'public']);
    assert.equal(progress.at(-1).job.id, 'job-1');
    assert.equal(progress.at(-1).job.upload_offset, 8);
});

test('chunk retry budget is bounded on repeated rate limits', async () => {
    let uploadCalls = 0;
    let completeCalls = 0;
    const api = {
        async createImportSession() {
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk() {
            uploadCalls += 1;
            throw uploadError({
                status: 429,
                code: 'upload_user_limit',
                offset: 0,
            });
        },
        async completeImportSession() {
            completeCalls += 1;
        },
    };

    await assert.rejects(uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    }), (error) => error.status === 429);

    assert.equal(uploadCalls, 4);
    assert.equal(completeCalls, 0);
});

test('lost Complete response retries the same job and accepts processing projection', async () => {
    let sessionCreates = 0;
    let completeCalls = 0;
    const api = {
        async createImportSession() {
            sessionCreates += 1;
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk(_slug, _jobId, chunk, offset) {
            return offset + chunk.size;
        },
        async completeImportSession(_slug, jobId) {
            completeCalls += 1;
            if (completeCalls === 1) throw new TypeError('response lost');
            return { id: jobId, status: 'processing', current_stage: 'embedding' };
        },
    };

    const result = await uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
    });

    assert.equal(sessionCreates, 1);
    assert.equal(completeCalls, 2);
    assert.equal(result.current_stage, 'embedding');
});

test('AbortSignal stops retry waiting without cancelling the server job', async () => {
    const controller = new AbortController();
    let uploadCalls = 0;
    const api = {
        async createImportSession() {
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk() {
            uploadCalls += 1;
            throw uploadError({
                status: 429,
                code: 'upload_user_limit',
                offset: 0,
                retryAfter: 30,
            });
        },
        async completeImportSession() {
            assert.fail('Complete must not run after cancellation');
        },
    };

    const upload = uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
        signal: controller.signal,
    });
    setTimeout(() => controller.abort(), 5);

    await assert.rejects(upload, (error) => error.name === 'AbortError');
    assert.equal(uploadCalls, 1);
});

test('completed retry waits remove their abort listener', async () => {
    const controller = new AbortController();
    const originalAdd = controller.signal.addEventListener.bind(controller.signal);
    const originalRemove = controller.signal.removeEventListener.bind(controller.signal);
    let added = 0;
    let removed = 0;
    controller.signal.addEventListener = (...args) => {
        added += 1;
        return originalAdd(...args);
    };
    controller.signal.removeEventListener = (...args) => {
        removed += 1;
        return originalRemove(...args);
    };
    let uploadCalls = 0;
    const api = {
        async createImportSession() {
            return { id: 'job-1', status: 'uploading', upload_offset: 0 };
        },
        async uploadImportChunk(_slug, _jobId, chunk, offset) {
            uploadCalls += 1;
            if (uploadCalls === 1) {
                throw uploadError({
                    status: 429,
                    code: 'upload_user_limit',
                    offset: 0,
                    retryAfter: 0.001,
                });
            }
            return offset + chunk.size;
        },
        async completeImportSession(_slug, jobId) {
            return { id: jobId, status: 'queued', current_stage: 'queued' };
        },
    };

    await uploadFileInChunks({
        api,
        slug: 'public',
        file: fileOfSize(4),
        batchId: 'batch-1',
        configuration: { ...DEFAULT_IMPORT_CONFIGURATION, chunk_bytes: 4 },
        signal: controller.signal,
    });

    assert.equal(added, 1);
    assert.equal(removed, 1);
});

test('Import view aborts only its active browser upload when unmounted', () => {
    assert.match(importViewSource, /signal: uploadController\.signal/);
    assert.match(importViewSource, /activeUploadController\?\.abort\(\)/);
    assert.match(
        importViewSource,
        /if \(e\?\.name === 'AbortError' \|\| uploadController\.signal\.aborted\) throw e;/,
    );
    assert.match(
        importViewSource,
        /if \(e\?\.name === 'AbortError' \|\| uploadController\.signal\.aborted\) return;/,
    );
    assert.match(importViewSource, /if \(uploadController\.signal\.aborted\) return;/);
    assert.doesNotMatch(importViewSource, /cancelImportJob\(/);
});

test('Import view freezes one target library for the whole upload batch', () => {
    assert.match(importViewSource, /const targetSlug = slug\.value;/);
    assert.match(
        importViewSource,
        /const uploadConfiguration = \{ \.\.\.importConfiguration\.value \};/,
    );
    assert.match(importViewSource, /const uploadOptions = graphUploadOptions\(\);/);
    assert.match(importViewSource, /async function retryGraphImport\(importJob, targetSlug\)/);
    assert.match(importViewSource, /retryGraphImport\(it\.importJob, targetSlug\)/);
    assert.match(importViewSource, /retryImportJob\(targetSlug, it\.importJobId\)/);
    assert.match(importViewSource, /slug: targetSlug,/);
    assert.match(importViewSource, /configuration: uploadConfiguration,/);
    assert.match(importViewSource, /\.\.\.uploadOptions,/);
    assert.match(
        importViewSource,
        /:disabled="routeReplaceActive \|\| batchReplacing \|\| uploading \|\| addingFiles"/,
    );
    assert.match(
        importViewSource,
        /:disabled="graphExtractionConfigLoading \|\| uploading \|\| addingFiles"/,
    );
});

test('Import component locks queue edits and counts only live upload items', async (t) => {
    let releaseUpload;
    let markUploadStarted;
    const uploadBlocked = new Promise((resolve) => { releaseUpload = resolve; });
    const uploadStarted = new Promise((resolve) => { markUploadStarted = resolve; });
    const sessions = [];
    const harness = {
        messages: { success: [], warning: [], error: [] },
        beforeUnmount: null,
        api: {
            async createImportSession(_slug, payload) {
                const session = {
                    id: `job-${sessions.length + 1}`,
                    status: 'uploading',
                    upload_offset: 0,
                    payload,
                };
                sessions.push(session);
                return session;
            },
            async uploadImportChunk(_slug, _jobId, chunk, offset) {
                markUploadStarted();
                await uploadBlocked;
                return offset + chunk.size;
            },
            async completeImportSession(_slug, jobId) {
                return {
                    id: jobId,
                    status: 'succeeded',
                    current_stage: 'completed',
                    result_operation: 'created',
                };
            },
        },
    };
    const { ImportView, hooks } = await loadImportComponent(harness);
    t.after(() => {
        harness.beforeUnmount?.();
        hooks.deregister();
        delete globalThis.__IMPORT_COMPONENT_HARNESS__;
    });
    const view = ImportView.setup();
    const existingFile = fileOfSize(4);
    const extraFile = { ...fileOfSize(4), name: 'extra.txt', lastModified: 2 };
    const existing = {
        _key: 'resume.txt|4|1',
        file: existingFile,
        name: existingFile.name,
        size: existingFile.size,
        status: 'pending',
        error: '',
        progress: 0,
        stageLabel: '',
    };
    let fileClicks = 0;
    let folderClicks = 0;
    view.fileInput.value = { click() { fileClicks += 1; } };
    view.folderInput.value = { click() { folderClicks += 1; } };
    view.queue.value = [existing];
    view.uploading.value = true;

    view.triggerFileSelect();
    view.triggerFolderSelect();
    view.addFiles([extraFile]);
    view.removeItem(existing);
    view.clearQueue();

    assert.deepEqual({
        fileClicks,
        folderClicks,
        queue: view.queue.value.map((item) => item.name),
    }, {
        fileClicks: 0,
        folderClicks: 0,
        queue: ['resume.txt'],
    });
    assert.equal(
        (ImportView.template.match(/:disabled="importConfigurationLoading \|\| uploading \|\| addingFiles"/g) || []).length >= 3,
        true,
    );
    assert.equal((ImportView.template.match(/:disabled="uploading(?: \|\| addingFiles)?"/g) || []).length >= 3, true);

    const secondFile = { ...fileOfSize(4), name: 'second.txt', lastModified: 3 };
    const second = {
        _key: 'second.txt|4|3',
        file: secondFile,
        name: secondFile.name,
        size: secondFile.size,
        status: 'pending',
        error: '',
        progress: 0,
        stageLabel: '',
    };
    view.queue.value = [existing, second];
    view.uploading.value = false;
    view.slug.value = 'public';

    const running = view.runQueue(false);
    await uploadStarted;
    view.queue.value.splice(view.queue.value.indexOf(second), 1);
    releaseUpload();
    await running;

    assert.equal(sessions.length, 1);
    assert.equal(harness.messages.success.at(-1), '上传完成：1 成功');
});
