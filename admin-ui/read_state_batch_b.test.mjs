import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { readProjection } from './src/read_state_ui.js';

const searchSource = readFileSync(new URL('./src/views/Search.js', import.meta.url), 'utf8');
const librariesSource = readFileSync(new URL('./src/views/Libraries.js', import.meta.url), 'utf8');
const apiKeysSource = readFileSync(new URL('./src/views/ApiKeys.js', import.meta.url), 'utf8');

test('Batch B uses the shared read projection and latest-request fence', () => {
    for (const [name, source] of [
        ['Search', searchSource],
        ['Libraries', librariesSource],
        ['ApiKeys', apiKeysSource],
    ]) {
        assert.match(source, /readProjection\(/, `${name} projects explicit read states`);
        assert.match(source, /createRequestFence\(\)/, `${name} fences stale responses`);
        assert.match(source, /\.isCurrent\(/, `${name} checks request ownership`);
    }
});

test('failed reads remain different from successful empty responses', () => {
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: false,
        empty: true,
        error: '请求失败',
    }), 'fatal');
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: true,
        empty: true,
    }), 'empty');
});

test('Search exposes durable search, library, and FAQ errors with real retry paths', () => {
    assert.match(searchSource, /searchReadState === 'fatal'/);
    assert.match(searchSource, /searchReadState === 'refresh-error'/);
    assert.match(searchSource, /libsError/);
    assert.match(searchSource, /faqError/);
    assert.match(searchSource, /@click="handleSearch">重试搜索/);
    assert.match(searchSource, /@click="loadLibs\(true\)"/);
});

test('Libraries distinguishes list and FAQ read failures without changing mutations', () => {
    assert.match(librariesSource, /librariesReadState === 'fatal'/);
    assert.match(librariesSource, /librariesReadState === 'refresh-error'/);
    assert.match(librariesSource, /faqMgr\.error/);
    assert.match(librariesSource, /@click="loadFaq">重试/);
    for (const call of [
        'api.createLibrary', 'api.updateLibrary', 'api.deleteLibrary',
        'api.rebuildLibraryCollection', 'api.createLibraryFaq',
        'api.updateLibraryFaq', 'api.deleteLibraryFaq',
    ]) {
        assert.ok(librariesSource.includes(call), `preserves ${call}`);
    }
});

test('ApiKeys keeps list errors separate from empty keys and preserves mutations', () => {
    assert.match(apiKeysSource, /keysReadState === 'fatal'/);
    assert.match(apiKeysSource, /keysReadState === 'refresh-error'/);
    assert.match(apiKeysSource, /@click="load\(true\)">重试/);
    assert.ok(apiKeysSource.includes('api.createApiKey'), 'preserves createApiKey');
    assert.ok(apiKeysSource.includes('api.revokeApiKey'), 'preserves revokeApiKey');
});
