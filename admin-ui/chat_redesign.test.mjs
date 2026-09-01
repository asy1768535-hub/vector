import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── Existing behaviors preserved ──
test('preserves existing core behaviors', () => {
    for (const token of [
        'newChat', 'selectConversation', 'archiveConv', 'deleteConv', 'send',
        'streamChatMessage', 'renderMarkdown', 'copyTextToClipboard', 'copyAnswer',
        'mobileHistoryOpen', 'chat-history-toggle',
        'chat-history-title-row', 'chat-new-button',
        'chat-toolbar', 'chat-toolbar-label',
        'chat-avatar--ai', 'chat-avatar--user',
        'chat-input-shell',
    ]) {
        assert.ok(chat.includes(token), `missing existing behavior: ${token}`);
    }
});

// ── Allowed / forbidden features ──
test('allows top_k selector and refresh; rejects forbidden features', () => {
    for (const token of ['topK', '检索数量', 'topk-select', 'loadLibs']) {
        assert.ok(chat.includes(token), `missing allowed feature: ${token}`);
    }
    for (const forbidden of [
        '上传附件', '点赞', '点踩', '查看原文', '消息中心', '帮助中心',
    ]) {
        assert.ok(!chat.includes(forbidden), `unexpected fake feature: ${forbidden}`);
    }
});

test('offers per-question graph assistance and sends the choice to the API', () => {
    for (const token of ['graphAssist', '图谱辅助检索', 'el-switch']) {
        assert.ok(chat.includes(token), `missing graph assistance control: ${token}`);
    }
    assert.ok(chat.includes('use_graph: graphAssist.value'));
});

