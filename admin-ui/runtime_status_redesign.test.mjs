import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    SERVICE_LABELS, STATUS_TAG, STATUS_TEXT, relTime,
    computeRuntimeSummary, formatOperationTime, rebuildStatusMeta,
} from './src/operations_ui.js';

// ══════════════════════════════════════════════════
//  operations_ui unit tests
// ══════════════════════════════════════════════════

test('computeRuntimeSummary: normal data returns correct aggregates', () => {
    const data = {
        services: [
            { service_type: 'api', status: 'online' },
            { service_type: 'embedding_worker', status: 'online' },
            { service_type: 'cleanup_worker', status: 'offline' },
        ],
        embedding_jobs: { pending: 5, processing: 2, done: 10, failed: 1, total: 18 },
        cleanup_outbox: { pending: 3, processing: 1, done: 8, failed: 0, dead_letter: 0, total: 12 },
        libraries: { total: 5, rebuilding: 2, failed: 1 },
    };
    const s = computeRuntimeSummary(data);
    assert.equal(s.onlineServices, 2);
    assert.equal(s.abnormalServices, 1);
    assert.equal(s.embeddingPending, 5);
    assert.equal(s.cleanupPending, 3);
    assert.equal(s.rebuilding, 2);
});

test('computeRuntimeSummary: null/undefined data returns all null', () => {
    const s = computeRuntimeSummary(null);
    assert.equal(s.onlineServices, null);
    assert.equal(s.abnormalServices, null);
    assert.equal(s.embeddingPending, null);
    assert.equal(s.cleanupPending, null);
    assert.equal(s.rebuilding, null);
});

test('computeRuntimeSummary: missing sub-objects return null (not 0)', () => {
    const data = { services: [] };
    const s = computeRuntimeSummary(data);
    assert.equal(s.embeddingPending, null);
    assert.equal(s.cleanupPending, null);
    assert.equal(s.rebuilding, null);
});

test('computeRuntimeSummary: actual zero values stay 0', () => {
    const data = {
        services: [],
        embedding_jobs: { pending: 0 },
        cleanup_outbox: { pending: 0 },
        libraries: { rebuilding: 0 },
    };
    const s = computeRuntimeSummary(data);
    assert.equal(s.embeddingPending, 0);
    assert.equal(s.cleanupPending, 0);
    assert.equal(s.rebuilding, 0);
});

test('formatOperationTime: valid ISO returns zh-CN 24h', () => {
    const r = formatOperationTime('2026-06-15T08:30:00');
    assert.ok(r.includes('2026'), `expected year in "${r}"`);
});

test('formatOperationTime: empty/null/invalid returns —', () => {
    assert.equal(formatOperationTime(''), '—');
    assert.equal(formatOperationTime(null), '—');
    assert.equal(formatOperationTime('not-a-date'), '—');
});

test('rebuildStatusMeta: preparing, running, other', () => {
    assert.deepEqual(rebuildStatusMeta('preparing'), { label: '准备中', type: 'warning' });
    assert.deepEqual(rebuildStatusMeta('running'), { label: '进行中', type: 'primary' });
    const other = rebuildStatusMeta('unknown');
    assert.equal(other.label, 'unknown');
    assert.equal(other.type, 'info');
});

test('relTime: negative treated as 0', () => {
    assert.equal(relTime(-5), '0 秒前');
    assert.equal(relTime(-999), '0 秒前');
});

test('relTime: normal values unchanged', () => {
    assert.equal(relTime(30), '30 秒前');
    assert.equal(relTime(90), '1 分前');
    assert.equal(relTime(7200), '2 小时前');
    assert.equal(relTime(null), '—');
});

test('STATUS_TAG: online=success, degraded=warning, offline=danger', () => {
    assert.equal(STATUS_TAG.online, 'success');
    assert.equal(STATUS_TAG.degraded, 'warning');
    assert.equal(STATUS_TAG.offline, 'danger');
});

// ══════════════════════════════════════════════════
//  Template regression
// ══════════════════════════════════════════════════
const src = readFileSync(new URL('./src/views/RuntimeStatus.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('根容器使用 runtime-workspace', () => {
    assert.ok(src.includes('runtime-workspace'), 'root class');
});

