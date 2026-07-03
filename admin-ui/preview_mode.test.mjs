import assert from 'node:assert/strict';

const preview = await import('./src/preview_mode.js').catch(() => ({}));

assert.equal(typeof preview.isLocalPreviewUrl, 'function');
assert.equal(preview.isLocalPreviewUrl('http://127.0.0.1:5599/?preview=1#/login'), true);
assert.equal(preview.isLocalPreviewUrl('http://localhost:5599/?preview=1#/login'), true);
assert.equal(preview.isLocalPreviewUrl('http://127.0.0.1:5599/#/login'), false);
assert.equal(preview.isLocalPreviewUrl('http://127.0.0.1:8100/?preview=1#/login'), false);
assert.equal(preview.isLocalPreviewUrl('https://example.com:5599/?preview=1#/login'), false);

console.log('5 preview mode tests passed');
