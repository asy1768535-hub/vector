import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { iconSvg } from './src/icons.js';

const iconsSource = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');

// ── Expected icon keys ──
const STATUS_KEYS = [
    'status:pending', 'status:processing', 'status:success', 'status:failed',
    'status:skipped', 'status:retry', 'status:partial-failed',
];
const SERVICE_KEYS = [
    'service:api', 'service:embedding-worker', 'service:cleanup-worker',
    'service:qdrant', 'service:postgresql', 'service:embedding-service',
];
const ALL_NEW_KEYS = [...STATUS_KEYS, ...SERVICE_KEYS];

// ── SVG generation ──
test('all 13 status/service icons generate SVG with 24×24 viewBox', () => {
    for (const name of ALL_NEW_KEYS) {
        const svg = iconSvg(name);
        assert.ok(svg.startsWith('<svg'), `${name} starts with <svg>`);
        assert.ok(svg.includes('viewBox="0 0 24 24"'), `${name} has 24x24 viewBox, got: ${svg.slice(0, 120)}`);
    }
});

test('status/service icons have no external URLs or scripts', () => {
    for (const name of ALL_NEW_KEYS) {
        const svg = iconSvg(name);
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.ok(!body.includes('http:'), `${name} contains http:`);
        assert.ok(!body.includes('https:'), `${name} contains https:`);
        assert.ok(!body.includes('cdn'), `${name} references CDN`);
        assert.ok(!body.includes('<script'), `${name} contains <script>`);
    }
});

test('status icons use fixed semantic colors, not currentColor', () => {
    // The status icons carry their own fixed color palette — they should NOT depend on currentColor.
    for (const name of STATUS_KEYS) {
        const svg = iconSvg(name);
        assert.ok(!svg.includes('stroke="currentColor"'), `${name} should not use currentColor (fixed semantic colors only)`);
        assert.ok(!svg.includes('fill="currentColor"'), `${name} should not use fill=currentColor`);
    }
});

test('service icons use fixed semantic colors, not currentColor', () => {
    for (const name of SERVICE_KEYS) {
        const svg = iconSvg(name);
        assert.ok(!svg.includes('stroke="currentColor"'), `${name} should not use currentColor (fixed semantic colors only)`);
        assert.ok(!svg.includes('fill="currentColor"'), `${name} should not use fill=currentColor`);
    }
});

test('all 13 icon keys are unique and present exactly once in icons.js', () => {
    for (const name of ALL_NEW_KEYS) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp(`'${escaped}':`, 'g');
        const matches = iconsSource.match(re);
        assert.ok(matches, `${name} key not found in icons.js`);
        assert.equal(matches.length, 1, `${name} appears ${matches.length} times, expected exactly 1`);
    }
});

test('no forbidden icon prefixes leaked into icons.js', () => {
    // file-*, op-*, search-*, cap-* must NOT be registered as icon keys
    const forbiddenPatterns = ["'file-", "'op-", "'search-", "'cap-"];
    for (const p of forbiddenPatterns) {
        assert.ok(!iconsSource.includes(p), `icons.js should not contain ${p}* keys`);
    }
});

// ── Jobs.js mapping ──
const jobsSource = readFileSync(new URL('./src/views/Jobs.js', import.meta.url), 'utf8');

test('Jobs.js imports STATUS_ICON from jobs_ui.js', () => {
    assert.ok(jobsSource.includes('STATUS_ICON'), 'Jobs.js should import STATUS_ICON');
});

test('Jobs stats cards use status:* icons', () => {
    assert.ok(jobsSource.includes('icon="status:pending"'), 'stats card uses status:pending');
    assert.ok(jobsSource.includes('icon="status:processing"'), 'stats card uses status:processing');
    assert.ok(jobsSource.includes('icon="status:success"'), 'stats card uses status:success');
    assert.ok(jobsSource.includes('icon="status:failed"'), 'stats card uses status:failed');
});

test('Jobs status column uses STATUS_ICON mapping', () => {
    assert.ok(jobsSource.includes('STATUS_ICON[row.status]'), 'status column should use STATUS_ICON mapping');
});

test('Jobs retry button uses status:retry icon', () => {
    assert.ok(jobsSource.includes('icon="status:retry"'), 'retry button should use status:retry icon');
});

test('Jobs status labels remain as Chinese text (icons are auxiliary)', () => {
    // Status labels are still displayed — icons only assist, not replace text.
    assert.ok(jobsSource.includes('STATUS_LABEL[row.status]'), 'status label text still present in status column');
    assert.ok(jobsSource.includes('重试'), 'retry button text still 重试');
    assert.ok(jobsSource.includes('待处理'), 'stats still show 待处理');
    assert.ok(jobsSource.includes('已完成'), 'stats still show 已完成');
    assert.ok(jobsSource.includes('失败'), 'stats still show 失败');
});

// ── RuntimeStatus.js mapping ──
const runtimeSource = readFileSync(new URL('./src/views/RuntimeStatus.js', import.meta.url), 'utf8');

test('RuntimeStatus.js has SERVICE_ICON mapping for all 3 known service types', () => {
    assert.ok(runtimeSource.includes("api: 'service:api'"), 'api → service:api');
    assert.ok(runtimeSource.includes("embedding_worker: 'service:embedding-worker'"), 'embedding_worker → service:embedding-worker');
    assert.ok(runtimeSource.includes("cleanup_worker: 'service:cleanup-worker'"), 'cleanup_worker → service:cleanup-worker');
});

test('RuntimeStatus service table uses row.icon for dynamic icon binding', () => {
    assert.ok(runtimeSource.includes(':icon="row.icon"'), 'service table uses dynamic :icon="row.icon"');
    assert.ok(runtimeSource.includes('icon: SERVICE_ICON[s.service_type]'), 'icon derived from SERVICE_ICON mapping');
});

test('RuntimeStatus service labels unchanged (icons are auxiliary)', () => {
    // The service name labels come from SERVICE_LABELS — unchanged.
    assert.ok(runtimeSource.includes('SERVICE_LABELS[s.service_type]'), 'SERVICE_LABELS still drives service names');
});

// ── jobs_ui.js mapping correctness ──
const jobsUiSource = readFileSync(new URL('./src/jobs_ui.js', import.meta.url), 'utf8');

test('jobs_ui.js STATUS_ICON mapping is correct', () => {
    assert.ok(jobsUiSource.includes("pending: 'status:pending'"), 'pending → status:pending');
    assert.ok(jobsUiSource.includes("processing: 'status:processing'"), 'processing → status:processing');
    assert.ok(jobsUiSource.includes("done: 'status:success'"), 'done → status:success');
    assert.ok(jobsUiSource.includes("failed: 'status:failed'"), 'failed → status:failed');
    assert.ok(jobsUiSource.includes("superseded: 'status:skipped'"), 'superseded → status:skipped');
});