test('零 inline style', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.equal((tpl.match(/\sstyle="/g) || []).length, 0, 'zero inline style');
});

// ── Auto-refresh ──
test('autoRefresh 默认 true', () => {
    assert.ok(src.includes('autoRefresh = ref(true)'), 'autoRefresh defaults to true');
});

test('30 秒刷新周期', () => {
    assert.ok(src.includes('30'), '30 second constant');
});

test('onBeforeUnmount 清理定时器', () => {
    assert.ok(src.includes('onBeforeUnmount'), 'onBeforeUnmount imported');
    assert.ok(src.includes('clearInterval'), 'clearInterval called');
    assert.ok(src.includes('stopAutoRefresh'), 'stopAutoRefresh on unmount');
});

test('load() 第一行 if (loading.value) return 在 api 调用之前', () => {
    const loadFn = src.slice(src.indexOf('async function load'));
    const guardPos = loadFn.indexOf('if (loading.value) return');
    const apiPos = loadFn.indexOf('api.operationsStatus');
    assert.ok(guardPos > 0, 'loading guard exists');
    assert.ok(guardPos < apiPos, 'guard must be BEFORE api.operationsStatus');
});

test('手动和自动刷新都传 forceRefresh=true', () => {
    const intervalBlock = src.slice(src.indexOf('setInterval'));
    assert.ok(intervalBlock.includes('load(true)'), 'auto refresh calls load(true)');
    assert.ok(src.includes('@click="load(true)"'), 'manual refresh calls load(true)');
});

test('防止重复 timer（startAutoRefresh 检查 refreshTimer）', () => {
    assert.ok(src.includes('if (refreshTimer) return'), 'guards against duplicate timer');
});

test('防止重叠请求（loading.value guard）', () => {
    const intervalBlock = src.slice(src.indexOf('setInterval'));
    assert.ok(intervalBlock.includes('loading.value'), 'checks loading before auto-refresh');
});

test('首次失败显示错误状态和重新加载按钮', () => {
    assert.ok(src.includes('loadError'), 'loadError flag');
    assert.ok(src.includes('重新加载'), 'reload button');
    assert.ok(src.includes('!data && loadError'), 'first-time error state');
});

test('已有数据后刷新失败保留旧数据', () => {
    // Catch block: if (!data.value) loadError = true — only sets loadError when no prior data
    assert.ok(src.includes('!data.value'), 'checks if prior data exists');
    assert.ok(src.includes("loadError.value = true"), 'sets loadError only on first failure');
});

// ── dataFetchedAt / misc ──
test('dataFetchedAt 优先使用 d.now，缺失才用客户端时间', () => {
    assert.ok(src.includes('d.now'), 'prefers d.now for timestamp');
    assert.ok(src.includes("d.now || new Date()"), 'd.now || client time fallback');
});

test('未使用 nextTick', () => {
    assert.ok(!src.includes('nextTick'), 'nextTick import removed');
});

test('runtime-overview 始终渲染（无 v-if="data"）', () => {
    const tpl = src.slice(src.indexOf('template:'));
    const overviewSection = tpl.slice(tpl.indexOf('runtime-overview'));
    const endTag = overviewSection.indexOf('>');
    const openingTag = overviewSection.slice(0, endTag + 1);
    assert.ok(!openingTag.includes('v-if="data"'), 'overview must NOT have v-if="data"');
});

// ── 五格总览 ──
test('顶部总览为单卡五等分', () => {
    assert.ok(src.includes('runtime-overview'), 'overview card');
    assert.ok(src.includes('runtime-overview-item'), 'overview items');
    // 5 icons: service:api, status:failed, service:embedding-worker, service:cleanup-worker, status:processing
    assert.ok(src.includes('service:api'), 'online services icon');
    assert.ok(src.includes('status:failed'), 'abnormal services icon');
    assert.ok(src.includes('service:embedding-worker'), 'embedding icon');
    assert.ok(src.includes('service:cleanup-worker'), 'cleanup icon');
    assert.ok(src.includes('status:processing'), 'rebuilding icon');
});

test('数据未加载时显示 —', () => {
    assert.ok(src.includes("!= null ?"), 'null guard for overview values');
    assert.ok(src.includes(": '—'"), 'fallback to —');
});

// ── 服务在线状态卡 ──
test('服务表格六列：服务、在线状态、在线实例数、最后心跳、主机、PID', () => {
    assert.ok(src.includes('在线状态'), 'status column');
    assert.ok(src.includes('在线实例数'), 'instances column');
    assert.ok(src.includes('最后心跳'), 'heartbeat column');
    assert.ok(src.includes('主机'), 'hostname column');
    assert.ok(src.includes('PID'), 'PID column');
    // 主机和 PID 分列
    assert.ok(src.includes('row.hostname'), 'hostname separate');
    assert.ok(src.includes('row.pid'), 'pid separate');
});

test('在线实例数只显示 onlineInstances，不拼接 knownInstances', () => {
    // Template: {{ row.onlineInstances }} only, no row.knownInstances in the same cell
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(!tpl.includes('knownInstances'), 'must not display knownInstances in template');
    assert.ok(tpl.includes('row.onlineInstances'), 'must display onlineInstances');
});

test('最后心跳主文字精确时间 + 默认次要文字', () => {
    assert.ok(src.includes('row.lastSeenAt'), 'precise heartbeat time');
    assert.ok(src.includes('row.lastSeenRel'), 'relative heartbeat time');
    assert.ok(src.includes('runtime-heartbeat-rel'), 'heartbeat rel CSS');
});

test('表格 footer 显示共 N 条和数据时间', () => {
    assert.ok(src.includes('runtime-table-footer'), 'table footer');
    assert.ok(src.includes('数据时间'), 'data time label');
});

// ── 底部三卡 ──
test('底部三列卡片结构', () => {
    assert.ok(src.includes('runtime-bottom-grid'), 'bottom grid');
    assert.ok(src.includes('Embedding 任务'), 'embedding card title');
    assert.ok(src.includes('Cleanup Outbox'), 'cleanup card title');
    assert.ok(src.includes('重建状态'), 'rebuild card title');
});

test('Embedding 卡包含 router-link 到 /jobs', () => {
    assert.ok(src.includes('router-link'), 'router-link');
    assert.ok(src.includes('/jobs'), 'links to /jobs');
});

test('Cleanup 卡包含蓝色浅底说明', () => {
    assert.ok(src.includes('runtime-card-note'), 'note class');
    assert.ok(src.includes('异步清理已删除文档'), 'cleanup note text');
});

test('重建卡显示中文状态和进度条', () => {
    assert.ok(src.includes('rebuildStatusMeta'), 'uses rebuildStatusMeta');
    assert.ok(src.includes('el-progress'), 'progress bar');
});

test('无活动重建时显示紧凑空状态', () => {
    assert.ok(src.includes('runtime-empty-compact'), 'compact empty state');
    assert.ok(src.includes('当前没有进行中的重建'), 'no rebuilds text');
});

// ── 无虚构字段和接口 ──
test('仅使用 operationsStatus，无新增接口', () => {
    assert.ok(src.includes('api.operationsStatus'), 'calls operationsStatus');
    assert.ok(!src.includes('api.listJobs'), 'no listJobs');
    assert.ok(!src.includes('api.listLibraries'), 'no listLibraries');
});

test('不显示虚构字段（更新时间、优先级、平均耗时）', () => {
    assert.ok(!src.includes('更新时间'), 'no 更新时间');
    assert.ok(!src.includes('优先级'), 'no 优先级');
    assert.ok(!src.includes('平均耗时'), 'no 平均耗时');
});

// ── CSS ──
test('CSS defines runtime-workspace, runtime-overview, runtime-bottom-grid', () => {
    for (const c of ['runtime-workspace', 'runtime-overview', 'runtime-bottom-grid', 'runtime-card']) {
        assert.ok(css.includes(c), `${c} in CSS`);
    }
});

test('CSS 1199px 和 899px 响应式', () => {
    assert.ok(css.includes('1199px'), '1199px media query');
    assert.ok(css.includes('899px'), '899px media query');
    assert.ok(css.includes('runtime-bottom-grid'), 'responsive bottom grid');
});

test('表格仅 runtime-table-shell 内横向滚动', () => {
    assert.ok(css.includes('runtime-table-shell'), 'table shell');
    assert.ok(css.includes('overflow-x:auto'), 'horizontal scroll on shell');
});

test('现有 service 图标映射不变', () => {
    assert.ok(src.includes("api: 'service:api'"), 'api→service:api');
    assert.ok(src.includes("embedding_worker: 'service:embedding-worker'"), 'embedding_worker→service:embedding-worker');
    assert.ok(src.includes("cleanup_worker: 'service:cleanup-worker'"), 'cleanup_worker→service:cleanup-worker');
});

// Run with: node --check src/views/RuntimeStatus.js
// Run with: node --check src/operations_ui.js

console.log('runtime status redesign test passed');
