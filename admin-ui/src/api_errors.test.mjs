// api_errors 单测：统一错误解析（Node 内置 assert）
// 运行：node admin-ui/src/api_errors.test.mjs
import assert from 'node:assert/strict';
import { humanizeApiError, humanizeFetchError } from './api_errors.js';

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
eq('空 detail 回退', humanizeApiError({ detail: '' }, 500, '服务暂不可用'),
    '服务暂不可用');

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

// ═══════════════════════════════════════════════════════════════
// humanizeFetchError
// ═══════════════════════════════════════════════════════════════

eq('fetch 400', humanizeFetchError({ status: 400 }), '请求参数不正确，请检查输入');
eq('fetch 401', humanizeFetchError({ status: 401 }), '登录已过期，请重新登录');
eq('fetch 403', humanizeFetchError({ status: 403 }), '你没有执行此操作的权限');
eq('fetch 404', humanizeFetchError({ status: 404 }), '请求的资源不存在');
eq('fetch 413', humanizeFetchError({ status: 413 }), '上传文件超过大小限制');
eq('fetch 415', humanizeFetchError({ status: 415 }), '不支持的文件类型');
eq('fetch 429', humanizeFetchError({ status: 429 }), '请求过于频繁，请稍后重试');
eq('fetch 500', humanizeFetchError({ status: 500 }), '服务器内部错误，请稍后重试');
eq('fetch 503', humanizeFetchError({ status: 503 }), '服务暂不可用，请稍后重试');

// 409：通过 body.detail 细分
eq('fetch 409 中文 detail',
    humanizeFetchError({ status: 409, body: { detail: '相同内容已存在' } }),
    '相同内容已存在');
eq('fetch 409 英文 detail → 回退',
    humanizeFetchError({ status: 409, body: { detail: 'conflict' } }),
    '请求发生冲突，请刷新后重试');

// 422 通过 body.detail 数组处理（未知英文 msg → 安全回退中文）
eq('fetch 422 数组',
    humanizeFetchError({ status: 422, body: { detail: [{ loc: ['body', 'display_name'], msg: 'too long' }] } }),
    '显示名：格式不符合要求，请检查后重试');

// null
eq('fetch null', humanizeFetchError(null), '发生未知错误');

// —— 通用守卫：所有输出不含 [object Object] ——
const allTests = [
    humanizeApiError(email422, 422),
    humanizeApiError(multi422, 422),
    humanizeApiError({ detail: '中文' }, 400),
    humanizeApiError({ detail: 'english' }, 400, '回退文案'),
    humanizeApiError({ message: 'test' }, 400, 'fallback'),
    humanizeApiError('text', 500, 'fallback'),
    humanizeApiError(null, 500, 'fallback'),
    humanizeFetchError({ status: 422, body: email422 }),
    humanizeFetchError({ status: 409, body: {} }),
];
for (const s of allTests) {
    ok(`输出不含 [object Object]: "${s.slice(0, 30)}..."`, !s.includes('[object Object]'));
}

console.log(`\n${passed} tests passed`);