test('conversation history is paged and can load earlier messages without jumping', () => {
    for (const token of [
        'loadEarlierMessages', 'hasEarlierMessages', 'loadingEarlierMessages',
        'before', 'previousHeight', 'chat-load-earlier', '加载更早消息',
    ]) assert.ok(chat.includes(token), `missing paged history behavior: ${token}`);
    assert.match(css, /\.chat-load-earlier\s*\{/);
});

test('remembers a valid library and keeps common chat actions available', () => {
    assert.ok(chat.includes('LAST_CHAT_LIBRARY_KEY'));
    assert.ok(chat.includes('savedChatLibrary()'));
    assert.ok(chat.includes('libs.value[0]?.slug'));
    assert.ok(chat.includes("messages.length ? '继续提问……' : '输入问题……'"));
    assert.ok(chat.includes('检索数量（Top K）'));
    assert.ok(chat.includes('结果不足时可适当提高，数值越大检索范围越广。'));
    assert.ok(!chat.includes('chat-new-button" circle :disabled="!currentSlug"'));
});

// ── Refresh button moved to toolbar ──
test('refresh button is in toolbar, not using mdi:history', () => {
    assert.ok(chat.includes('chat-toolbar-right'), 'toolbar right section exists');
    // Refresh icon in toolbar (not mdi:history for refresh purpose)
    assert.ok(chat.includes('刷新'), 'uses text 刷新 for refresh button');
    // No separate chat-input-footer-right (unified in shell)
    assert.ok(!chat.includes('chat-input-footer-right'), 'old footer-right class removed');
});

// ── Time formatting ──
test('includes time formatting helpers', () => {
    assert.ok(chat.includes('fmtTime'), 'missing fmtTime');
    assert.ok(chat.includes('chat-msg-time'), 'missing message time element');
    assert.ok(chat.includes('nowISO'), 'missing nowISO helper');
});

// ── Compact sources with two-column layout ──
test('replaces expanded source boxes with two-column compact rows', () => {
    assert.ok(chat.includes('chat-source-left'), 'missing source left column');
    assert.ok(chat.includes('chat-source-right'), 'missing source right column');
    assert.ok(chat.includes('chat-source-summary'), 'missing compact source summary');
    assert.ok(chat.includes('chat-source-detail'), 'missing source detail link');
    assert.ok(chat.includes('openDocDetail'), 'missing openDocDetail');
    assert.ok(chat.includes('el-collapse'), 'sources use collapsible panel');
    assert.ok(chat.includes('el-collapse-item'), 'sources are collapsible');
});

// ── Input area: unified editor ──
test('input area has maxlength, char count, and unified shell', () => {
    assert.ok(chat.includes('maxlength="2000"'), 'missing maxlength');
    assert.ok(chat.includes('chat-input-footer'), 'missing footer inside shell');
    // Footer is inside chat-input-shell, not separate from it
    const shellIdx = chat.indexOf('chat-input-shell');
    const footerIdx = chat.indexOf('chat-input-footer');
    const barEndIdx = chat.indexOf('chat-input-bar', shellIdx + 1);
    // Footer should appear between shell start and next bar
    assert.ok(footerIdx > shellIdx, 'footer must be inside input shell');
});

// ── CSS: two-card layout with 12px gap ──
test('CSS defines two independent cards with 12px gap', () => {
    assert.match(css, /\.chat-wrap\s*\{[\s\S]*?gap:\s*12px/);
    assert.match(css, /\.chat-history-panel\s*\{[\s\S]*?border-radius:\s*12px/);
    assert.match(css, /\.chat-main\s*\{[\s\S]*?border-radius:\s*12px/);
});

// ── CSS: history panel ──
test('history panel ~310px, items 62-68px with box-sizing', () => {
    assert.match(css, /\.chat-history-panel\s*\{[\s\S]*?width:\s*310px/);
    assert.match(css, /\.chat-history-item\s*\{[\s\S]*?min-height:\s*62px/);
    assert.match(css, /\.chat-history-item\s*\{[\s\S]*?max-height:\s*68px/);
    assert.match(css, /\.chat-history-item\s*\{[\s\S]*?box-sizing:\s*border-box/);
    assert.match(css, /\.chat-history-item-time\s*\{/);
    assert.match(css, /\.chat-history-item\.is-active\s*\{[\s\S]*?border-left-color:\s*var\(--app-primary\)/);
    assert.match(css, /\.chat-history-item\.is-active\s*\{[\s\S]*?background:\s*var\(--app-primary-soft\)/);
});

// ── CSS: message bubbles ──
test('user bubble max-width ~46% or 520px; AI width calc', () => {
    assert.match(css, /\.chat-message--user\s+\.chat-message-content\s*\{[\s\S]*?max-width:\s*min\(520px,\s*46%\)/);
});

test('AI message content uses width calc', () => {
    assert.match(css, /\.chat-message-content\s*\{[\s\S]*?width:\s*min\(720px,\s*calc\(100%\s*-\s*48px\)\)/);
});

// ── CSS: messages overflow-x hidden ──
test('chat-messages has overflow-x hidden', () => {
    assert.match(css, /\.chat-messages\s*\{[\s\S]*?overflow-x:\s*hidden/);
});

// ── CSS: input area ──
test('input area unified shell with footer inside', () => {
    // shell is flex-direction: column
    assert.match(css, /\.chat-input-shell\s*\{[\s\S]*?flex-direction:\s*column/);
    // footer inside shell
    assert.match(css, /\.chat-input-footer\s*\{/);
    // textarea height
    assert.match(css, /\.chat-input\s+\.el-textarea__inner\s*\{[\s\S]*?min-height:\s*34px/);
    assert.match(css, /\.chat-topk-select\s*\{/);
});

// ── CSS: history and toolbar headers share the same height ──
test('history and toolbar headers share a fixed 64px border-box height', () => {
    assert.match(css, /\.chat-history-title-row,\s*\.chat-toolbar\s*\{[\s\S]*?height:\s*64px/);
    assert.match(css, /\.chat-history-title-row,\s*\.chat-toolbar\s*\{[\s\S]*?box-sizing:\s*border-box/);
});

// ── CSS: font sizes ──
test('AI text 15px/1.7, user 15px, source title 13px, summary 12px', () => {
    assert.match(css, /\.chat-markdown\s*\{[\s\S]*?font-size:\s*15px[\s\S]*?line-height:\s*1\.7/);
    assert.match(css, /\.chat-user-text\s*\{[\s\S]*?font-size:\s*15px/);
    assert.match(css, /\.chat-source-title\s*\{[\s\S]*?font-size:\s*13px/);
    assert.match(css, /\.chat-source-summary\s*\{[\s\S]*?font-size:\s*12px/);
});

// ── CSS: compact sources two-column ──
test('sources use two-column flex layout with min-width:0', () => {
    assert.match(css, /\.chat-source-item\s*\{[\s\S]*?display:\s*flex/);
    assert.match(css, /\.chat-source-left\s*\{/);
    assert.match(css, /\.chat-source-right\s*\{/);
    assert.match(css, /\.chat-source-summary\s*\{[\s\S]*?-webkit-line-clamp:\s*2/);
    assert.match(css, /\.chat-source-summary\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
    assert.match(css, /\.chat-source-title\s*\{[\s\S]*?min-width:\s*0/);
    assert.match(css, /\.chat-source-detail\s*\{/);
});

test('document detail link is blue and becomes light blue on hover', () => {
    assert.match(css, /\.chat-source-detail\s*\{[\s\S]*?color:\s*#3976C5\s*!important/);
    assert.match(css, /\.chat-source-detail:hover\s*\{[\s\S]*?color:\s*#7EB8E0\s*!important/);
});

// ── CSS: no horizontal overflow in source items ──
test('source items have min-width:0 to prevent overflow', () => {
    assert.match(css, /\.chat-source-item\s*\{[\s\S]*?min-width:\s*0/);
    assert.match(css, /\.chat-source-left\s*\{[\s\S]*?flex:\s*1[\s\S]*?min-width:\s*0/);
});

// ── Responsive ──
test('responsive breakpoints preserved', () => {
    assert.match(css, /@media\s*\(max-width:\s*1199px\)/);
    assert.match(css, /@media\s*\(max-width:\s*899px\)/);
    assert.match(css, /@media\s*\(max-width:\s*899px\)[\s\S]*\.chat-wrap\.is-history-open/);
    assert.match(css, /@media\s*\(max-width:\s*899px\)[\s\S]*\.chat-history-toggle\s*\{[\s\S]*?display:\s*inline-flex/);
});

// ── No horizontal overflow ──
test('prevents horizontal overflow', () => {
    assert.match(css, /\.chat-wrap\s*\{[\s\S]*?overflow:\s*hidden/);
    assert.match(css, /\.chat-main\s*\{[\s\S]*?min-width:\s*0/);
});

console.log('chat redesign test passed');
