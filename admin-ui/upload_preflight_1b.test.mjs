import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { humanizeApiError } from './src/api_errors.js';
import * as importUi from './src/import_ui.js';
import { humanizeError } from './src/views/import_errors.js';


const importSource = readFileSync(
    new URL('./src/views/Import.js', import.meta.url),
    'utf8',
);
const configuration = {
    allowed_extensions: ['.pdf', '.xlsx'],
    max_file_bytes: 1024 * 1024,
    max_files_per_selection: 100,
};

function mockFile(name, lastModified = 1, webkitRelativePath = '') {
    return {
        name,
        size: 128,
        lastModified,
        webkitRelativePath,
    };
}

function functionSource(source, startToken, endToken) {
    const start = source.indexOf(startToken);
    const end = source.indexOf(endToken, start + startToken.length);
    assert.notEqual(start, -1, `missing source token: ${startToken}`);
    assert.notEqual(end, -1, `missing source token: ${endToken}`);
    return source.slice(start, end);
}

function uploadError(body) {
    const message = humanizeApiError(body, 415, '上传失败（HTTP 415）');
    return Object.assign(new Error(message), { status: 415, body });
}


test('macOS metadata and Office lock files have stable ignored codes and reasons', () => {
    const files = [
        mockFile('._contract.pdf', 1, 'contracts/._contract.pdf'),
        mockFile('~$budget.xlsx', 2, 'finance/~$budget.xlsx'),
    ];
    const first = importUi.validateBatch(files, [], configuration);
    const second = importUi.validateBatch(files, [], configuration);

    assert.ok(Array.isArray(first.ignored), 'validateBatch must expose an ignored array');
    assert.deepEqual(
        first.ignored.map(({ code }) => code),
        ['metadata_file', 'office_lock_file'],
    );
    assert.match(first.ignored[0].reason, /macOS.*元数据/);
    assert.match(first.ignored[1].reason, /Office.*临时锁文件/);
    assert.deepEqual(
        first.ignored.map(({ code, reason }) => ({ code, reason })),
        second.ignored.map(({ code, reason }) => ({ code, reason })),
        'ignored classification must be deterministic',
    );
});

test('validateBatch separates ignored files from accepted and invalid files', () => {
    const files = [
        mockFile('report.pdf', 1),
        mockFile('._report.pdf', 2),
        mockFile('~$report.xlsx', 3),
    ];
    const result = importUi.validateBatch(files, [], configuration);

    assert.deepEqual(result.accepted.map(({ file }) => file.name), ['report.pdf']);
    assert.deepEqual(result.invalid, []);
    assert.deepEqual(
        result.ignored?.map(({ file, code }) => [file.name, code]),
        [
            ['._report.pdf', 'metadata_file'],
            ['~$report.xlsx', 'office_lock_file'],
        ],
    );
});

test('ignored files do not consume the configured batch capacity', () => {
    const files = [
        mockFile('._report.pdf', 1),
        mockFile('~$budget.xlsx', 2),
        mockFile('report.pdf', 3),
    ];
    const result = importUi.validateBatch(files, [], {
        ...configuration,
        max_files_per_selection: 1,
    });

    assert.deepEqual(result.accepted.map(({ file }) => file.name), ['report.pdf']);
    assert.deepEqual(result.invalid, []);
    assert.deepEqual(
        result.ignored.map(({ code }) => code),
        ['metadata_file', 'office_lock_file'],
    );
});

test('ordinary and merely similar file names are not ignored', () => {
    const files = [
        mockFile('report.pdf', 1),
        mockFile('.hidden.pdf', 2),
        mockFile('copy._report.pdf', 3),
        mockFile('~report.xlsx', 4),
        mockFile('budget~$.xlsx', 5),
    ];
    const result = importUi.validateBatch(files, [], configuration);

    assert.deepEqual(
        result.accepted.map(({ file }) => file.name),
        files.map(({ name }) => name),
    );
    assert.deepEqual(result.invalid, []);
    assert.deepEqual(result.ignored ?? [], []);
});

