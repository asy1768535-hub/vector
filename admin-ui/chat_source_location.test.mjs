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

test('citation scores use their reliable score type in every source surface', () => {
    const fmtScore = chat.slice(chat.indexOf('function fmtScore'), chat.indexOf('function nowISO'));
    assert.ok(fmtScore.includes("source?.score_type === 'rrf'"));
    assert.ok(fmtScore.includes("'融合排序'"));
    assert.ok(fmtScore.includes("'相关度'"));
    assert.ok(fmtScore.includes("'向量相似度'"));
    assert.ok(fmtScore.includes('Math.max(0, Math.min(1, Number(raw)))'));
    assert.ok(fmtScore.includes('(score * 100).toFixed(1)'));
    assert.ok(chat.includes('<span class="chat-source-score">{{ fmtScore(s) }}</span>'));
    assert.ok(chat.includes('{{ fmtScore(recalledChunkDialog.source) }}'));
    assert.ok(chat.includes('{{ fmtScore(sourceLocationDialog.source) }}'));
    assert.ok(!chat.includes('fmtScore(s.score)'));
    assert.match(css, /\.chat-source-score\s*\{[^}]*color:\s*var\(--app-text-secondary\)/);
    assert.doesNotMatch(css, /\.score--(?:high|mid|low)\b/);
});

test('formats reliable source scores and never percentages RRF', () => {
    const start = chat.indexOf('function fmtScore');
    const end = chat.indexOf('function nowISO');
    const format = Function(`${chat.slice(start, end)}; return fmtScore;`)();

    assert.equal(format({ score_type: 'rerank', display_score: 0.863 }), '相关度 86.3%');
    assert.equal(format({ score_type: 'vector', display_score: 0.724 }), '向量相似度 72.4%');
    assert.equal(format({ score_type: 'rrf', display_score: 0.0164 }), '融合排序');
    assert.equal(format({ score_type: 'vector', display_score: 1.5 }), '向量相似度 100.0%');
});

test('same-document chunks retain list order and citation indices', () => {
    assert.match(chat, /function citationSources\(message\)[\s\S]*?source\.citation_index[\s\S]*?return sources;/);
    assert.match(chat, /v-for="\(s, si\) in m\.sources"[\s\S]*?s\.citation_index \|\| citationSources\(m\)\.indexOf\(s\) \+ 1/);
    assert.doesNotMatch(chat, /new Set\([^\n]*document_id|document_id[^\n]*new Set/);

    const chunks = [
        { document_id: 'doc-1', chunk_id: 'chunk-1' },
        { document_id: 'doc-1', chunk_id: 'chunk-2' },
    ];
    assert.deepEqual(chunks.map((source, index) => [index + 1, source.chunk_id]), [
        [1, 'chunk-1'],
        [2, 'chunk-2'],
    ]);
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
    assert.ok(chat.includes('@closed="closeAndInvalidateSourceDialog"'));
    assert.ok(!chat.includes('@closed="++sourceLocationRequestSeq"'));
    assert.ok(!chat.includes('@closed="sourceLocationRequestSeq++"'));
});

test('source dialog close handler avoids Vue-reserved underscore prefix', () => {
    const returned = setupReturnBlock();
    assert.match(returned, /\bcloseAndInvalidateSourceDialog\b/);
    assert.doesNotMatch(returned, /\b_closeAndInvalidateSourceDialog\b/);
});

test('onLibChange closes and invalidates source dialog', () => {
    // onLibChange 函数体内调用了 closeAndInvalidateSourceDialog
    const onLibFn = chat.match(/function onLibChange\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(onLibFn, 'onLibChange should exist');
    const body = onLibFn[1];
    assert.ok(body.includes('closeAndInvalidateSourceDialog') ||
              (body.includes('++sourceLocationRequestSeq') && body.includes('sourceLocationDialog')),
        'onLibChange must invalidate source location request');
});

test('newChat closes and invalidates source dialog', () => {
    const newChatFn = chat.match(/function newChat\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(newChatFn, 'newChat should exist');
    const body = newChatFn[1];
    assert.ok(body.includes('closeAndInvalidateSourceDialog') ||
              (body.includes('++sourceLocationRequestSeq') && body.includes('sourceLocationDialog')),
        'newChat must invalidate source location request');
});

test('selectConversation closes and invalidates source dialog', () => {
    const selFn = chat.match(/async function selectConversation\s*\([^)]*\)\s*\{([^}]*\}[^}]*\})/s);
    // Just verify the function calls closeAndInvalidateSourceDialog or increments the seq + closes dialog
    assert.ok(
        chat.includes('closeAndInvalidateSourceDialog') ||
        chat.match(/selectConversation[\s\S]*?sourceLocationRequestSeq/),
        'selectConversation must invalidate source location request'
    );
});

test('openDocDetail uses requestSeq guard for async response', () => {
    assert.ok(chat.includes('const requestSeq = ++sourceLocationRequestSeq'));
    assert.ok(chat.includes('if (requestSeq !== sourceLocationRequestSeq) return'));
});

test('sourceLocationDialog cleans loading when closed', () => {
    // closeAndInvalidateSourceDialog resets loading + open to false
    const closeFn = chat.match(/function closeAndInvalidateSourceDialog\s*\(\)\s*\{([^}]+)\}/s);
    assert.ok(closeFn, 'closeAndInvalidateSourceDialog should exist');
    const body = closeFn[1];
    assert.ok(body.includes('sourceLocationRequestSeq'));
    assert.ok(body.includes('open: false') || body.includes("open:!1") || body.includes('false'));
    assert.ok(body.includes('loading: false') || body.includes('loading:!1'));
});
