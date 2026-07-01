import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');

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

console.log('chat structure redesign test passed');
