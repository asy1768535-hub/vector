import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const loginJs = readFileSync(new URL('./src/views/Login.js', import.meta.url), 'utf-8');
const styleCss = readFileSync(new URL('./style.css', import.meta.url), 'utf-8');

assert.ok(loginJs.includes('login-shell'), 'expected unified login shell markup');
assert.ok(loginJs.includes('company-logo.png'), 'expected local company logo asset reference');
assert.ok(loginJs.includes('login-hero'), 'expected left-side hero section');
assert.ok(loginJs.includes('login-corner-brand'), 'expected corner logo markup');
assert.ok(styleCss.includes('.login-shell'), 'expected login shell styles');
assert.ok(styleCss.includes('.login-bg-layer'), 'expected unified background layer styles');
assert.ok(styleCss.includes('.login-panel'), 'expected floating login card styles');
assert.ok(styleCss.includes('.login-corner-brand'), 'expected corner logo styles');
assert.ok(styleCss.includes('box-sizing: border-box'), 'expected viewport-height layout to use border-box sizing');
assert.ok(styleCss.includes('100svh'), 'expected login layout to size to the dynamic viewport height');

console.log('login redesign test passed');
