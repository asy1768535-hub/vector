import assert from 'node:assert/strict';
import test from 'node:test';
import { paginate, formatTime } from './src/common_ui.js';

test('paginate clamps invalid page and size', () => {
    const rows = Array.from({ length: 25 }, (_, i) => i + 1);

    assert.deepEqual(paginate(rows, 0, 0), {
        items: rows.slice(0, 10),
        total: 25,
        page: 1,
        pageCount: 3,
    });

    assert.deepEqual(paginate(rows, 9, 20), {
        items: rows.slice(20, 25),
        total: 25,
        page: 2,
        pageCount: 2,
    });
});

test('formatTime returns zh-CN 24h string or dash', () => {
    assert.ok(formatTime('2026-06-15T08:30:00Z').includes('2026'));
    assert.equal(formatTime(null), '—');
    assert.equal(formatTime('not-a-date'), '—');
});
