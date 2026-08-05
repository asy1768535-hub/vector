import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const root = new URL('./', import.meta.url);
const read = (path) => readFileSync(new URL(path, root), 'utf8');

test('refresh buttons use one local icon and rotate it while loading', () => {
    const svg = read('assets/icons/refresh-icon.svg');
    const css = read('style.css');
    assert.match(svg, /M20 6v5h-5/);
    assert.match(svg, /<circle\s+cx="12"\s+cy="12"\s+r="8"/);
    assert.match(css, /mask:\s*url\('\.\/assets\/icons\/refresh-icon\.svg\?v=2'\)/);
    assert.match(css, /transform-origin:\s*center/);
    assert.match(css, /\.app-refresh-button\.is-loading > \.el-icon\.is-loading\s*\{\s*display:\s*none/);
    assert.match(css, /\.app-refresh-button\.is-loading \.app-refresh-icon[\s\S]*?animation:\s*app-refresh-spin/);

    for (const path of [
        'src/views/Documents.js',
        'src/views/ApiKeys.js',
        'src/views/ChatLogs.js',
        'src/views/Audit.js',
        'src/views/Dashboard.js',
        'src/views/Chat.js',
        'src/views/Users.js',
        'src/views/GraphGovernance.js',
        'src/views/Jobs.js',
        'src/views/RuntimeStatus.js',
        'src/views/KnowledgeCatalog.js',
        'src/views/SchemaLifecycle.js',
        'src/views/Libraries.js',
    ]) {
        const source = read(path);
        assert.match(source, /app-refresh-button/, `${path} missing refresh button class`);
        assert.match(source, /app-refresh-icon/, `${path} missing refresh icon`);
    }
});