test('Import.js summarizes ignored files without queueing or creating upload sessions', () => {
    const addFilesSource = functionSource(
        importSource,
        'function addFiles(files)',
        'function onFileChange',
    );
    const runQueueSource = functionSource(
        importSource,
        'async function runQueue(onlyFailed)',
        '// ── Replace mode',
    );
    const batchBinding = addFilesSource.match(
        /const\s*\{([^}]*)\}\s*=\s*validateBatch(?:Chunk)?\s*\(/,
    );

    assert.ok(batchBinding, 'addFiles must consume batch validation result');
    assert.match(batchBinding[1], /\bignored\b/);
    assert.match(addFilesSource, /ignored\.length/);
    assert.match(addFilesSource, /已忽略/);

    const queuePushes = addFilesSource.match(/queue\.value\.push\(\{[\s\S]*?\}\);/g) || [];
    assert.equal(queuePushes.length, 2, 'only accepted and invalid files belong in queue');
    assert.match(queuePushes[0], /status:\s*'pending'/);
    assert.match(queuePushes[1], /status:\s*'invalid'/);
    assert.doesNotMatch(addFilesSource, /status:\s*['"]ignored['"]/);
    assert.doesNotMatch(addFilesSource, /createImportSession|uploadFileInChunks/);

    assert.match(
        runQueueSource,
        /const filterStatus = onlyFailed \? 'failed' : 'pending'/,
    );
    assert.doesNotMatch(runQueueSource, /\bignored\b/);
});

test('Import.js reports ignored metadata and lock-file counts separately', () => {
    const addFilesSource = functionSource(
        importSource,
        'function addFiles(files)',
        'function onFileChange',
    );

    assert.match(addFilesSource, /code\s*===\s*['"]metadata_file['"]/);
    assert.match(addFilesSource, /code\s*===\s*['"]office_lock_file['"]/);
    assert.match(addFilesSource, /macOS 元数据/);
    assert.match(addFilesSource, /Office 临时锁文件/);
});

test('known Chinese 415 guidance survives the API and Import page error layers', () => {
    const cases = [
        {
            code: 'encrypted_office_file',
            message: '检测到加密的 Office 文件，请在本地解密后重新上传',
        },
        {
            code: 'file_signature_mismatch',
            message: '文件后缀与实际格式不一致，请改为 .xlsx 后重新上传',
            suggested_extension: '.xlsx',
        },
    ];

    for (const expected of cases) {
        const error = uploadError({ detail: expected });
        assert.equal(error.message, expected.message);
        assert.equal(humanizeError(error), expected.message);
    }
});

test('unknown English 415 detail keeps the existing generic message', () => {
    const error = uploadError({
        detail: {
            code: 'unknown_media_type',
            message: 'unsupported binary container',
        },
    });

    assert.equal(humanizeError(error), '暂不支持该文件类型');
});

test('unknown 415 codes never expose Chinese paths passwords or mixed diagnostics', () => {
    const messages = [
        '文件 local-private-budget.xlsx 无法解析',
        '解密失败，密码是 P@ssword-123',
        'parser stack /srv/private/upload.xlsx 解析失败',
        'upload_preflight:v1:cleanup_pending:encrypted_office_file:内部诊断',
    ];

    for (const message of messages) {
        const rendered = humanizeError(uploadError({
            detail: { code: 'unknown_preflight_error', message },
        }));
        assert.equal(rendered, '暂不支持该文件类型');
        assert.equal(rendered.includes(message), false);
        assert.equal(rendered.includes('upload_preflight:v1:'), false);
    }
});

test('known 415 codes use fixed copy instead of untrusted server messages', () => {
    const maliciousMessage = (
        'upload_preflight:v1:cleanup_pending:encrypted_office_file:'
        + '密码 P@ssword-123，路径 C:\\private\\secret.xlsx'
    );
    const cases = [
        ['metadata_file', 'macOS 元数据文件，已忽略'],
        ['office_lock_file', 'Office 临时锁文件，已忽略'],
        ['encrypted_office_file', '检测到加密的 Office 文件，请在本地解密后重新上传'],
        ['file_signature_unconfirmed', '无法确认文件的真实格式，请检查文件后重新上传'],
    ];

    for (const [code, expected] of cases) {
        const rendered = humanizeError(uploadError({
            detail: { code, message: maliciousMessage },
        }));
        assert.equal(rendered, expected);
        assert.equal(rendered.includes('P@ssword-123'), false);
        assert.equal(rendered.includes('C:\\private'), false);
        assert.equal(rendered.includes('upload_preflight:v1:'), false);
    }

    const mismatch = humanizeError(uploadError({
        detail: {
            code: 'file_signature_mismatch',
            message: maliciousMessage,
            suggested_extension: '.xlsx',
        },
    }));
    assert.equal(mismatch, '文件后缀与实际格式不一致，请改为 .xlsx 后重新上传');
    assert.equal(mismatch.includes('P@ssword-123'), false);
});

test('signature mismatch only renders allowlisted suggested extensions', () => {
    for (const suggestedExtension of ['.doc', '.docx', '.xls', '.xlsx', '.pptx']) {
        const rendered = humanizeError(uploadError({
            detail: {
                code: 'file_signature_mismatch',
                message: 'untrusted',
                suggested_extension: suggestedExtension,
            },
        }));
        assert.equal(
            rendered,
            `文件后缀与实际格式不一致，请改为 ${suggestedExtension} 后重新上传`,
        );
    }

    for (const suggestedExtension of ['.exe', '../../etc/passwd', '.xlsx<script>']) {
        const rendered = humanizeError(uploadError({
            detail: {
                code: 'file_signature_mismatch',
                message: '路径 /srv/private/upload 中的文件后缀错误',
                suggested_extension: suggestedExtension,
            },
        }));
        assert.equal(rendered, '文件后缀与实际格式不一致，请检查后重新上传');
        assert.equal(rendered.includes(suggestedExtension), false);
        assert.equal(rendered.includes('/srv/private'), false);
    }
});
