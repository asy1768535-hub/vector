import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { iconSvg } from './src/icons.js';

const iconsSource = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');

const SIDEBAR_NAMES = [
    'sidebar:chat', 'sidebar:document', 'sidebar:search', 'sidebar:import',
    'sidebar:api-key', 'sidebar:overview', 'sidebar:user', 'sidebar:library',
    'sidebar:permission', 'sidebar:task', 'sidebar:runtime', 'sidebar:audit',
    'sidebar:qa-log',
];

const MENU_MAPPING = [
    ['/chat', 'sidebar:chat'],
    ['/documents', 'sidebar:document'],
    ['/search', 'sidebar:search'],
    ['/import', 'sidebar:import'],
    ['/api-keys', 'sidebar:api-key'],
    ['/dashboard', 'sidebar:overview'],
    ['/users', 'sidebar:user'],
    ['/libraries', 'sidebar:library'],
    ['/permissions', 'sidebar:permission'],
    ['/jobs', 'sidebar:task'],
    ['/operations', 'sidebar:runtime'],
    ['/audit', 'sidebar:audit'],
    ['/chat-logs', 'sidebar:qa-log'],
];

// ── SVG generation ──
test('all 13 sidebar icons generate SVG without external URLs', () => {
    for (const name of SIDEBAR_NAMES) {
        const svg = iconSvg(name);
        assert.ok(svg.startsWith('<svg'), `${name} starts with <svg>`);
        assert.ok(svg.includes('viewBox="0 0 24 24"'), `${name} has 24x24 viewBox`);
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.ok(!body.includes('http:'), `${name} has no http:`);
        assert.ok(!body.includes('https:'), `${name} has no https:`);
    }
});

test('sidebar icons use stroke-based rendering', () => {
    for (const name of SIDEBAR_NAMES) {
        const svg = iconSvg(name);
        assert.ok(svg.includes('stroke="currentColor"'), `${name} uses stroke`);
        assert.ok(svg.includes('stroke-width="1.8"'), `${name} has stroke-width 1.8`);
    }
});

// ── Layout.js menu verification ──
test('Layout.js uses sidebar:* icons for all 13 menu items', () => {
    for (const name of SIDEBAR_NAMES) {
        assert.ok(layoutSource.includes(name), `Layout.js includes ${name}`);
    }
});

test('each menu index has correct sidebar icon', () => {
    for (const [index, icon] of MENU_MAPPING) {
        const idx = layoutSource.indexOf(`index="${index}"`);
        assert.ok(idx >= 0, `menu item ${index} exists`);
        // Check the icon name appears after the menu item
        const after = layoutSource.slice(idx);
        assert.ok(after.includes(icon), `${index} → ${icon}`);
    }
});

test('Layout.js adds v0.8 knowledge management menus and preserves permissions', () => {
    const menuItems = layoutSource.match(/<el-menu-item\s+/g) || [];
    assert.equal(menuItems.length, 16, '16 menu items total');
    assert.ok(layoutSource.includes('index="/catalog"'), 'Catalog menu exists');
    assert.ok(layoutSource.includes('v-if="access.catalog"'), 'Catalog read guard exists');
    assert.ok(layoutSource.includes('mdi:bookshelf'), 'Catalog uses local bookshelf icon');
    assert.ok(layoutSource.includes('index="/retrieval-test"'), 'retrieval menu exists');
    assert.ok(layoutSource.includes('v-if="access.retrievalTest"'), 'retrieval admin guard exists');
    assert.ok(layoutSource.includes('index="/classification-review"'), 'classification review menu exists');
    assert.ok(layoutSource.includes('v-if="access.classificationReview"'), 'classification management guard exists');
    assert.ok(layoutSource.includes('mdi:shield-key-outline'), 'classification review uses a local icon');
    // Verify permission guards still present
    assert.ok(layoutSource.includes('v-if="access.chat"'), 'chat permission');
    assert.ok(layoutSource.includes('v-if="isSuper"'), 'admin group');
    assert.ok(layoutSource.includes('el-menu-item-group'), 'menu group wrapper');
});

test('collapse button icons unchanged', () => {
    assert.ok(layoutSource.includes("mdi:chevron-right"), 'chevron-right preserved');
    assert.ok(layoutSource.includes("mdi:chevron-left"), 'chevron-left preserved');
    assert.ok(layoutSource.includes("mdi:menu"), 'menu toggle preserved');
});

test('no duplicate sidebar icon definitions in ICONS', () => {
    for (const name of SIDEBAR_NAMES) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp(`'${escaped}':`, 'g');
        const matches = iconsSource.match(regex) || [];
        assert.equal(matches.length, 1, `${name} defined exactly once`);
    }
});

console.log('sidebar icons test passed');
