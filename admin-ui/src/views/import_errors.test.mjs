// 聚焦单测：humanizeError（Node 内置 assert，无第三方框架）。
// 运行：node admin-ui/src/views/import_errors.test.mjs
import assert from 'node:assert/strict';
import { humanizeError } from './import_errors.js';

const cases = [
    // —— 409 冲突分支 ——
    ['duplicate 409',
        { status: 409, message: '相同内容已存在，无法重复上传' },
        '相同内容已存在，请选择其他文件'],
    ['external 409 (英文 external)',
        { status: 409, message: 'library is managed by external source' },
        '该知识库由外部系统管理，不能在本系统上传'],
    ['external 409 (中文)',
        { status: 409, message: '该库由外部系统管理' },
        '该知识库由外部系统管理，不能在本系统上传'],
    ['unknown 409 (英文 detail → 通用文案)',
        { status: 409, message: 'conflict: something odd' },
        '上传发生冲突，请刷新后重试'],
    ['unknown 409 (安全中文 detail → 透传)',
        { status: 409, message: '该文档正在被其他任务占用' },
        '该文档正在被其他任务占用'],

    // —— 503 重建（不能被当成 409）——
    ['rebuilding 503',
        { status: 503, message: 'library index rebuilding' },
        '服务暂不可用，请稍后重试'],

    // —— 422 数组 detail：api.js 会把数组 stringify 成 [object Object]，不得外显 ——
    ['422 array detail 不显示 [object Object]',
        { status: 422, message: '[object Object],[object Object]' },
        '文件或参数不符合要求，请检查后重试'],
    ['422 relative path mismatch gives a safe action',
        { status: 422, body: { detail: { code: 'relative_path_mismatch' } } },
        '所选文件夹路径与文件名不一致，请重新选择原始文件夹'],
    ['422 invalid relative path gives a safe action',
        { status: 422, body: { detail: { code: 'invalid_relative_path' } } },
        '所选文件夹路径无效，请重新选择原始文件夹'],
    ['422 schema error names the rejected relative path field',
        { status: 422, body: { detail: [{ loc: ['body', 'relative_path'] }] } },
        '文件夹路径无效或过长，请缩短目录层级后重试'],
    ['422 schema error names the rejected batch field',
        { status: 422, body: { detail: [{ loc: ['body', 'batch_id'] }] } },
        '上传批次已失效，请刷新页面后重新选择文件'],
    ['422 unknown field preserves the safe message from api.js',
        { status: 422, message: '请求内容：格式不符合要求，请检查后重试' },
        '请求内容：格式不符合要求，请检查后重试'],
    ['422 includes the exact selected file path when session creation fails',
        {
            status: 422,
            body: { detail: [{ loc: ['body', 'size_bytes'] }] },
            uploadFileName: '错误文件.txt',
            uploadRelativePath: '市场营销部/错误文件.txt',
        },
        '市场营销部/错误文件.txt：文件大小参数无效，请重新选择文件后重试'],

    // —— 其它已覆盖状态 ——
    ['403', { status: 403, message: 'Forbidden' }, '你没有该知识库的上传权限'],
    ['413', { status: 413, message: 'Payload Too Large' }, '文件超过上传大小限制'],
    ['400', { status: 400, message: 'bad' }, '文件内容无法解析或不符合格式'],
    ['unknown status + 英文 detail → 通用',
        { status: 418, message: 'I am a teapot' }, '上传失败，请稍后重试'],
];

let passed = 0;
for (const [name, input, expected] of cases) {
    const got = humanizeError(input);
    assert.equal(got, expected, `[FAIL] ${name}: 期望「${expected}」实得「${got}」`);
    // 通用守卫：任何输出都不得包含 [object Object]
    assert.ok(!got.includes('[object Object]'), `[FAIL] ${name}: 输出含 [object Object]`);
    console.log(`  ok  ${name} -> ${got}`);
    passed++;
}
console.log(`\n${passed}/${cases.length} passed`);
