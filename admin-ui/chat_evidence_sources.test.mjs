import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { marked } from './vendor/marked.esm.js';
import { renderAssistantMarkdown } from './src/chat_citations.js';

const chat = readFileSync(new URL('./src/views/Chat.js', import.meta.url), 'utf8');
function ordered(message) {
    const start = chat.indexOf('function citationSources(message)');
    const end = chat.indexOf('function citationGraphError', start);
    return Function(`${chat.slice(start, end)}; return citationSources;`)()(message);
}

test('mixed ordinary and graph sources keep their actual citation identity', () => {
    const first = { citation_index: 1, chunk_id: 'first' };
    const third = { citation_index: 3, chunk_id: 'third' };
    const graph = { citation_index: 2, chunk_id: 'graph' };
    assert.deepEqual(ordered({ sources: [first, third], graph_evidence: [graph] }), [first, graph, third]);
});

test('legacy history ordinary sources fill the gaps around saved graph indexes', () => {
    const first = { chunk_id: 'first' };
    const third = { chunk_id: 'third' };
    const graph = { citation_index: 2, chunk_id: 'graph' };
    assert.deepEqual(ordered({ sources: [first, third], graph_evidence: [graph] }), [first, graph, third]);
});

test('missing source indexes never create a clickable badge', () => {
    const html = renderAssistantMarkdown('依据[1][2][3]', [{ title: 'first' }, undefined, { title: 'third' }],
        { marked, DOMPurify: { sanitize: value => value } });
    assert.match(html, /data-citation-index="0"/);
    assert.match(html, /data-citation-index="2"/);
    assert.doesNotMatch(html, /data-citation-index="1"/);
});

test('sources and recalled excerpts show verified server locations and citation indexes', () => {
    assert.match(chat, /chat-source-num[^\n]*s\.citation_index/);
    assert.match(chat, /s\.location_label/);
    assert.match(chat, /recalledChunkDialog\.source\.location_label/);
});
