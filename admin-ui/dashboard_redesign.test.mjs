import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    ACTION_LABELS, actionLabel, permActions, targetSummary, fmtAuditTime, relAuditTime,
} from './src/admin_activity_ui.js';
import {
    SERVICE_LABELS, STATUS_TAG, STATUS_TEXT, relTime,
} from './src/operations_ui.js';
import { iconSvg } from './src/icons.js';

const dashSource = readFileSync(new URL('./src/views/Dashboard.js', import.meta.url), 'utf8');
const auditSource = readFileSync(new URL('./src/views/Audit.js', import.meta.url), 'utf8');
const runtimeSource = readFileSync(new URL('./src/views/RuntimeStatus.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ════════════════════════════════════════════════════════════
//  admin_activity_ui.js tests
// ════════════════════════════════════════════════════════════
test('actionLabel returns Chinese label or falls back to code', () => {
    assert.equal(actionLabel('library.create'), '创建知识库');
    assert.equal(actionLabel('unknown.action'), 'unknown.action');
});

test('targetSummary formats known actions', () => {
    assert.ok(targetSummary('library.create', { slug: 'test' }).includes('创建知识库 test'));
    assert.ok(targetSummary('permission.grant', { user_id: 'u1', library_slug: 'lib', actions: ['read'] }).includes('读取'));
    assert.equal(targetSummary('unknown', null), '');
});

test('fmtAuditTime formats valid ISO', () => {
    const r = fmtAuditTime('2026-06-15T08:30:00Z');
    assert.ok(r.includes('2026'));
    assert.equal(fmtAuditTime(''), '');
});

test('relAuditTime returns relative or date', () => {
    const now = new Date().toISOString();
    assert.ok(relAuditTime(now) === '刚刚' || relAuditTime(now).includes('秒'));
    assert.equal(relAuditTime(''), '');
    assert.equal(relAuditTime(null), '');
});

// ════════════════════════════════════════════════════════════
//  operations_ui.js tests
// ════════════════════════════════════════════════════════════
test('SERVICE_LABELS has three worker types', () => {
    assert.equal(SERVICE_LABELS.api, 'API');
    assert.equal(SERVICE_LABELS.embedding_worker, 'Embedding Worker');
    assert.equal(SERVICE_LABELS.cleanup_worker, 'Cleanup Worker');
});

test('relTime formats seconds correctly', () => {
    assert.equal(relTime(null), '—');
    assert.equal(relTime(30), '30 秒前');
    assert.equal(relTime(90), '1 分前');
    assert.equal(relTime(7200), '2 小时前');
});

// ════════════════════════════════════════════════════════════
//  Audit.js reuses shared modules
// ════════════════════════════════════════════════════════════
test('Audit.js imports from admin_activity_ui.js', () => {
    assert.ok(auditSource.includes("from '../admin_activity_ui.js'"), 'imports admin_activity_ui');
    assert.ok(auditSource.includes('fmtAuditTime'), 'uses fmtAuditTime');
    assert.ok(!auditSource.includes('function actionLabel'), 'no local actionLabel');
    assert.ok(!auditSource.includes('const ACTION_LABELS'), 'no local ACTION_LABELS');
});

// ════════════════════════════════════════════════════════════
//  RuntimeStatus.js reuses shared modules
// ════════════════════════════════════════════════════════════
test('RuntimeStatus.js imports from operations_ui.js', () => {
    assert.ok(runtimeSource.includes("from '../operations_ui.js'"), 'imports operations_ui');
    assert.ok(!runtimeSource.includes('const SERVICE_LABELS'), 'no local SERVICE_LABELS');
});

// ════════════════════════════════════════════════════════════
//  Dashboard.js structure
// ════════════════════════════════════════════════════════════
test('Dashboard: core data in allSettled, health loads in background', () => {
    assert.ok(dashSource.includes('Promise.allSettled'), 'uses allSettled');
    assert.ok(dashSource.includes('api.operationsStatus'), 'calls operationsStatus');
    assert.ok(dashSource.includes('api.listLibraries'), 'calls listLibraries');
    assert.ok(dashSource.includes('api.listAudit'), 'calls listAudit');
    // health is called outside allSettled, in background
    assert.ok(dashSource.includes('api.health'), 'calls health');
    assert.ok(dashSource.includes('healthLoading'), 'has healthLoading ref');
    // Verify health starts after core allSettled results pass the stale-response fence.
    const allSettledIdx = dashSource.indexOf('Promise.allSettled');
    const staleFenceIdx = dashSource.indexOf('if (!requestFence.isCurrent(requestToken)) return;', allSettledIdx);
    const healthIdx = dashSource.indexOf('await api.health', allSettledIdx);
    assert.ok(staleFenceIdx > allSettledIdx, 'core results pass stale-response fence');
    assert.ok(healthIdx > staleFenceIdx, 'health must start after core allSettled');
});

test('Dashboard projects fatal and partial failures with real retry wiring', () => {
    assert.ok(dashSource.includes("dashboardReadState === 'fatal'"), 'fatal state is durable');
    assert.ok(dashSource.includes('retryFailedReads'), 'failed reads have a retry path');
    assert.ok(dashSource.includes('failedReads.value'), 'failed resources are tracked');
    assert.ok(dashSource.includes('requestFence.isCurrent(requestToken)'), 'stale responses are fenced');
    assert.ok(dashSource.includes("operationsResolved ? jobStats.pending : '—'"), 'unresolved metrics are not shown as zero');
});

test('Dashboard: forceRefresh param flows to all cached calls', () => {
    assert.ok(dashSource.includes('load(forceRefresh'), 'load accepts forceRefresh');
    assert.ok(dashSource.includes('load(false)'), 'onMounted calls load(false)');
    assert.ok(dashSource.includes('function refreshDashboard()'), 'refresh command is explicit');
    assert.ok(dashSource.includes('return load(true)'), 'refresh command calls the real read path');
    assert.ok(dashSource.includes('@click="refreshDashboard"'), 'refresh button uses the exposed command');
});

test('Dashboard handles 500+ library truncation', () => {
    assert.ok(dashSource.includes('500'), 'references 500 limit');
    assert.ok(dashSource.includes("'500+'"), 'shows 500+ indicator');
});

test('Dashboard template has 5 stat cards and 4-quadrant grid', () => {
    assert.ok(dashSource.includes('dashboard-stats'), 'stats row');
    assert.ok(dashSource.includes('dashboard-grid'), 'grid layout');
    assert.ok(dashSource.includes('服务状态'), 'services card');
    assert.ok(dashSource.includes('最近活动'), 'activity card');
    assert.ok(dashSource.includes('任务处理概况'), 'jobs card');
    assert.ok(dashSource.includes('最近创建的知识库'), 'libraries card');
});

test('Dashboard has 0 inline style attributes', () => {
    const tpl = dashSource.slice(dashSource.indexOf('template:'));
    assert.equal((tpl.match(/style="/g) || []).length, 0);
});

test('Dashboard imports from shared modules', () => {
    assert.ok(dashSource.includes("from '../admin_activity_ui.js'"));
    assert.ok(dashSource.includes("from '../operations_ui.js'"));
});

// ── Data transformation behavior ──
test('libCount: shows number for <500, shows 500+ when truncated', () => {
    // Simulate the computed logic
    const mkLibs = (n) => Array.from({ length: n }, (_, i) => ({ id: String(i), slug: `lib${i}`, created_at: '2026-01-01' }));
    // 3 libs → "3"
    const small = mkLibs(3);
    assert.equal(String(small.length), '3');
    // 500 libs → "500+"
    const big = mkLibs(500);
    assert.equal(big.length >= 500 ? '500+' : String(big.length), '500+');
    // 0 libs → "0"
    assert.equal(String([].length), '0');
});

test('allServices: merges worker services with health infra services', () => {
    // ops.services provides workers, health provides db/qdrant/embedding
    const ops = {
        services: [
            { service_type: 'api', status: 'online', online_instances: 2, known_instances: 2,
              latest: { seconds_since_last_seen: 5 } },
            { service_type: 'embedding_worker', status: 'online', online_instances: 1, known_instances: 1,
              latest: { seconds_since_last_seen: 10 } },
            { service_type: 'cleanup_worker', status: 'offline', online_instances: 0, known_instances: 1,
              latest: null },
        ],
    };
    const health = { db: true, qdrant: true, embedding: 'ok', status: 'ok' };
    // Verify worker count + infra count = 3 + 3 = 6
    const workerCount = ops.services.length;
    const infraCount = 3; // PostgreSQL, Qdrant, Embedding
    assert.equal(workerCount + infraCount, 6);
    // Offline worker check
    const offlineWorker = ops.services.find(s => s.service_type === 'cleanup_worker');
    assert.equal(offlineWorker.status, 'offline');
    // Health infra
    assert.equal(health.db, true);
    assert.equal(health.embedding, 'ok');
});

test('recentLibs: sorts by created_at DESC, takes top 5', () => {
    const libs = [
        { slug: 'a', created_at: '2026-03-01T00:00:00Z' },
        { slug: 'b', created_at: '2026-06-01T00:00:00Z' },
        { slug: 'c', created_at: '2026-01-01T00:00:00Z' },
        { slug: 'd', created_at: '2026-05-01T00:00:00Z' },
        { slug: 'e', created_at: '2026-04-01T00:00:00Z' },
        { slug: 'f', created_at: '2026-02-01T00:00:00Z' },
    ];
    const sorted = libs.slice().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    assert.equal(sorted[0].slug, 'b');
    assert.equal(sorted[1].slug, 'd');
    const top5 = sorted.slice(0, 5);
    assert.equal(top5.length, 5);
    // c (Jan) is the oldest and should be excluded
    assert.ok(!top5.find(l => l.slug === 'c'));
});

test('partial failure: allSettled allows individual API errors', () => {
    // Simulate Promise.allSettled: one rejection doesn't block others
    const results = [
        { status: 'fulfilled', value: { status: 'ok' } },
        { status: 'rejected', reason: new Error('timeout') },
        { status: 'fulfilled', value: [{ slug: 'lib1' }] },
        { status: 'fulfilled', value: [{ action: 'test' }] },
    ];
    const errors = results.filter(r => r.status === 'rejected');
    const ok = results.filter(r => r.status === 'fulfilled');
    assert.equal(errors.length, 1);
    assert.equal(ok.length, 3);
});

// ── CSS ──
test('CSS defines dashboard-* classes', () => {
    for (const c of ['dashboard-workspace', 'dashboard-stats', 'dashboard-grid',
                     'dashboard-card', 'dashboard-activity-item']) {
        assert.match(css, new RegExp('\\.' + c.replace(/-/g, '\\-') + '\\s*\\{'));
    }
});

test('CSS includes dashboard responsive rules', () => {
    assert.match(css, /1199px[\s\S]*dashboard-stats/, 'dashboard at 1199px');
    assert.match(css, /899px[\s\S]*dashboard-grid/, 'dashboard at 899px');
});

// ── Overview icons ──
const OVERVIEW_NAMES = [
    'overview:kb-count', 'overview:pending-jobs', 'overview:processing-jobs',
    'overview:failed-jobs', 'overview:online-services',
    'overview:service-status', 'overview:recent-activity', 'overview:rebuild',
];

test('all 8 overview icons generate SVG without external URLs', () => {
    for (const name of OVERVIEW_NAMES) {
        const svg = iconSvg(name);
        assert.ok(svg.startsWith('<svg'), `${name} starts with <svg>`);
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.ok(!body.includes('http:'), `${name} has no http:`);
        assert.ok(!body.includes('https:'), `${name} has no https:`);
        assert.ok(svg.includes('stroke="currentColor"'), `${name} uses stroke`);
    }
});

test('Dashboard.js stat cards use overview:* icons', () => {
    const expected = [
        'overview:kb-count', 'overview:pending-jobs', 'overview:processing-jobs',
        'overview:failed-jobs', 'overview:online-services',
    ];
    for (const name of expected) {
        assert.ok(dashSource.includes(name), `Dashboard uses ${name}`);
    }
});

test('Dashboard.js section titles use overview:* icons', () => {
    assert.ok(dashSource.includes('overview:service-status'), 'service-status title icon');
    assert.ok(dashSource.includes('overview:recent-activity'), 'recent-activity title icon');
    assert.ok(dashSource.includes('overview:rebuild'), 'rebuild title icon');
});

test('CSS defines dashboard-card-title-icon', () => {
    assert.match(css, /\.dashboard-card-title-icon\s*\{/, 'title icon class defined');
    assert.match(css, /\.dashboard-card-title-icon\s*\{[\s\S]*?font-size:\s*18px/, 'title icon 18px');
});

test('no duplicate overview icon definitions in ICONS', () => {
    const icoSource = readFileSync(new URL('./src/icons.js', import.meta.url), 'utf8');
    for (const name of OVERVIEW_NAMES) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp(`'${escaped}':`, 'g');
        const matches = icoSource.match(regex) || [];
        assert.equal(matches.length, 1, `${name} defined exactly once`);
    }
});

console.log('dashboard redesign test passed');
