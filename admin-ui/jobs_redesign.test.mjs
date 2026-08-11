import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL,
    formatJobTime, jobDuration, shortId, filterJobs, paginateJobs, libraryName,
    jobErrorText, jobStageLabel, retryReasonLabel, statsStatusTotal,
    retryTargetKey, isRetrySelectable, uniqueRetryRows, retryItem, retryTypeSummary,
} from './src/jobs_ui.js';

const src = readFileSync(new URL('./src/views/Jobs.js', import.meta.url), 'utf8');
const uiSrc = readFileSync(new URL('./src/jobs_ui.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('job labels and pure helpers remain stable', () => {
    assert.equal(STATUS_LABEL.done, '已完成');
    assert.equal(STATUS_TAG.failed, 'danger');
    assert.equal(STATUS_ICON.processing, 'status:processing');
    assert.equal(TASK_TYPE_LABEL.graph, '知识图谱');
    assert.equal(STAGE_LABEL.extracting, '抽取实体与关系');
    assert.ok(formatJobTime('2026-06-15T08:30:00Z').includes('2026'));
    assert.equal(formatJobTime(null), '—');
    assert.equal(jobDuration({ created_at: 'bad' }), '—');
    assert.equal(shortId('abcdefghijklmno'), 'abcdefgh…');
    assert.equal(libraryName([{ id: 'l1', name: '法规库', slug: 'law' }], 'l1'), '法规库 (law)');
});

test('job filters and pagination preserve result meaning', () => {
    const jobs = [
        { id: '1', task_type: 'import', status: 'pending', library_id: 'l1', worker_id: 'w1', document_id: 'd1', created_at: '2026-06-01T00:00:00Z' },
        { id: '2', task_type: 'graph', status: 'done', library_id: 'l2', worker_id: 'w2', document_id: 'd2', created_at: '2026-07-01T00:00:00Z' },
    ];
    assert.equal(filterJobs(jobs, { status: 'pending' }).length, 1);
    assert.equal(filterJobs(jobs, { task_type: 'graph' }).length, 1);
    assert.equal(paginateJobs(Array.from({ length: 25 }, (_, i) => ({ id: String(i) })), 1, 10).total, 25);
});

test('failure and retry semantics are explicit', () => {
    assert.equal(jobErrorText({ status: 'failed', task_type: 'graph', last_error: null }), '图谱抽取失败，未记录详细错误');
    assert.equal(jobErrorText({ status: 'failed', task_type: 'import', last_error: null }), '文件导入失败，未记录详细错误');
    assert.equal(jobErrorText({ status: 'failed', task_type: 'embedding', last_error: 'upstream' }), 'upstream');
    assert.equal(jobErrorText({ status: 'failed', task_type: 'legacy', last_error: null }), '历史任务未记录错误');
    assert.equal(jobStageLabel({ status: 'failed', stage: 'finalizing' }), '收尾阶段失败');
    assert.equal(retryReasonLabel({ retry_capability: 'unsupported' }), '当前任务类型暂不支持');
    assert.equal(retryReasonLabel({ retry_capability: 'exhausted' }), '尝试次数耗尽');
});

test('stats status counts close over total', () => {
    const stats = { pending: 2, processing: 3, done: 10, failed: 4, cancelled: 1, superseded: 2, total: 22 };
    assert.equal(statsStatusTotal(stats), stats.total);
});

test('retry targets support all task types and deduplicate underlying jobs', () => {
    const rows = [
        { status: 'failed', retryable: true, retry_target_type: 'embedding', retry_target_id: 'e1', retry_generation: 3 },
        { status: 'failed', retryable: true, retry_target_type: 'graph', retry_target_id: 'g1', retry_generation: 2 },
        { status: 'failed', retryable: true, retry_target_type: 'import', retry_target_id: 'i1', retry_generation: 1 },
        { status: 'failed', retryable: true, retry_target_type: 'embedding', retry_target_id: 'e1', retry_generation: 3 },
        { status: 'failed', retryable: false, retry_target_type: 'graph', retry_target_id: 'g2', retry_generation: 1 },
        { status: 'processing', retryable: true, retry_target_type: 'import', retry_target_id: 'i2', retry_generation: 1 },
    ];
    assert.equal(retryTargetKey(rows[0]), 'embedding:e1');
    assert.equal(isRetrySelectable(rows[0]), true);
    assert.equal(isRetrySelectable(rows[4]), false);
    assert.deepEqual(uniqueRetryRows(rows).map((row) => retryTargetKey(row)), ['embedding:e1', 'graph:g1', 'import:i1']);
    assert.deepEqual(retryItem(rows[1]), { task_type: 'graph', job_id: 'g1', observed_generation: 2 });
    assert.equal(retryTypeSummary(rows), '向量 1 条、图谱 1 条、导入 1 条');
});

