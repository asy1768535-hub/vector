import assert from 'node:assert/strict';
import test from 'node:test';
import { marked } from './vendor/marked.esm.js';
import { renderAssistantMarkdown, highlightSourceWindow } from './src/chat_citations.js';

const DOMPurify = {
    sanitize(html) {
        return html
            .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
            .replace(/\s+on\w+\s*=\s*["'][^"']*["']/gi, '');
    },
};

const deps = { marked, DOMPurify };
const sources = [{ title: 'A' }, { title: 'B' }, { title: 'C' }];

test('single and consecutive citations become accessible badges', () => {
    const html = renderAssistantMarkdown('结论见[1][2]', sources, deps);

    assert.match(html, /class="chat-citation-badge"/);
    assert.match(html, /data-citation-index="0"/);
    assert.match(html, /data-citation-index="1"/);
    assert.match(html, /aria-label="查看引用 1"/);
});

test('citations in code links urls and out of range stay as text', () => {
    const html = renderAssistantMarkdown('`[1]` [link[2]](https://x.test) https://x.test/[3] [9]', sources, deps);

    assert.equal((html.match(/chat-citation-badge/g) || []).length, 0);
    assert.match(html, /\[1\]/);
    assert.match(html, /\[9\]/);
});

test('sanitizer removes script while keeping citation attributes', () => {
    const html = renderAssistantMarkdown('<script>alert(1)</script>资料[1]', sources, deps);

    assert.doesNotMatch(html, /<script/);
    assert.match(html, /data-citation-index="0"/);
});

test('model supplied citation badge html is stripped before generated badges are inserted', () => {
    let sanitizeOptions = null;
    const strictDOMPurify = {
        sanitize(html, options) {
            sanitizeOptions = options;
            return html
                .replace(/<button\b[^>]*>[\s\S]*?<\/button>/gi, '')
                .replace(/\s+(class|data-citation-index|aria-label|type)="[^"]*"/gi, '');
        },
    };

    const html = renderAssistantMarkdown(
        '<button class="chat-citation-badge" data-citation-index="0">1</button> 正文[1]',
        sources,
        { marked, DOMPurify: strictDOMPurify },
    );

    assert.ok(!sanitizeOptions.ALLOWED_TAGS.includes('button'));
    assert.ok(!sanitizeOptions.ALLOWED_ATTR.includes('class'));
    assert.ok(!sanitizeOptions.ALLOWED_ATTR.includes('data-citation-index'));
    assert.equal((html.match(/chat-citation-badge/g) || []).length, 1);
    assert.match(html, /正文/);
});

test('highlightSourceWindow splits absolute offsets into local spans', () => {
    assert.deepEqual(
        highlightSourceWindow({ text_window: 'abcdef', window_start: 10, source_start: 12, source_end: 14 }),
        { before: 'ab', match: 'cd', after: 'ef' },
    );
});
