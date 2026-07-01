import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const index = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const icons = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');

assert.doesNotMatch(app, /theme\.js/);
assert.doesNotMatch(index, /element-plus\.dark-css-vars\.css/);
assert.doesNotMatch(layout, /\bTHEMES\b|\bcurrentTheme\b|\bapplyTheme\b|onThemeCommand|themeOptions/);
assert.doesNotMatch(layout, /mdi:palette-outline|theme-switcher-item/);
assert.match(css, /:root\s*\{/);
assert.doesNotMatch(css, /\[data-theme=/);
assert.doesNotMatch(css, /ai-dark|government|theme-switcher/);
assert.doesNotMatch(icons, /mdi:palette-outline|mdi:check|mdi:weather-night|mdi:white-balance-sunny/);

console.log('single theme cleanup test passed');
