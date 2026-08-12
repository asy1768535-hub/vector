import assert from 'node:assert/strict';
import test from 'node:test';
import { cachedRequest, invalidateByPrefix, clearCache } from './src/request_cache.js';

// ── Helpers ──
let callCount;
function mockFn(result, delay = 0) {
    return async () => {
        callCount++;
        if (delay) await new Promise((r) => setTimeout(r, delay));
        if (result instanceof Error) throw result;
        return result;
    };
}

test.beforeEach(() => { clearCache(); callCount = 0; });

// ════════════════════════════════════════════════════════════
test('cache hit returns data without re-calling fn', async () => {
    const fn = mockFn({ x: 1 });
    const r1 = await cachedRequest('k1', fn, 5000);
    assert.deepEqual(r1, { x: 1 });
    assert.equal(callCount, 1);

    const r2 = await cachedRequest('k1', fn, 5000);
    assert.deepEqual(r2, { x: 1 });
    assert.equal(callCount, 1, 'second call should hit cache');
});

test('cache hit returns a shallow copy, not original object', async () => {
    const data = { arr: [1, 2, 3] };
    const r1 = await cachedRequest('k2', mockFn(data), 5000);
    r1.arr.push(4); // mutate the returned copy
    const r2 = await cachedRequest('k2', mockFn(data), 5000);
    assert.deepEqual(r2.arr, [1, 2, 3], 'cache not mutated');
});

test('TTL expiry re-fetches', async () => {
    const fn = mockFn({ v: 1 });
    await cachedRequest('k3', fn, 10); // 10ms TTL
    await new Promise((r) => setTimeout(r, 15));
    await cachedRequest('k3', fn, 10);
    assert.equal(callCount, 2, 'refetched after TTL');
});

test('forceRefresh skips cache', async () => {
    const fn = mockFn({ v: 1 });
    await cachedRequest('k4', fn, 5000);
    await cachedRequest('k4', fn, 5000, true);
    assert.equal(callCount, 2, 'forceRefresh re-fetches');
});

test('concurrent calls dedup: only one request sent', async () => {
    const fn = mockFn({ v: 1 }, 20); // slow
    const [r1, r2, r3] = await Promise.all([
        cachedRequest('k5', fn, 5000),
        cachedRequest('k5', fn, 5000),
        cachedRequest('k5', fn, 5000),
    ]);
    assert.equal(callCount, 1, 'only one call for concurrent requests');
    assert.deepEqual(r1, { v: 1 });
    assert.deepEqual(r2, { v: 1 });
    assert.deepEqual(r3, { v: 1 });
});

test('failed request is NOT cached', async () => {
    const fn = mockFn(new Error('boom'));
    await assert.rejects(() => cachedRequest('k6', fn, 5000));
    assert.equal(callCount, 1);

    const fn2 = mockFn({ ok: true });
    const r = await cachedRequest('k6', fn2, 5000);
    assert.deepEqual(r, { ok: true });
    assert.equal(callCount, 2, 'refetched after failure');
});

test('invalidateByPrefix removes matching keys', async () => {
    await cachedRequest('users:1', mockFn({}), 5000);
    await cachedRequest('users:2', mockFn({}), 5000);
    await cachedRequest('libs:1', mockFn({}), 5000);
    assert.equal(callCount, 3);
    invalidateByPrefix('users:');
    await cachedRequest('users:1', mockFn({}), 5000);
    await cachedRequest('users:2', mockFn({}), 5000);
    assert.equal(callCount, 5, 'users refetched');
    await cachedRequest('libs:1', mockFn({}), 5000);
    assert.equal(callCount, 5, 'libs still cached');
});

test('clearCache removes all', async () => {
    await cachedRequest('x', mockFn({}), 5000);
    await cachedRequest('y', mockFn({}), 5000);
    clearCache();
    await cachedRequest('x', mockFn({}), 5000);
    await cachedRequest('y', mockFn({}), 5000);
    assert.equal(callCount, 4);
});

test('stale in-flight does not write after clearCache', async () => {
    const fn = mockFn({ fresh: true }, 20); // slow
    const p = cachedRequest('stale', fn, 5000);
    clearCache(); // generation bumped
    await p;
    // The slow promise should NOT have written because gen changed
    const fn2 = mockFn({ next: true });
    const r = await cachedRequest('stale', fn2, 5000);
    assert.deepEqual(r, { next: true });
    assert.equal(callCount, 2, 'stale write discarded, re-fetched');
});

test('concurrent callers get independent clones from shared in-flight', async () => {
    const fn = mockFn({ arr: [1, 2] }, 10);
    const [r1, r2] = await Promise.all([
        cachedRequest('shared', fn, 5000),
        cachedRequest('shared', fn, 5000),
    ]);
    assert.equal(callCount, 1);
    r1.arr.push(3);
    assert.deepEqual(r2.arr, [1, 2], 'r2 not mutated by r1');
});

test('concurrent forceRefresh replaces old in-flight, only latest writes', async () => {
    // Each forceRefresh replaces prior in-flight; only the last's result persists
    const fn1 = mockFn({ first: true });
    const fn2 = mockFn({ second: true });
    // Start first forceRefresh
    const p1 = cachedRequest('fr1', fn1, 5000, true);
    // Second forceRefresh replaces it immediately
    const r2 = await cachedRequest('fr1', fn2, 5000, true);
    assert.deepEqual(r2, { second: true });
    // First one completes but should not overwrite
    await p1.catch(() => {});
    const fn3 = mockFn({});
    const final = await cachedRequest('fr1', fn3, 5000);
    assert.deepEqual(final, { second: true }, 'latest forceRefresh wins');
});

test('forceRefresh result wins over late-arriving old request', async () => {
    // Old slow request started first (takes 30ms)
    const slowFn = mockFn({ old: true }, 30);
    // Don't await — fire and let it run in background
    const oldP = cachedRequest('race', slowFn, 5000);
    // Wait a tick so old request is in-flight
    await new Promise((r) => setTimeout(r, 5));
    // forceRefresh starts after, finishes fast
    const freshFn = mockFn({ fresh: true }, 5);
    const fresh = await cachedRequest('race', freshFn, 5000, true);
    assert.deepEqual(fresh, { fresh: true });
    // Now let old finish
    await oldP.catch(() => {});
    // Final cache must be fresh, not old
    const finalFn = mockFn({ check: true });
    const final = await cachedRequest('race', finalFn, 5000);
    assert.deepEqual(final, { fresh: true }, 'cache is fresh, old did not overwrite');
});

test('failed old request does not delete forceRefresh cache', async () => {
    // Cache a value first
    await cachedRequest('race2', mockFn({ initial: 1 }), 5000);
    // Start a slow forceRefresh that will fail
    const failFn = mockFn(new Error('boom'), 20);
    const failP = cachedRequest('race2', failFn, 5000, true);
    const failCheck = assert.rejects(failP, /boom/);
    // Meanwhile, another forceRefresh succeeds
    await new Promise((r) => setTimeout(r, 5));
    const okFn = mockFn({ ok: true }, 5);
    await cachedRequest('race2', okFn, 5000, true);
    // Let the failed one finish
    await failCheck;
    // Cache must still be ok
    const r = await cachedRequest('race2', mockFn({}), 5000);
    assert.deepEqual(r, { ok: true }, 'cache preserved after failed race');
});

console.log('request_cache test passed');
