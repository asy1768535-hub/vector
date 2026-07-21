import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const asset = new URL('./assets/app-brand-mark.png', import.meta.url);

assert.ok(existsSync(asset), 'expected app brand mark');
assert.match(layout, /app-brand-mark\.png/);
assert.match(layout, /class="app-brand-mark"/);
assert.match(layout, /class="header-breadcrumb"/);
assert.match(layout, /layout-main--chat/);
assert.match(layout, /currentPath === '\/chat'/);
assert.match(css, /\.app-brand-mark\s*\{/);
assert.match(css, /\.header-breadcrumb\s*\{/);
assert.match(css, /\.layout-main--chat\s*\{/);
assert.match(css, /\.layout-header\s*\{[\s\S]*height:\s*64px/);

console.log('global layout redesign test passed');
