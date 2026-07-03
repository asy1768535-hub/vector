const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);

export function isLocalPreviewUrl(value) {
    try {
        const url = new URL(value);
        return LOOPBACK_HOSTS.has(url.hostname)
            && url.port === '5599'
            && url.searchParams.get('preview') === '1';
    } catch (_) {
        return false;
    }
}

if (typeof window !== 'undefined') {
    window.__DEV_PREVIEW__ = isLocalPreviewUrl(window.location.href);
}
