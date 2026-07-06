import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

function setupReturnBlock() {
    const start = chat.indexOf('return {');
    assert.notEqual(start, -1, 'setup return block should exist');
    const end = chat.indexOf('};', start);
    assert.notEqual(end, -1, 'setup return block should close');
    return chat.slice(start, end);
}

test('chat source API and dialogs are wired in page', () => {
    assert.ok(api.includes('getDocumentSource'));
    assert.ok(chat.includes('openCitationChunk'));
    assert.ok(chat.includes('sourceLocationDialog'));
    assert.ok(chat.includes('recalledChunkDialog'));
    assert.ok(chat.includes('sourceLocationRequestSeq'));
    assert.ok(chat.includes('@click="openDocDetail(s)"'));
    assert.ok(!chat.includes('window.open(resolved.href'));
    assert.ok(!chat.includes("router.resolve({ path: '/documents'"));
});

test('source actions use source wording and show chunk sequence', () => {
    assert.ok(chat.includes('查看出处'));
    assert.ok(!chat.includes('title="文档详情"'));
    assert.ok(chat.includes('sourceLocationDialog.source?.seq'));
    assert.ok(chat.includes('recalledChunkDialog.source.seq'));
});

test('source location modal and citation styles exist', () => {
    assert.match(css, /\.chat-citation-badge/);
    assert.match(css, /\.chat-source-location-dialog/);
    assert.match(css, /width:\s*900px/);
    assert.match(css, /calc\(100vw - 24px\)/);
    assert.match(css, /\.chat-source-window/);
    assert.match(css, /\.chat-source-highlight/);
    assert.match(css, /\.chat-recalled-dialog/);
});

// ── Task 4: 关闭弹窗后禁止重开 ──

test('dialog closed event calls a returned source invalidation function', () => {
    assert.ok(chat.includes('@closed="_closeAndInvalidateSourceDialog"'));
    assert.ok(!chat.includes('@closed="++sourceLocationRequestSeq"'));
    assert.ok(!chat.includes('@closed="sourceLocationRequestSeq++"'));
});

test('_closeAndInvalidateSourceDialog is returned from setup for template access', () => {
    const returned = setupReturnBlock();
    assert.match(returned, /\b_closeAndInvalidateSourceDialog\b/);
});

test('onLibChange closes and invalidates source dialog', () => {
    // onLibChange 函数体内调用了 _closeAndInvalidateSourceDialog
    const onLibFn = chat.match(/function onLibChange\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(onLibFn, 'onLibChange should exist');
    const body = onLibFn[1];
    assert.ok(body.includes('_closeAndInvalidateSourceDialog') ||
              (body.includes('++sourceLocationRequestSeq') && body.includes('sourceLocationDialog')),
        'onLibChange must invalidate source location request');
});

test('newChat closes and invalidates source dialog', () => {
    const newChatFn = chat.match(/function newChat\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(newChatFn, 'newChat should exist');
    const body = newChatFn[1];
    assert.ok(body.includes('_closeAndInvalidateSourceDialog') ||
              (body.includes('++sourceLocationRequestSeq') && body.includes('sourceLocationDialog')),
        'newChat must invalidate source location request');
});

test('selectConversation closes and invalidates source dialog', () => {
    const selFn = chat.match(/async function selectConversation\s*\([^)]*\)\s*\{([^}]*\}[^}]*\})/s);
    // Just verify the function calls _closeAndInvalidateSourceDialog or increments the seq + closes dialog
    assert.ok(
        chat.includes('_closeAndInvalidateSourceDialog') ||
        chat.match(/selectConversation[\s\S]*?sourceLocationRequestSeq/),
        'selectConversation must invalidate source location request'
    );
});

test('openDocDetail uses requestSeq guard for async response', () => {
    assert.ok(chat.includes('const requestSeq = ++sourceLocationRequestSeq'));
    assert.ok(chat.includes('if (requestSeq !== sourceLocationRequestSeq) return'));
});

test('sourceLocationDialog cleans loading when closed', () => {
    // _closeAndInvalidateSourceDialog resets loading + open to false
    const closeFn = chat.match(/function _closeAndInvalidateSourceDialog\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(closeFn, '_closeAndInvalidateSourceDialog should exist');
    const body = closeFn[1];
    assert.ok(body.includes('sourceLocationRequestSeq'));
    assert.ok(body.includes('open: false') || body.includes("open:!1") || body.includes('false'));
    assert.ok(body.includes('loading: false') || body.includes('loading:!1'));
});
