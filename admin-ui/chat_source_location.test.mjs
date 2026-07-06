import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

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
