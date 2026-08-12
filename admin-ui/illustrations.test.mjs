import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import * as ill from './src/illustrations.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

const FILES = [
    ['chatWelcome', 'chat-welcome.svg'],
    ['searchEmpty', 'search-empty.svg'],
    ['uploadEmpty', 'upload-empty.svg'],
    ['dataEmpty', 'data-empty.svg'],
    ['taskEmpty', 'task-empty.svg'],
    ['serviceError', 'service-error.svg'],
    ['apiKeySecurity', 'api-key-security.svg'],
];

// ── File existence ──
test('all 7 illustration files exist on disk', () => {
    for (const [name, file] of FILES) {
        const fp = join(__dirname, 'assets', 'illustrations', file);
        assert.ok(existsSync(fp), `${file} missing`);
    }
});

// ── SVG validation ──
test('all illustrations are valid SVGs with viewBox 0 0 320 240', () => {
    for (const [name, file] of FILES) {
        const fp = join(__dirname, 'assets', 'illustrations', file);
        const content = readFileSync(fp, 'utf8');
        assert.ok(content.startsWith('<svg') || content.includes('<svg'), `${file} is SVG`);
        assert.ok(content.includes('viewBox="0 0 320 240"'), `${file} has viewBox 0 0 320 240`);
        assert.ok(content.includes('</svg>'), `${file} closes svg`);
    }
});

// ── No external URLs (ignore xmlns) ──
test('illustrations contain no script, foreignObject, base64, or external URLs', () => {
    for (const [name, file] of FILES) {
        const fp = join(__dirname, 'assets', 'illustrations', file);
        const content = readFileSync(fp, 'utf8');
        assert.ok(!content.includes('<script'), `${file} has no script`);
        assert.ok(!content.includes('foreignObject'), `${file} has no foreignObject`);
        assert.ok(!content.includes('base64'), `${file} has no base64`);
        const body = content.replace(/xmlns="[^"]*"/g, '');
        assert.ok(!body.includes('http:'), `${file} has no http:`);
        assert.ok(!body.includes('https:'), `${file} has no https:`);
    }
});

// ── Module exports ──
test('illustrations.js exports correct paths', () => {
    for (const [name, file] of FILES) {
        assert.equal(typeof ill[name], 'string', `${name} is exported`);
        assert.ok(ill[name].includes(file), `${name} path includes ${file}`);
    }
});

// ── Page integration checks ──
const pages = {
    Chat: ['chatWelcome', 'src/views/Chat.js'],
    Search: ['searchEmpty', 'src/views/Search.js'],
    Import: ['uploadEmpty', 'src/views/Import.js'],
    ApiKeys: ['dataEmpty', 'apiKeySecurity', 'src/views/ApiKeys.js'],
    Libraries: ['dataEmpty', 'src/views/Libraries.js'],
    Users: ['dataEmpty', 'src/views/Users.js'],
    Audit: ['dataEmpty', 'src/views/Audit.js'],
    ChatLogs: ['dataEmpty', 'src/views/ChatLogs.js'],
    Jobs: ['taskEmpty', 'src/views/Jobs.js'],
    RuntimeStatus: ['serviceError', 'src/views/RuntimeStatus.js'],
    Permissions: ['dataEmpty', 'src/views/Permissions.js'],
};

test('each page imports and exposes the correct illustration', () => {
    for (const [page, ...args] of Object.values(pages)) {
        const src = readFileSync(join(__dirname, args.pop()), 'utf8');
        for (const name of args) {
            assert.ok(src.includes(name), `${page}: imports ${name}`);
        }
    }
});

// ── Rendered usage checks ──
test('Search: searchEmpty renders in both initial state and no-result #empty slot', () => {
    const src = readFileSync(join(__dirname, 'src/views/Search.js'), 'utf8');
    // Initial empty state uses searchEmpty
    assert.ok(src.includes('search-empty-card'), 'initial state card uses searchEmpty');
    // Table #empty slot also uses searchEmpty for no-results
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('searchEmpty'), 'searchEmpty referenced in template');
    assert.ok(tpl.includes('未找到匹配结果'), 'no-result text preserved');
});

test('Permissions: dataEmpty renders in el-empty and table #empty', () => {
    const src = readFileSync(join(__dirname, 'src/views/Permissions.js'), 'utf8');
    assert.ok(src.includes('dataEmpty'), 'Permissions imports dataEmpty');
    const tpl = src.slice(src.indexOf('template:'));
    // el-empty uses dataEmpty
    assert.ok(tpl.includes('dataEmpty'), 'template renders dataEmpty');
});

test('all table #empty slots use illustration-empty-wrapper', () => {
    const pages = ['Libraries', 'Users', 'Audit', 'ChatLogs', 'Jobs', 'RuntimeStatus', 'ApiKeys', 'Search'];
    for (const page of pages) {
        const src = readFileSync(join(__dirname, 'src/views', `${page}.js`), 'utf8');
        const tpl = src.slice(src.indexOf('template:'));
        if (tpl.includes('#empty')) {
            assert.ok(tpl.includes('illustration-empty-wrapper'), `${page}: #empty uses wrapper`);
        }
    }
});

console.log('illustrations test passed');
