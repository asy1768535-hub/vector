// In-memory request cache with dedup, TTL, token-based race protection.
const _store = new Map();   // key → { data, expiresAt, _promise, _force, _token }
let _generation = 0;
let _nextToken = 0;

/**
 * Execute fn(key) with caching:
 * - Cache hit returns a deep clone.
 * - Concurrent calls with same key share one in-flight promise.
 * - Each caller of a shared promise gets an independent clone.
 * - forceRefresh replaces old in-flight; old results cannot overwrite new cache.
 * - Non-2xx / exceptions are NOT cached.
 */
export async function cachedRequest(key, fn, ttlMs, forceRefresh = false) {
    const token = ++_nextToken;
    const gen = _generation;

    // Cache hit (non-force)
    if (!forceRefresh) {
        const entry = _store.get(key);
        if (entry) {
            if (entry._promise) return entry._promise.then(_clone);
            if (Date.now() <= entry.expiresAt) return _clone(entry.data);
        }
    }

    // forceRefresh: always start a new request (replaces old in-flight)

    const isForce = forceRefresh;

    const promise = (async () => {
        try {
            const data = await fn();
            const cur = _store.get(key);
            // Write only if: generation unchanged AND our token is still the entry's token
            if (_generation === gen && cur && cur._token === token) {
                _store.set(key, { data, expiresAt: Date.now() + ttlMs, _promise: null, _force: false, _token: 0 });
            }
            return _clone(data);
        } catch (e) {
            const cur = _store.get(key);
            // Delete only if our token is still current (don't delete newer entries)
            if (cur && cur._token === token && _generation === gen) {
                _store.delete(key);
            }
            throw e;
        }
    })();

    _store.set(key, { _promise: promise, expiresAt: 0, _force: isForce, _token: token });
    return promise.then(_clone);
}

export function invalidateByPrefix(prefix) {
    for (const key of _store.keys()) {
        if (key.startsWith(prefix)) _store.delete(key);
    }
    _generation++;
}

export function clearCache() {
    _store.clear();
    _generation++;
}

function _clone(data) {
    if (data === null || data === undefined) return data;
    if (Array.isArray(data)) return data.map(_clone);
    if (typeof data === 'object') {
        const out = {};
        for (const k of Object.keys(data)) out[k] = _clone(data[k]);
        return out;
    }
    return data;
}
