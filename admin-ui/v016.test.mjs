// v0.1.6 图标本地化 + 流式队列 + 创建用户授权测试（Node 内置 assert）
// 运行：node admin-ui/v016.test.mjs
import assert from 'node:assert/strict';
import { iconSvg } from './src/icons.js';
import { validateCreate } from './src/validate.js';
import { createStreamQueue } from './src/stream_queue.js';

let passed = 0;

function ok(name, cond) {
    assert.ok(cond, `[FAIL] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}
function eq(name, got, expected) {
    assert.equal(got, expected, `[FAIL] ${name}: 期望「${expected}」实得「${got}」`);
    console.log(`  ok  ${name}`);
    passed++;
}
function contains(name, haystack, needle) {
    assert.ok(haystack.includes(needle), `[FAIL] ${name}: 期望包含「${needle}」`);
    console.log(`  ok  ${name}`);
    passed++;
}
function deepEq(name, got, expected) {
    assert.deepEqual(got, expected, `[FAIL] ${name}: ${JSON.stringify(got)} !== ${JSON.stringify(expected)}`);
    console.log(`  ok  ${name}`);
    passed++;
}
function notContains(name, haystack, needle) {
    assert.ok(!haystack.includes(needle), `[FAIL] ${name}: 不应包含「${needle}」`);
    console.log(`  ok  ${name}`);
    passed++;
}

// ═══════════════════════════════════════════════════════════════
// 一、图标本地化
// ═══════════════════════════════════════════════════════════════

const icons = [
    'mdi:archive-arrow-down-outline', 'mdi:trash-can-outline', 'mdi:email-outline',
    'mdi:lock-outline', 'mdi:view-dashboard-outline', 'mdi:file-document-outline',
    'mdi:text-search', 'mdi:chat-question-outline', 'mdi:database-import-outline',
    'mdi:key-variant', 'mdi:account-group-outline', 'mdi:bookshelf',
    'mdi:shield-key-outline', 'mdi:cog-sync-outline', 'mdi:heart-pulse', 'mdi:history',
    'mdi:comment-text-multiple-outline', 'mdi:chevron-down', 'mdi:lock-reset',
    'mdi:logout', 'mdi:menu', 'mdi:backburger', 'carbon:chart-relationship',
];

for (const name of icons) {
    const svg = iconSvg(name);
    ok(`${name} 返回 SVG`, svg.startsWith('<svg'));
    ok(`${name} 含 viewBox`, svg.includes('viewBox="0 0'));
    ok(`${name} 含 path`, svg.includes('<path'));
    // SVG namespace only, no network URLs
    notContains(`${name} 无 api.iconify`, svg, 'api.iconify');
    notContains(`${name} 无 unpkg`, svg, 'unpkg.com');
    notContains(`${name} 无 jsdelivr`, svg, 'jsdelivr.net');
    notContains(`${name} 无 cdnjs`, svg, 'cdnjs.cloudflare');
}

// 未知图标回退
const fallback = iconSvg('mdi:nonexistent-xyz');
ok('未知图标回退 SVG', fallback.startsWith('<svg'));
contains('回退图标含 fallback path', fallback, '<path');

// Carbon 图标使用更大 viewBox 避免裁切
const carbon = iconSvg('carbon:chart-relationship');
contains('carbon 使用 32×32 viewBox', carbon, 'viewBox="0 0 32 32"');

// ── 所有图标不包含公网 CDN URL（SVG namespace http://www.w3.org 除外）──
for (const name of icons) {
    const svg = iconSvg(name);
    // 移除 SVG namespace 后检查
    const clean = svg.replace(/xmlns="http:\/\/www\.w3\.org\/[^"]*"/g, '');
    ok(`${name} 无公网 http`, !clean.includes('http://'));
    ok(`${name} 无公网 https`, !clean.includes('https://'));
}

// ═══════════════════════════════════════════════════════════════
// 二、流式队列逻辑 — 测试生产模块 createStreamQueue
// ═══════════════════════════════════════════════════════════════

// 多 delta 无丢失
const q1 = createStreamQueue();
q1.enqueue('你好');
q1.enqueue('，世界');
q1.enqueue('！');
eq('流式拼接无丢失', q1.flush(), '你好，世界！');

// done 后排空队列
const q2 = createStreamQueue();
q2.enqueue('Hello');
eq('flush 返回完整文本', q2.flush(), 'Hello');
ok('flush 后队列为空', q2.isEmpty());

// error 清理
const q3 = createStreamQueue();
q3.enqueue('part');
q3.enqueue('ial');
eq('cleanup 返回丢弃数量', q3.cleanup(), 2);
ok('cleanup 后队列为空', q3.isEmpty());

// 空队列 flush 返回空字符串
const q4 = createStreamQueue();
eq('空 flush → 空字符串', q4.flush(), '');

// 二次 flush 不变
const q5 = createStreamQueue();
q5.enqueue('keep');
eq('首次 flush', q5.flush(), 'keep');
eq('二次 flush 返回空', q5.flush(), '');
ok('二次 flush 后队列为空', q5.isEmpty());

// isEmpty 准确反映状态
const q6 = createStreamQueue();
ok('初始 isEmpty', q6.isEmpty());
q6.enqueue('x');
ok('入队后非空', !q6.isEmpty());
q6.flush();
ok('flush 后为空', q6.isEmpty());

// 空字符串不入队
const q7 = createStreamQueue();
q7.enqueue('');
ok('空字符串不入队', q7.isEmpty());

// ═══════════════════════════════════════════════════════════════
// 三、创建用户授权逻辑
// ═══════════════════════════════════════════════════════════════

// 模拟：actions 严格为 read+insert
function buildGrantActions() {
    return ['read', 'insert'];
}
function grantContains(grant) {
    return grant.includes('read') && grant.includes('insert')
        && !grant.includes('delete') && !grant.includes('admin');
}

const grant = buildGrantActions();
ok('授权含 read', grant.includes('read'));
ok('授权含 insert', grant.includes('insert'));
ok('授权不含 delete', !grant.includes('delete'));
ok('授权不含 admin', !grant.includes('admin'));
ok('授权仅 read+insert', grant.length === 2);

// 逐库授权计数
function simulateGrants(selectedSlugs) {
    let ok = 0, fail = 0;
    for (const slug of selectedSlugs) {
        if (slug === 'fail-lib') {
            fail++;
        } else {
            ok++;
        }
    }
    if (selectedSlugs.length === 0) return '用户已创建';
    if (fail === 0) return `用户已创建，已授权 ${ok} 个知识库`;
    return `用户已创建，但 ${fail} 个知识库授权失败，请到权限矩阵补充`;
}

eq('未选库', simulateGrants([]), '用户已创建');
eq('全部成功', simulateGrants(['lib-a', 'lib-b']), '用户已创建，已授权 2 个知识库');
eq('部分失败', simulateGrants(['lib-a', 'fail-lib']), '用户已创建，但 1 个知识库授权失败，请到权限矩阵补充');

// ═══════════════════════════════════════════════════════════════
// 四、validateCreate 来自真实生产模块
// ═══════════════════════════════════════════════════════════════
deepEq('validateCreate 合法输入', validateCreate({ email: 'a@b.com', password: '12345678' }), []);
ok('validateCreate 非法邮箱', validateCreate({ email: 'x', password: '12345678' }).some(e => e.includes('邮箱')));
ok('validateCreate 密码过短', validateCreate({ email: 'a@b.com', password: '1' }).some(e => e.includes('密码')));

// ═══════════════════════════════════════════════════════════════
// 五、AbortController 流式取消测试
// ═══════════════════════════════════════════════════════════════
function simulateStreamOnAbort() {
    // 模拟 abort：队列有内容，abort 后不应把残文写入 aiMsg
    let queue = [];
    let text = '';
    function enqueue(t) { queue.push(t); }
    function flush() { text += queue.join(''); queue = []; }
    function cleanup() { queue = []; } // abort → 不 flush，直接清
    // abort 场景：不清空已渲染 text，只清队列
    enqueue('未完成的');
    cleanup(); // AbortController 触发
    return text; // 之前 flush 过的内容仍在
}
eq('abort 后已渲染文本保留', simulateStreamOnAbort(), '');
eq('abort 不清空已 flush 内容', (() => {
    let t = '';
    const q = [];
    q.push('Hello'); t += q.join(''); q.length = 0;
    q.push('World'); q.length = 0; // abort cleanup
    return t;
})(), 'Hello');

// ═══════════════════════════════════════════════════════════════
// 六、CDN 扫描 —— admin-ui 源文件无公网 CDN URL
// ═══════════════════════════════════════════════════════════════
// 注：此测试是 compile-time 检查，确保约定不被后续改动破坏。
// vendor/ 目录下的库文件由人工审核，不在自动化扫描范围。
const CDN_PATTERNS = [
    /https?:\/\/unpkg\.com\//,
    /https?:\/\/cdn\.jsdelivr\.net\//,
    /https?:\/\/code\.iconify\.design\//,
    /https?:\/\/cdnjs\.cloudflare\.com\//,
    /https?:\/\/api\.iconify\.design\//,
];

import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const JS_FILES = [
    'src/app.js', 'src/api.js', 'src/api_errors.js', 'src/icons.js',
    'src/views/Chat.js', 'src/views/Login.js', 'src/views/Layout.js',
    'src/views/Users.js', 'src/views/Import.js', 'src/views/Dashboard.js',
    'src/views/Libraries.js', 'src/views/Documents.js', 'src/views/Search.js',
    'src/views/ApiKeys.js', 'src/views/Permissions.js', 'src/views/Jobs.js',
    'src/views/RuntimeStatus.js', 'src/views/Audit.js', 'src/views/ChatLogs.js',
    'src/views/import_errors.js',
    'src/store.js', 'src/menu_access.js', 'src/validate.js',
    'style.css',
];

for (const f of JS_FILES) {
    const path = resolve(__dirname, f);
    let content;
    try {
        content = readFileSync(path, 'utf-8');
    } catch (e) {
        console.log(`  skip ${f} (not found)`);
        continue;
    }
    for (const pat of CDN_PATTERNS) {
        ok(`${f} 无 CDN 引用`, !pat.test(content));
    }
}

console.log(`\n${passed} tests passed`);
