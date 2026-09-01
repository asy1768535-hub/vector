export function readProjection({
    started = false,
    loading = false,
    hasResolved = false,
    empty = false,
    error = '',
} = {}) {
    if (!started) return 'idle';
    if (!hasResolved) {
        if (error) return 'fatal';
        return loading ? 'loading' : 'idle';
    }
    if (loading) return 'refreshing';
    if (error) return 'refresh-error';
    return empty ? 'empty' : 'ready';
}

export function createRequestFence() {
    let latest = 0;
    return {
        begin() {
            latest += 1;
            return latest;
        },
        isCurrent(token) {
            return token === latest;
        },
    };
}
