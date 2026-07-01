import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

assert.match(chat, /chat-history-title-row/);
assert.match(chat, /chat-new-button/);
assert.match(chat, /chat-toolbar/);
assert.match(chat, /chat-toolbar-label/);
assert.match(chat, /chat-message-content/);
assert.match(chat, /chat-avatar--ai/);
assert.match(chat, /chat-avatar--user/);
assert.match(chat, /chat-input-shell/);
assert.match(chat, /copyAnswer/);
assert.match(chat, /navigator\.clipboard/);
assert.match(chat, /mobileHistoryOpen/);
assert.match(chat, /chat-history-toggle/);

for (const required of [
    'newChat', 'selectConversation', 'archiveConv', 'deleteConv', 'send',
    'streamChatMessage', 'renderMarkdown', 'chat-sources',
]) {
    assert.ok(chat.includes(required), `expected existing behavior: ${required}`);
}

for (const forbidden of [
    '上传附件', '检索设置', '点赞', '点踩', '查看原文', '消息中心', '帮助中心',
]) {
    assert.ok(!chat.includes(forbidden), `unexpected fake feature: ${forbidden}`);
}

assert.match(css, /\.chat-history-panel\s*\{[\s\S]*width:\s*290px/);
assert.match(css, /\.chat-toolbar\s*\{/);
assert.match(css, /\.chat-message-content\s*\{/);
assert.match(css, /\.chat-avatar--ai\s*\{/);
assert.match(css, /\.chat-input-shell\s*\{/);
assert.match(css, /\.chat-copy-answer\s*\{/);
assert.match(css, /\.chat-history-toggle\s*\{/);
assert.match(css, /@media\s*\(max-width:\s*899px\)/);
assert.match(css, /@media\s*\(max-width:\s*899px\)[\s\S]*\.chat-wrap\.is-history-open/);

console.log('chat structure redesign test passed');
