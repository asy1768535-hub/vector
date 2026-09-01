// api_errors 单测：统一错误解析（Node 内置 assert）
// 运行：node admin-ui/src/api_errors.test.mjs
import assert from 'node:assert/strict';
import { humanizeApiError } from './api_errors.js';

let passed = 0;

function ok(name, cond) {
    assert.ok(cond, `[FAIL] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}

function eq(name, got, expected) {
    assert.equal(got, expected, `[FAIL] ${name}: 期望「${expected}」实得「${got}」`);
    console.log(`  ok  ${name} -> ${got}`);
    passed++;
}

// ═══════════════════════════════════════════════════════════════
// humanizeApiError
// ═══════════════════════════════════════════════════════════════

// —— 422 数组 detail（英文 msg 自动翻译为中文）——
const email422 = {
    detail: [
        { loc: ['body', 'email'], msg: 'value is not a valid email address', type: 'value_error' },
    ],
};
eq('422 email 英文→中文', humanizeApiError(email422, 422),
    '邮箱：格式不正确，请输入合法邮箱');

const multi422 = {
    detail: [
        { loc: ['body', 'password'], msg: 'ensure this value has at least 8 characters', type: 'value_error' },
        { loc: ['body', 'username'], msg: 'ensure this value has at most 64 characters', type: 'value_error' },
    ],
};
eq('422 多字段英文→中文', humanizeApiError(multi422, 422),
    '密码：长度不够，至少需要 8 个字符；用户名：超出长度限制，最多 64 个字符');

// 中文 msg 直接透传
const cn422 = {
    detail: [
        { loc: ['body', 'email'], msg: '邮箱格式不正确' },
    ],
};
eq('422 中文 msg 透传', humanizeApiError(cn422, 422), '邮箱：邮箱格式不正确');

// 422 数组不产生 [object Object]
const arr422 = humanizeApiError(email422, 422);
ok('422 数组不含 [object Object]', !arr422.includes('[object Object]'));

// —— 字符串 detail ——
eq('中文 detail 透传', humanizeApiError({ detail: '邮箱已被注册' }, 400),
    '邮箱已被注册');
eq('英文 detail 回退通用', humanizeApiError({ detail: 'Bad Request' }, 400, '请求参数不正确'),
    '请求参数不正确');
eq('API Key 缺少组织时给出可操作提示',
    humanizeApiError({ detail: 'organization_id is required' }, 422),
    '请选择 API Key 所属组织');
eq('空 detail 回退', humanizeApiError({ detail: '' }, 500, '服务暂不可用'),
    '服务暂不可用');

eq('嵌套 detail.message 中文 → 透传',
    humanizeApiError({ detail: { code: 'upload_busy', message: '上传请求繁忙，请稍后重试' } }, 409),
    '上传请求繁忙，请稍后重试');
eq('嵌套 detail.message 英文 → 回退且不展示内部码',
    humanizeApiError(
        { detail: { code: 'upload_busy', message: 'another request owns this upload' } },
        409,
        '上传暂时繁忙',
    ),
    '上传暂时繁忙');

// —— 对象（无 detail 字段）——
eq('对象 message 中文 → 透传',
    humanizeApiError({ message: '该用户已存在' }, 400),
    '该用户已存在');
eq('对象 message 英文 → 回退',
    humanizeApiError({ message: 'User already exists' }, 400, '操作失败'),
    '操作失败');
eq('对象 error 字段',
    humanizeApiError({ error: '权限不足' }, 403),
    '权限不足');

// —— 非 JSON (string body) ——
eq('纯文本响应（非中文）',
    humanizeApiError('Internal Server Error', 500, '服务器错误'),
    '服务器错误');

// —— null body ——
eq('null body',
    humanizeApiError(null, 500, '服务暂不可用'),
    '服务暂不可用');

// —— 通用守卫：所有输出不含 [object Object] ——
const allTests = [
    humanizeApiError(email422, 422),
    humanizeApiError(multi422, 422),
    humanizeApiError({ detail: '中文' }, 400),
    humanizeApiError({ detail: 'english' }, 400, '回退文案'),
    humanizeApiError({ detail: { code: 'internal_code', message: 'internal failure' } }, 409, '回退文案'),
    humanizeApiError({ message: 'test' }, 400, 'fallback'),
    humanizeApiError('text', 500, 'fallback'),
    humanizeApiError(null, 500, 'fallback'),
];
for (const s of allTests) {
    ok(`输出不含 [object Object]: "${s.slice(0, 30)}..."`, !s.includes('[object Object]'));
}

console.log(`\n${passed} tests passed`);