test('details use a dialog and never force page scrolling', () => {
    assert.ok(src.includes('el-dialog'));
    assert.ok(src.includes('v-model="detailOpen"'));
    assert.ok(src.includes('@closed="clearDetail"'));
    assert.ok(!src.includes('el-drawer'));
    assert.ok(!src.includes('jobs-detail-card'));
    assert.ok(!src.includes('nextTick'));
    assert.ok(!src.includes('detailCardRef'));
    assert.ok(!src.includes('scrollIntoView'));
    assert.ok(src.includes('detailOpen.value = true'));
    assert.ok(src.includes('function clearDetail'));
});

test('stale detail closes after filters, pages, or refresh', () => {
    assert.ok(src.includes('watch([filtered, paged]'));
    assert.ok(src.includes('!paged.value.items.some'));
    assert.ok(src.includes('selectedJob.value = fresh'));
    assert.ok(src.includes('else closeDetail()'));
    assert.ok(src.includes('page.value = 1'));
});

test('retry actions are capability-gated and never call embedding for other types', () => {
    assert.ok(src.includes('v-if="row.retryable"'));
    assert.ok(src.includes('jobs-retry-unavailable'));
    assert.ok(!src.includes(':title="retryReasonLabel(row)"'));
    assert.ok(!src.includes('{{ retryReasonLabel(row) }}'));
    assert.ok(src.includes('retryReasonLabel(selectedJob)'));
    assert.ok(!src.includes(':disabled="!row.retryable"'));
    assert.ok(src.includes('api.retryMonitoredTasks'));
    assert.ok(src.includes('retryTargetKey'));
    assert.ok(src.includes('uniqueRetryRows'));
    assert.ok(uiSrc.includes('retry_target_type'));
    assert.ok(uiSrc.includes('retry_target_id'));
    assert.ok(uiSrc.includes('observed_generation'));
    assert.ok(src.includes('retryTypeSummary'));
    assert.ok(src.includes('retryResultSummary'));
    assert.ok(!src.includes('api.retryJob'));
    assert.ok(!src.includes('Promise.allSettled(rows.map((row) => api.retryJob(row.id)))'));
    assert.ok(src.includes('retryableCount'));
    assert.ok(src.includes('当前失败任务均不可重试'));
    assert.ok(src.includes('canResetFailed'));
});

test('truncation threshold matches request limit and stats show all terminal states', () => {
    assert.ok(src.includes('listMonitoredTasks({ limit: 1500 }'));
    assert.ok(src.includes('length >= 1500'));
    assert.ok(src.includes('前1500条'));
    assert.ok(src.includes('stats.cancelled'));
    assert.ok(src.includes('stats.superseded'));
    assert.ok(src.includes('statsStatusTotal(stats)'));
});

test('table layout keeps long values bounded and operation column scroll-safe', () => {
    assert.ok(src.includes('jobs-library-name'));
    assert.ok(src.includes(':title="libraryName(libs, row.library_id)"'));
    assert.ok(src.includes(':title="jobErrorText(row)"'));
    assert.ok(!src.includes('fixed="right"'));
    assert.match(css, /\.jobs-table-shell \.el-table\s*\{[^}]*min-width:1500px/s);
    assert.match(css, /\.jobs-table-actions\s*\{[^}]*display:flex/s);
    assert.match(css, /\.jobs-retry-unavailable\s*\{[^}]*white-space:normal/s);
    assert.ok(!css.includes('.jobs-retry-unavailable { color:var(--app-text-muted); font-size:12px; white-space:nowrap; }'));
    assert.match(css, /\.jobs-detail-dialog \.el-dialog__body\s*\{[^}]*max-height:calc\(90vh/s);
    assert.match(css, /\.jobs-detail-dialog\s*\{[^}]*max-width:calc\(100vw - 32px\)/s);
});

console.log('jobs redesign test passed');
