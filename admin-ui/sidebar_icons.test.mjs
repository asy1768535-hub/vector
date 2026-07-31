import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { visibleSidebarGroups } from './src/domain_navigation.js';
import { iconSvg } from './src/icons.js';

const iconsSource = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');

const FULL_ACCESS = {
    knowledgeUse: true,
    knowledgeAssets: true,
    knowledgeGovernance: true,
    usersPermissions: true,
    libraries: true,
    libraryConfiguration: true,
    operationsCenter: true,
    auditCenter: true,
    chat: true,
    search: true,
    retrievalTest: true,
    documents: true,
    catalog: true,
    import: true,
    knowledgeGraph: true,
    schemaLifecycle: true,
    classificationReview: true,
    account: true,
    apiKeys: true,
};

const SIDEBAR_GROUPS = visibleSidebarGroups(FULL_ACCESS);
const SIDEBAR_ICONS = SIDEBAR_GROUPS.flatMap((group) => [
    group.icon,
    ...group.items.map((item) => item.icon),
]);

test('eight functional groups and their leaf pages provide local SVG sidebar icons', () => {
    assert.equal(SIDEBAR_GROUPS.length, 8);
    assert.deepEqual(SIDEBAR_GROUPS.map((item) => item.key), [
        'knowledgeUse',
        'knowledgeAssets',
        'knowledgeGovernance',
        'account',
        'usersPermissions',
        'libraries',
        'operationsCenter',
        'auditCenter',
    ]);
    const leafItems = SIDEBAR_GROUPS.flatMap((group) => group.items);
    assert.equal(leafItems.length, 17);
    assert.equal(
        leafItems.filter((item) => item.label === '知识内容').length,
        1,
    );

    for (const name of SIDEBAR_ICONS) {
        const svg = iconSvg(name);
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.ok(svg.startsWith('<svg'), `${name} starts with <svg>`);
        assert.match(svg, /viewBox="0 0 (?:24 24|32 32)"/, `${name} has a local viewBox`);
        assert.ok(svg.includes('currentColor'), `${name} uses currentColor`);
        assert.ok(!body.includes('http:'), `${name} has no external http URL`);
        assert.ok(!body.includes('https:'), `${name} has no external https URL`);
    }
});

test('Layout renders permission-aware groups with direct leaf-page entries', () => {
    assert.match(layoutSource, /sidebarEntries/);
    assert.match(layoutSource, /v-for="entry in sidebarEntries"/);
    assert.match(layoutSource, /:index="entry\.path"/);
    assert.match(layoutSource, /:icon="entry\.icon"/);
    assert.match(layoutSource, /visibleSidebarGroups/);
    assert.doesNotMatch(layoutSource, /el-sub-menu/);
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
    for (const name of new Set(SIDEBAR_ICONS)) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const matches = iconDefinitions.match(new RegExp(`'${escaped}':`, 'g')) || [];
        assert.equal(matches.length, 1, `${name} is defined exactly once`);
    }
});
