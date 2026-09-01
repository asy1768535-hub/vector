import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const libraries = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('chat restores and consumes graph augmentation metadata', () => {
    assert.match(chat, /graph_augmented: m\.graph_augmented === true/);
    assert.match(chat, /aiMsg\.graph_augmented = o\.graph_augmented === true/);
    assert.match(chat, /aiMsg\.graph_evidence = o\.graph_evidence \|\| \[\]/);
    assert.match(chat, /renderMarkdown\(m\.text, citationSources\(m\)\)/);
});

test('graph evidence appears before ordinary sources and reuses source detail', () => {
    const graphIndex = chat.indexOf('class="chat-graph-evidence"');
    const sourcesIndex = chat.indexOf('<!-- Collapsible sources -->');
    assert.ok(graphIndex > 0 && graphIndex < sourcesIndex);
    assert.match(chat, /知识图谱增强/);
    assert.match(chat, /关系证据（\{\{ m\.graph_evidence\.length \}\}）/);
    assert.match(chat, /@click="openDocDetail\(evidence\)"/);
    assert.match(css, /\.chat-graph-evidence\{/);
});

test('library edit exposes off shadow and enabled rollout modes', () => {
    assert.match(libraries, /graph_assisted_chat_mode: row\.graph_assisted_chat_mode \|\| 'off'/);
    assert.match(libraries, /v-model="edit\.form\.graph_assisted_chat_mode"/);
    assert.match(libraries, /value="shadow" label="影子评估"/);
    assert.match(libraries, /value="enabled" label="增强回答"/);
});
