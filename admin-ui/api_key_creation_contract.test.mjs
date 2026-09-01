import assert from 'node:assert/strict';
import test from 'node:test';

import { createApiKey } from './src/api.js';

test('createApiKey sends the selected organization and expiration', async () => {
    const originalFetch = globalThis.fetch;
    let request;
    globalThis.fetch = async (url, options) => {
        request = { url, options };
        return new Response(JSON.stringify({ plaintext_key: 'shown-once' }), {
            status: 201,
            headers: { 'Content-Type': 'application/json' },
        });
    };

    try {
        await createApiKey(
            'agent integration',
            '11111111-1111-4111-8111-111111111111',
            '2026-09-01T00:00:00.000Z',
        );
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.equal(request.url, '/me/api-keys');
    assert.equal(request.options.method, 'POST');
    assert.deepEqual(JSON.parse(request.options.body), {
        name: 'agent integration',
        organization_id: '11111111-1111-4111-8111-111111111111',
        expires_at: '2026-09-01T00:00:00.000Z',
    });
});
