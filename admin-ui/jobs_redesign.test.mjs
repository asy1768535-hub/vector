import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { STATUS_LABEL, STATUS_TAG, STATUS_ICON, TASK_TYPE_LABEL, STAGE_LABEL, formatJobTime, jobDuration, shortId, filterJobs, paginateJobs, libraryName } from './src/jobs_ui.js';

// ── Unit tests ──
test('STATUS_LABEL maps all statuses', () => {
    assert.equal(STATUS_LABEL.pending, '待处理');
    assert.equal(STATUS_LABEL.processing, '处理中');
    assert.equal(STATUS_LABEL.done, '已完成');
    assert.equal(STATUS_LABEL.failed, '已失败');
});

test('STATUS_ICON maps statuses correctly', () => {
    assert.equal(STATUS_ICON.pending, 'status:pending');
    assert.equal(STATUS_ICON.processing, 'status:processing');
    assert.equal(STATUS_ICON.done, 'status:success');
    assert.equal(STATUS_ICON.failed, 'status:failed');
});

test('STATUS_TAG maps tag types', () => {
    assert.equal(STATUS_TAG.done, 'success');
    assert.equal(STATUS_TAG.failed, 'danger');
});

test('formatJobTime: valid ISO and null', () => {
    assert.ok(formatJobTime('2026-06-15T08:30:00Z').includes('2026'));
    assert.equal(formatJobTime(null), '—');
});

test('jobDuration: uses claimed_at before created_at', () => {
    const recent = new Date(Date.now() - 65000).toISOString();
    const old = new Date(Date.now() - 3600000).toISOString();
    const dur1 = jobDuration({ claimed_at: recent, created_at: old });
    assert.ok(dur1.includes('m') || dur1.includes('s'), `claimed_at ~1m, got ${dur1}`);
    const dur2 = jobDuration({ created_at: recent });
    assert.ok(dur2.includes('m') || dur2.includes('s'), `created_at ~1m, got ${dur2}`);
    assert.equal(jobDuration({ created_at: 'bad' }), '—');
});

test('jobDuration: 非空但非法的 finished_at 必须返回 —', () => {
    const recent = new Date(Date.now() - 60000).toISOString();
    // finished_at is a non-empty garbage string — must return '—', NOT fallback to Date.now()
    const dur = jobDuration({ claimed_at: recent, finished_at: 'not-a-date' });
    assert.equal(dur, '—', `invalid finished_at must return '—', got "${dur}"`);
});

test('shortId: truncates long IDs', () => {
    assert.equal(shortId('abc123'), 'abc123');
    assert.equal(shortId('abcdefghijklmno'), 'abcdefgh…');
    assert.equal(shortId(null), '—');
});

test('filterJobs filters correctly', () => {
    const jobs = [
        { id: '1', task_type: 'import', status: 'pending', library_id: 'l1', worker_id: 'w1', document_id: 'd1', created_at: '2026-06-01T00:00:00Z' },
        { id: '2', task_type: 'graph', status: 'done', library_id: 'l2', worker_id: 'w2', document_id: 'd2', created_at: '2026-07-01T00:00:00Z' },
    ];
    assert.equal(filterJobs(jobs, { status: 'pending' }).length, 1);
    assert.equal(filterJobs(jobs, { task_type: 'graph' }).length, 1);
    assert.equal(filterJobs(jobs, { library_id: 'l2' }).length, 1);
    assert.equal(filterJobs(jobs, { dateFrom: '2026-07-01' }).length, 1);
});

test('paginateJobs paginates correctly', () => {
    const jobs = Array.from({ length: 25 }, (_, i) => ({ id: String(i) }));
    const p = paginateJobs(jobs, 1, 10);
    assert.equal(p.items.length, 10);
    assert.equal(p.total, 25);
    assert.equal(p.pageCount, 3);
});

test('libraryName maps library_id to name', () => {
    const libs = [{ id: 'l1', name: '法规库', slug: 'law' }];
    assert.ok(libraryName(libs, 'l1').includes('法规库'));
    assert.ok(libraryName(libs, 'unknown').includes('unknown'));
});

