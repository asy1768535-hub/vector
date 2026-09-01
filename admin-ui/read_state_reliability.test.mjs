import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { readProjection, createRequestFence } from './src/read_state_ui.js';

const usersSource = readFileSync(new URL('./src/views/Users.js', import.meta.url), 'utf8');
const dashboardSource = readFileSync(new URL('./src/views/Dashboard.js', import.meta.url), 'utf8');

test('readProjection distinguishes initial loading, successful empty, and fatal failure', () => {
    assert.equal(readProjection({
        started: true,
        loading: true,
        hasResolved: false,
    }), 'loading');
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: true,
        empty: true,
    }), 'empty');
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: false,
        error: '请求失败',
    }), 'fatal');
});

test('readProjection preserves resolved data while refreshing or after refresh failure', () => {
    assert.equal(readProjection({
        started: true,
        loading: true,
        hasResolved: true,
        empty: false,
    }), 'refreshing');
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: true,
        empty: false,
        error: '刷新失败',
    }), 'refresh-error');
});

test('readProjection keeps successful non-empty data in the ready state', () => {
    assert.equal(readProjection({
        started: true,
        loading: false,
        hasResolved: true,
        empty: false,
    }), 'ready');
});

test('readProjection does not treat an unresolved idle request as successful empty data', () => {
    assert.equal(readProjection({
        started: false,
        loading: false,
        hasResolved: false,
        empty: true,
    }), 'idle');
});

test('createRequestFence rejects stale responses after a newer request starts', () => {
    const fence = createRequestFence();
    const first = fence.begin();
    const second = fence.begin();

    assert.equal(fence.isCurrent(first), false);
    assert.equal(fence.isCurrent(second), true);
});

test('pilot pages use the shared projection and request fence', () => {
    for (const source of [usersSource, dashboardSource]) {
        assert.match(source, /readProjection\(/);
        assert.match(source, /createRequestFence\(\)/);
    }
});

test('pilot retry controls call real read functions and disable duplicate retry', () => {
    assert.match(usersSource, /:loading="loading" @click="loadUsers\(true\)">重试/);
    assert.match(dashboardSource, /loading\.value \|\| healthLoading\.value/);
    assert.match(dashboardSource, /return load\(true, failedReads\.value\)/);
});
