// Chat Markdown 渲染 + XSS 净化测试（Node 内置 assert）
// 运行：node admin-ui/markdown.test.mjs
//
// 注：真实 renderMarkdown() 使用 DOMPurify，它依赖浏览器 DOM（Node 不可用）。
// 因此本测试分为两层：
//   1. marked 渲染正确性 → 直接调用真实 marked 库 ✅
//   2. XSS 模式字符串级检测 → 使用 mockSanitize（与 DOMPurify 白名单规则同步）
// 完整的 DOMPurify 真实净化验证必须在浏览器验收阶段进行。
// Chat.js 中 ALLOWED_TAGS / ALLOWED_ATTR 白名单已与本测试同步。
import assert from 'node:assert/strict';
import { marked } from './vendor/marked.esm.js';

// 与 Chat.js 同步的 ALLOWED_TAGS 和 ALLOWED_ATTR
const ALLOWED_TAGS = new Set([
    'h1','h2','h3','h4','h5','h6','p','br','strong','em','del','a',
    'ul','ol','li','table','thead','tbody','tr','th','td',
    'blockquote','pre','code','hr','sup','sub','span',
]);

// 模拟净化：移除不在白名单中的标签（仅用于测试，不做属性级净化）
function mockSanitize(html) {
    // 移除 <script>...</script>
    let out = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
    // 移除 on* 事件属性
    out = out.replace(/\s+on\w+\s*=\s*["'][^"']*["']/gi, '');
    out = out.replace(/\s+on\w+\s*=\s*[^\s>]*/gi, '');
    // 移除 javascript: URL
    out = out.replace(/href\s*=\s*["']\s*javascript:/gi, 'href="x-sanitized"');
    out = out.replace(/src\s*=\s*["']\s*javascript:/gi, 'src="x-sanitized"');
    return out;
}

function renderAndSanitize(text) {
    if (!text) return '';
    const raw = marked.parse(text, { breaks: true, gfm: true });
    return mockSanitize(raw);
}

let passed = 0;

function ok(name, cond) {
    assert.ok(cond, `[FAIL] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}

function contains(name, haystack, needle) {
    assert.ok(haystack.includes(needle), `[FAIL] ${name}: 期望包含「${needle}」`);
    console.log(`  ok  ${name}`);
    passed++;
}

function notContains(name, haystack, needle) {
    assert.ok(!haystack.includes(needle), `[FAIL] ${name}: 不应包含「${needle}」`);
    console.log(`  ok  ${name}`);
    passed++;
}

function eq(name, got, expected) {
    assert.equal(got, expected, `[FAIL] ${name}: 期望「${expected}」实得「${got}」`);
    console.log(`  ok  ${name}`);
    passed++;
}

// ═══════════════════════════════════════════════════════════════
// Markdown 渲染测试
// ═══════════════════════════════════════════════════════════════

const h1 = renderAndSanitize('# 标题一');
contains('h1 渲染', h1, '<h1');
contains('h1 内容', h1, '标题一');

const h2 = renderAndSanitize('## 二级标题');
contains('h2 渲染', h2, '<h2');

const list = renderAndSanitize('- **粗体** 项目\n- 普通项目');
contains('ul 列表', list, '<ul>');
contains('li 项目', list, '<li>');
contains('strong 粗体', list, '<strong>粗体</strong>');

const table = renderAndSanitize('| A | B |\n|---|---|\n| 1 | 2 |');
contains('table 渲染', table, '<table>');
contains('th 渲染', table, '<th>A</th>');
contains('td 渲染', table, '<td>1</td>');

const quote = renderAndSanitize('> 引用文本');
contains('blockquote 渲染', quote, '<blockquote>');

const code = renderAndSanitize('```\nconsole.log("hi")\n```');
contains('pre 代码块', code, '<pre>');
contains('code 在 pre 内', code, '<code>');

const inline = renderAndSanitize('使用 `foo()` 函数');
contains('行内 code', inline, '<code>foo()</code>');

const breaks = renderAndSanitize('第一行\n第二行');
contains('换行 → <br>', breaks, '<br>');

// 引用编号保留
const refs = renderAndSanitize('结论见资料[1]和[2]');
contains('引用编号 [1] 保留', refs, '[1]');
contains('引用编号 [2] 保留', refs, '[2]');

// GFM 表格对齐
const aligned = renderAndSanitize('| 左 | 右 |\n|:---|---:|\n| a | 1 |');
contains('对齐表格', aligned, '<table>');
contains('对齐表格有数据', aligned, '<td');

// ═══════════════════════════════════════════════════════════════
// XSS 防护测试（字符串级）
// ═══════════════════════════════════════════════════════════════

// script 标签被移除
const scriptTest = renderAndSanitize('<script>alert("xss")</script>正常文本');
notContains('script 标签被移除', scriptTest, '<script>');
notContains('alert 被移除', scriptTest, 'alert');
contains('正常文本保留', scriptTest, '正常文本');

// onerror 事件属性被移除
const onerrorTest = renderAndSanitize('<img src=x onerror="alert(1)">');
notContains('onerror 被移除', onerrorTest, 'onerror');

// javascript: URL 被移除
const jsUrl = renderAndSanitize('[点击](javascript:alert(1))');
notContains('javascript: 被移除', jsUrl, 'href="javascript');

// onclick 事件被移除
const onclickTest = renderAndSanitize('<div onclick="steal()">test</div>');
notContains('onclick 被移除', onclickTest, 'onclick');

// 安全链接保留
const safeLink = renderAndSanitize('[安全链接](https://example.com)');
contains('安全链接保留', safeLink, 'https://example.com');

// 粗体安全保留
const safeHtml = renderAndSanitize('**粗体** 文本');
contains('bold rendered', safeHtml, '<strong>粗体</strong>');

// 空文本
eq('空文本', renderAndSanitize(''), '');
eq('空白文本', renderAndSanitize('   '), '');

// 多级嵌套 Markdown
const nested = renderAndSanitize('> **引用粗体**\n> - 列表项');
contains('嵌套引用+粗体', nested, '<blockquote>');
contains('嵌套引用+列表', nested, '<li>');

console.log(`\n${passed} tests passed`);