test('任务类型和图谱阶段使用中文展示', () => {
    assert.equal(TASK_TYPE_LABEL.import, '文件导入');
    assert.equal(TASK_TYPE_LABEL.embedding, '向量化');
    assert.equal(TASK_TYPE_LABEL.graph, '知识图谱');
    assert.equal(STAGE_LABEL.extracting, '抽取实体与关系');
    assert.equal(STAGE_LABEL.awaiting_graph, '等待创建图谱任务');
});

// ── Template regression ──
const src = readFileSync(new URL('./src/views/Jobs.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const uiSrc = readFileSync(new URL('./src/jobs_ui.js', import.meta.url), 'utf8');

test('No inline style attributes in template', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.equal((tpl.match(/\sstyle="/g) || []).length, 0, 'zero inline style');
});

test('CSS defines jobs-* classes', () => {
    for (const c of ['jobs-workspace', 'jobs-stats', 'jobs-toolbar', 'jobs-table-card']) {
        assert.ok(css.includes(c), `${c} in CSS`);
    }
});

test('默认每页 10 条', () => {
    assert.ok(src.includes('ref(10)') || src.includes('= 10'), 'default pageSize 10');
});

test('四项统计为独立卡片', () => {
    assert.ok(src.includes('jobs-stat-body'), 'jobs-stat-body for horizontal layout');
});

test('筛选控件有顶部标签', () => {
    assert.ok(src.includes('jobs-toolbar-label'), 'toolbar labels');
});

test('表格操作有查看详情和重试', () => {
    assert.ok(src.includes('查看详情'), 'view detail button');
    assert.ok(src.includes('status:retry'), 'retry icon');
});

test('内嵌详情卡而非 el-drawer', () => {
    assert.ok(!src.includes('el-drawer'), 'no el-drawer');
    assert.ok(src.includes('jobs-detail-card'), 'inline detail card');
});

test('选中行高亮并可收起', () => {
    assert.ok(src.includes('jobs-row-selected'), 'row highlight');
    assert.ok(src.includes('closeDetail'), 'closeDetail function');
    assert.ok(src.includes('收起'), 'collapse button');
});

test('jobDuration 优先用 claimed_at', () => {
    assert.ok(uiSrc.includes('claimed_at'), 'claimed_at in duration');
    assert.ok(uiSrc.includes('row.claimed_at || row.created_at'), 'priority order');
});

test('jobDuration 校验非法 finished_at 返回 —', () => {
    assert.ok(uiSrc.includes('Number.isNaN(t)'), 'validates finished_at date');
    assert.ok(uiSrc.includes("return '—'"), 'returns — on invalid finished_at');
});

// ── 新回归：task 3 行为 ──
test('使用 Promise.allSettled 独立加载 jobs 和 stats', () => {
    assert.ok(src.includes('Promise.allSettled'), 'must use Promise.allSettled');
});

test('stats 失败不阻断任务列表（sr.status handled independently）', () => {
    assert.ok(src.includes("sr.status === 'fulfilled'"), 'stats result handled independently');
    // jr (jobs) failure is separate — must check jr first
    assert.ok(src.includes("jr.status === 'fulfilled'"), 'jobs result handled independently');
});

test('刷新按钮同时重试知识库列表', () => {
    assert.ok(src.includes('refreshAll'), 'refreshAll function');
    assert.ok(src.includes('loadLibs'), 'loadLibs called on refresh');
});

test('每次 load 后刷新 selectedJob 引用', () => {
    // After jobs loaded: find fresh reference
    assert.ok(src.includes("selectedJob.value = fresh"), 'refresh selectedJob on reload');
});

test('selectedJob 不存在时关闭详情', () => {
    assert.ok(src.includes('closeDetail()'), 'close detail if job gone');
});

// ── statsFailed 回归 ──
test('statsFailed flag 存在并在 load 中管理', () => {
    assert.ok(src.includes('statsFailed'), 'statsFailed ref exists');
    assert.ok(src.includes('statsFailed.value = false'), 'statsFailed cleared on success');
    assert.ok(src.includes('statsFailed.value = true'), 'statsFailed set on failure');
});

test('statsFailed 时统计卡显示 —', () => {
    assert.ok(src.includes("statsFailed ? '—'"), 'stat cards show — when statsFailed');
});

test('statsFailed 时全局重置仍可用（不依赖不可靠数量）', () => {
    // Button disabled excludes statsFailed
    assert.ok(src.includes('!statsFailed'), 'button disabled allows statsFailed case');
    // Confirm branches on statsFailed
    assert.ok(src.includes('statsFailed.value'), 'resetFailed checks statsFailed');
    assert.ok(src.includes('将重置全部失败向量任务'), 'confirm text without unreliable count');
});

// ── 新回归：task 2 重置范围 ──
test('重置失败任务：未选知识库使用 retryable_failed 精确数量', () => {
    assert.ok(src.includes('stats.value.retryable_failed'), 'global uses retryable_failed');
});

test('重置失败任务：明确仅处理向量任务', () => {
    assert.ok(src.includes('重置该知识库中的所有失败向量任务'), 'scoped confirm text');
});

test('模板中无 filtered.filter 调用', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(!tpl.includes('filtered.filter'), 'no filtered.filter in template');
});

test('按钮禁用仅依赖 retryable_failed/statsFailed，不依赖其他筛选条件', () => {
    assert.ok(src.includes('!statsFailed && !stats.retryable_failed'), 'disabled only on global + stats available + no failures');
});

test('按钮数量标签仅未选知识库且 stats 可用时显示', () => {
    assert.ok(src.includes('!filters.library_id && !statsFailed && stats.retryable_failed'), 'count badge only when global and stats not failed');
});

test('监控页使用持久化统一接口并自动刷新', () => {
    assert.ok(src.includes('listMonitoredTasks'), 'uses persistent monitor endpoint');
    assert.ok(src.includes('monitoredTaskStats'), 'uses monitor stats endpoint');
    assert.ok(src.includes('setInterval'), 'auto refreshes');
    assert.ok(src.includes('clearInterval'), 'cleans up timer');
});

test('只有后端标记为 retryable 的任务可重试', () => {
    assert.ok(src.includes(':disabled="!row.retryable"'));
});

// ── 详情交互回归 ──
test('引入 nextTick 和 detailCardRef', () => {
    assert.ok(src.includes('nextTick'), 'imports nextTick');
    assert.ok(src.includes('detailCardRef'), 'has detailCardRef');
});

test('openDetail 打开后 nextTick + scrollIntoView', () => {
    assert.ok(src.includes('nextTick()'), 'calls nextTick');
    assert.ok(src.includes('scrollIntoView'), 'calls scrollIntoView');
});

test('详情卡绑定 ref="detailCardRef"', () => {
    assert.ok(src.includes('ref="detailCardRef"'), 'detail card has ref');
});

test('动态按钮文案：选中行“收起详情”，其他行“查看详情”', () => {
    assert.ok(src.includes('收起详情'), 'collapse text');
    assert.ok(src.includes('查看详情'), 'expand text');
    assert.ok(src.includes("isSelected(row) ? '收起详情' : '查看详情'"), 'dynamic button label');
});

test(':aria-expanded 反映展开状态', () => {
    assert.ok(src.includes(':aria-expanded'), 'aria-expanded attribute');
    assert.ok(src.includes("isSelected(row) ? 'true' : 'false'"), 'aria-expanded toggles on isSelected');
});

test('详情卡标题含文档短 ID', () => {
    assert.ok(src.includes('任务详情 ·'), 'detail title includes separator');
    assert.ok(src.includes('shortId(selectedJob.document_id)'), 'detail title shows doc shortId');
});

test('CSS 穿透 Element Plus td 高亮', () => {
    assert.ok(css.includes('tr.jobs-row-selected > td.el-table__cell'), 'CSS targets td.el-table__cell');
});

test('graph task details separate extraction progress from automatic publication', () => {
    for (const token of [
        '构建模式', '抽取进度', '批次进度', '自动发布',
        '等待抽取完成', '自动发布中', '已发布可用',
        '自动发布失败，可重试', '无合格事实，无需发布',
    ]) assert.ok(src.includes(token), `missing graph state token: ${token}`);
    assert.ok(!src.includes('图谱完成不代表已发布'));
});

test('graph task details expose throughput and rate-limit metrics', () => {
    for (const token of [
        'configured_concurrency', 'effective_concurrency', 'in_flight',
        'eta_seconds', 'cache_hits', 'throttled_count', 'retry_count',
        'completed_batches', 'planned_batches',
    ]) assert.ok(src.includes(token), `missing graph metric: ${token}`);
});

console.log('jobs redesign test passed');
