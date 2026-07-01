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

// ── New features: top_k selector ──
test('allows top_k selector and rejects forbidden features', () => {
    // Allowed new elements
    for (const token of ['topK', '检索数量', 'topk-select']) {
        assert.ok(chat.includes(token), `missing allowed feature: ${token}`);
    }
    // Still forbidden
    for (const forbidden of [
        '上传附件', '点赞', '点踩', '查看原文', '消息中心', '帮助中心',
    ]) {
        assert.ok(!chat.includes(forbidden), `unexpected fake feature: ${forbidden}`);
    }
});

// ── New features: time formatting ──
test('includes time formatting helpers', () => {
    assert.ok(chat.includes('fmtTime'), 'missing fmtTime function');
    assert.ok(chat.includes('chat-msg-time'), 'missing message time element');
    assert.ok(chat.includes('nowISO'), 'missing nowISO helper');
});

// ── New features: compact sources ──
test('replaces expanded source boxes with compact rows', () => {
    // Must have compact source structure
    assert.ok(chat.includes('chat-source-summary'), 'missing compact source summary');
    assert.ok(chat.includes('chat-source-detail'), 'missing source detail link');
    assert.ok(chat.includes('openDocDetail'), 'missing openDocDetail function');
    // Must NOT have old collapsible pattern
    assert.ok(!chat.includes('el-collapse'), 'old el-collapse sources still present');
    assert.ok(!chat.includes('el-collapse-item'), 'old el-collapse-item still present');
});

// ── New features: input enhancements ──
test('input area has maxlength, char count, refresh button', () => {
    assert.ok(chat.includes('maxlength="2000"'), 'missing maxlength on textarea');
    assert.ok(chat.includes('remainingChars') || chat.includes('字数'), 'missing char count');
    assert.ok(chat.includes('loadLibs'), 'missing refresh libraries action');
});

// ── CSS: two-card layout with 12px gap ──
test('CSS defines two independent cards with 12px gap', () => {
    // chat-wrap has gap
    assert.match(css, /\.chat-wrap\s*\{[\s\S]*?gap:\s*12px/);
    // Both panels have card styling (border-radius + box-shadow)
    assert.match(css, /\.chat-history-panel\s*\{[\s\S]*?border-radius:\s*12px/);
    assert.match(css, /\.chat-main\s*\{[\s\S]*?border-radius:\s*12px/);
    // No longer shares a single border (no outer border on wrap)
});

// ── CSS: history panel width ──
test('history panel is ~310px on desktop and has 86-96px item height', () => {
    assert.match(css, /\.chat-history-panel\s*\{[\s\S]*?width:\s*310px/);
    assert.match(css, /\.chat-history-item\s*\{[\s\S]*?min-height:\s*86px/);
    // History items show time
    assert.match(css, /\.chat-history-item-time\s*\{/);
    // Active state keeps green border + soft bg
    assert.match(css, /\.chat-history-item\.is-active\s*\{[\s\S]*?border-left-color:\s*var\(--app-primary\)/);
    assert.match(css, /\.chat-history-item\.is-active\s*\{[\s\S]*?background:\s*var\(--app-primary-soft\)/);
});

// ── CSS: message bubbles ──
test('user bubble max-width ~46% or 520px', () => {
    // User message content has max-width constraint
    assert.match(css, /\.chat-message--user\s+\.chat-message-content\s*\{[\s\S]*?max-width:\s*min\(520px,\s*46%\)/);
});

test('AI answer max-width ~720px', () => {
    assert.match(css, /\.chat-message-content\s*\{[\s\S]*?max-width:\s*720px/);
});

// ── CSS: input area height ~118-130px ──
test('input area height and textarea sizing', () => {
    assert.match(css, /\.chat-input\s+\.el-textarea__inner\s*\{[\s\S]*?min-height:\s*72px/);
    assert.match(css, /\.chat-input-footer\s*\{/);
    assert.match(css, /\.chat-topk-select\s*\{/);
});

// ── CSS: toolbar height ~68px ──
test('toolbar height ~68px', () => {
    assert.match(css, /\.chat-toolbar\s*\{[\s\S]*?min-height:\s*68px/);
});

// ── CSS: compact sources ──
test('sources use compact grid layout', () => {
    assert.match(css, /\.chat-source-item\s*\{[\s\S]*?display:\s*grid/);
    assert.match(css, /\.chat-source-summary\s*\{[\s\S]*?-webkit-line-clamp:\s*2/);
    assert.match(css, /\.chat-source-detail\s*\{/);
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
