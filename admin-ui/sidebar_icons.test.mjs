import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { visibleSidebarDomains } from './src/domain_navigation.js';
import { iconSvg } from './src/icons.js';

const iconsSource = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');

const FULL_ACCESS = {
    knowledgeUse: true,
    knowledgeAssets: true,
    knowledgeGovernance: true,
    usersPermissions: true,
    libraries: true,
    operationsCenter: true,
    auditCenter: true,
    chat: true,
    documents: true,
    knowledgeGraph: true,
};

const DOMAIN_MENUS = visibleSidebarDomains(FULL_ACCESS);
const DOMAIN_ICONS = DOMAIN_MENUS.map((item) => item.icon);

test('seven functional domains provide local SVG sidebar icons', () => {
    assert.equal(DOMAIN_MENUS.length, 7);
    assert.deepEqual(DOMAIN_MENUS.map((item) => item.key), [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
        'usersPermissions',
        'libraries',
        'operationsCenter',
        'auditCenter',
    ]);

    for (const name of DOMAIN_ICONS) {
        const svg = iconSvg(name);
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.ok(svg.startsWith('<svg'), `${name} starts with <svg>`);
        assert.match(svg, /viewBox="0 0 (?:24 24|32 32)"/, `${name} has a local viewBox`);
        assert.ok(svg.includes('currentColor'), `${name} uses currentColor`);
        assert.ok(!body.includes('http:'), `${name} has no external http URL`);
        assert.ok(!body.includes('https:'), `${name} has no external https URL`);
    }
});

test('Layout renders one permission-aware menu loop instead of historical page entries', () => {
    assert.match(layoutSource, /v-for="item in domainMenus"/);
    assert.match(layoutSource, /:index="item\.key"/);
    assert.match(layoutSource, /:icon="item\.icon"/);
    assert.match(layoutSource, /visibleSidebarDomains/);
    assert.doesNotMatch(layoutSource, /index="\/(?:chat|documents|dashboard|api-keys)"/);
});

test('collapse and account menu icons remain wired', () => {
    for (const name of [
        'mdi:chevron-right',
        'mdi:chevron-left',
        'mdi:menu',
        'sidebar:user',
        'mdi:logout',
    ]) {
        assert.ok(layoutSource.includes(name), `Layout.js includes ${name}`);
    }
});

test('domain sidebar icon definitions are unique', () => {
    const iconDefinitions = iconsSource.slice(0, iconsSource.indexOf('\n};') + 3);
    for (const name of new Set(DOMAIN_ICONS)) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const matches = iconDefinitions.match(new RegExp(`'${escaped}':`, 'g')) || [];
        assert.equal(matches.length, 1, `${name} is defined exactly once`);
    }
});
