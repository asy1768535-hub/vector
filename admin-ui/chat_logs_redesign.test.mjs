import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { filterChatLogs } from './src/logs_ui.js';

const src = readFileSync(new URL('./src/views/ChatLogs.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── 行为测试 ──
test('filterChatLogs: 改写 + 引用组合筛选', () => {
    const rows = [
        { question: 'a', rewritten_query: 'ra', sources: [{ title: 'x' }] },
        { question: 'b', rewritten_query: null, sources: [] },
        { question: 'c', rewritten_query: 'rc', sources: [] },
        { question: 'd', rewritten_query: null, sources: [{ title: 'y' }] },
    ];
    assert.equal(filterChatLogs(rows, { rewrite: 'yes' }).length, 2);
    assert.equal(filterChatLogs(rows, { rewrite: 'no' }).length, 2);
    assert.equal(filterChatLogs(rows, { hasSources: 'yes' }).length, 2);
    assert.equal(filterChatLogs(rows, { hasSources: 'no' }).length, 2);
    assert.equal(filterChatLogs(rows, { rewrite: 'yes', hasSources: 'yes' }).length, 1);
    assert.equal(filterChatLogs(rows, {}).length, 4);
});

// ── 模板回归 ──
test('标题 sidebar:qa-log 图标', () => {
    assert.ok(src.includes('sidebar:qa-log'), 'sidebar:qa-log icon');
});
test('知识库名映射：libDisplay 显示名称，下方 slug', () => {
    assert.ok(src.includes('libDisplay('), 'libDisplay function');
    assert.ok(src.includes('chat-logs-lib-slug'), 'slug subtitle class');
});

test('模板使用 libNameMap[row.library_slug] 条件渲染 slug', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('libNameMap[row.library_slug]'), 'template uses libNameMap[row.library_slug]');
});

test('setup return 块暴露 libNameMap', () => {
    // Extract the return block between "return {" and the closing "};"
    const returnStart = src.indexOf('return {');
    const returnEnd = src.indexOf('};', returnStart);
    const returnBlock = src.slice(returnStart, returnEnd + 2);
    assert.ok(returnBlock.includes('libNameMap'), 'return block exposes libNameMap');
    // Must be inside return, not just a computed definition elsewhere
    assert.ok(!returnBlock.includes('computed(()'), 'return block must not contain function body');
});
test('是否改写筛选', () => {
    assert.ok(src.includes("filters.rewrite"), 'rewrite filter');
    assert.ok(src.includes('已改写'), 'yes option');
    assert.ok(src.includes('未改写'), 'no option');
});
test('是否有引用筛选', () => {
    assert.ok(src.includes("filters.hasSources"), 'hasSources filter');
    assert.ok(src.includes('有引用'), 'with sources option');
    assert.ok(src.includes('无引用'), 'without sources option');
});
test('详情四栏：logs-detail-grid', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('logs-detail-grid'), 'detail grid');
    assert.ok(tpl.includes('logs-detail-cell--full'), 'full-width cell');
});
test('回答和错误同时存在时都显示', () => {
    const expandBlock = src.slice(src.indexOf('logs-detail-label">回答'));
    // First row.answer occurrence: v-if="row.answer"
    const answerCtx = expandBlock.slice(expandBlock.indexOf('row.answer') - 20, expandBlock.indexOf('row.answer') + 30);
    assert.ok(answerCtx.includes('v-if="row.answer"'), 'answer is independent v-if');
    // The row.error_message appears twice: first in v-else-if, second in v-if.
    // Verify the second occurrence uses v-if (independent), not v-else-if.
    const firstErr = expandBlock.indexOf('row.error_message');
    const secondErr = expandBlock.indexOf('row.error_message', firstErr + 1);
    assert.ok(secondErr > firstErr, 'row.error_message appears twice in answer section');
    const errorCtx = expandBlock.slice(secondErr - 20, secondErr + 30);
    assert.ok(errorCtx.includes('v-if="row.error_message"'), 'second error use is independent v-if');
});
test('来源显示 document_id', () => {
    assert.ok(src.includes('source.document_id'), 'source document_id');
});

test('引用来源使用紧凑列表，全文进入详情弹窗', () => {
    assert.ok(src.includes('sourceDetail.open'), 'source detail dialog state');
    assert.ok(src.includes('openSourceDetail(s)'), 'source detail action');
    assert.ok(src.includes('查看详情'), 'source detail button text');
    assert.ok(src.includes('logs-source-detail-dialog'), 'source detail dialog class');
    assert.ok(src.includes('sourceMeta(sourceDetail.item)'), 'dialog source metadata');
    assert.doesNotMatch(src, /<div class="logs-source-text">\{\{ s\.content \}\}<\/div>/);
});

test('引用来源样式支持紧凑摘要和详情正文', () => {
    assert.match(css, /\.logs-source-item\s*\{/);
    assert.match(css, /\.logs-source-title\s*\{[^}]*text-overflow:ellipsis/s);
    assert.match(css, /\.logs-source-actions\s*\{/);
    assert.match(css, /\.logs-source-detail-text\s*\{/);
});

test('引用来源摘要只显示文档名和分级相似度', () => {
    const sourceBlock = src.slice(src.indexOf('logs-source-list'), src.indexOf('logs-source-detail-dialog'));
    assert.ok(src.includes('sourceScoreTone(s)'), 'source score tone helper is used');
    assert.ok(src.includes('fmtSourceScore(s)'), 'source score formatter receives source metadata');
    assert.ok(sourceBlock.includes('logs-source-score-pill'), 'score pill class in source card');
    assert.ok(!sourceBlock.includes('sourceMeta(s)'), 'compact card omits document metadata');
    assert.ok(!sourceBlock.includes('document_id'), 'compact card omits document id');
});

test('引用来源列表使用两列紧凑网格和相似度分级', () => {
    assert.match(css, /\.logs-source-list\s*\{[^}]*grid-template-columns:repeat\(2,/s);
    assert.match(css, /\.logs-source-item\s*\{[^}]*min-height:48px/s);
    assert.match(css, /\.logs-source-score-pill--high\s*\{/);
    assert.match(css, /\.logs-source-score-pill--medium\s*\{/);
    assert.match(css, /\.logs-source-score-pill--low\s*\{/);
    assert.match(css, /\.logs-source-score-pill--neutral\s*\{/);
    assert.doesNotMatch(src, /<el-button link type="primary" size="small" @click="openSourceDetail\(s\)">查看详情<\/el-button>/);
});

test('引用来源区域跨两列，避免来源卡片内容挤压重叠', () => {
    assert.ok(src.includes('logs-detail-cell--sources'), 'source detail cell has widening class');
    assert.match(css, /\.logs-detail-cell--sources\s*\{[^}]*grid-column:span 2/s);
});

test('引用来源内部详情使用轻量文字操作，不使用方框按钮', () => {
    assert.ok(src.includes('class="logs-source-detail-link"'), 'source detail link class');
    assert.doesNotMatch(src, /<el-button type="primary" plain size="small" @click="openSourceDetail\(s\)">查看详情<\/el-button>/);
});

test('问答日志行级详情操作使用按钮样式', () => {
    assert.ok(src.includes('class="chat-logs-row-action"'), 'row detail button class');
    assert.doesNotMatch(src, /<el-button size="small" link type="primary" @click\.stop="toggleExpand\(row\)">/);
});

test('问答日志行级操作列宽足够容纳按钮，不出现省略号', () => {
    assert.ok(src.includes('label="操作" width="120"'), 'row action column is wide enough for button');
    assert.match(css, /\.chat-logs-row-action\s*\{[^}]*min-width:82px/s);
});
test('CSV 增加知识库名称、是否改写、引用数量', () => {
    const csvHeaders = src.slice(src.indexOf("exportCSV"), src.indexOf('chat-logs.csv'));
    assert.ok(csvHeaders.includes('知识库名称'), 'csv: lib name');
    assert.ok(csvHeaders.includes('是否改写'), 'csv: rewrite flag');
    assert.ok(csvHeaders.includes('引用数量'), 'csv: source count');
});
test('零内联 style', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.equal((tpl.match(/\sstyle="/g) || []).length, 0);
});
test('CSS 四栏响应式', () => {
    const mq1199 = css.slice(css.indexOf('1199px'));
    assert.ok(mq1199.includes('grid-template-columns:repeat(2,'), 'detail 2col at 1199px');
    const mq899 = css.slice(css.indexOf('899px'));
    assert.ok(mq899.includes('grid-template-columns:1fr'), 'detail 1col at 899px');
});

test('chat-logs-filter-grid 存在于模板', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('chat-logs-filter-grid'), 'filter grid class in template');
});

test('CSS 12列 filter grid 基础布局', () => {
    assert.ok(css.includes('grid-template-columns:repeat(12, minmax(0, 1fr))'), '12-col base grid');
});

test('1199px filter grid → 6列', () => {
    const mq1199 = css.slice(css.indexOf('1199px'));
    assert.ok(mq1199.includes('chat-logs-filter-grid') && mq1199.includes('repeat(6,'), '6-col at 1199px');
});

test('899px filter grid → 1列，操作按钮并排各半', () => {
    const mq899 = css.slice(css.indexOf('899px'));
    assert.ok(mq899.includes('chat-logs-filter-grid') && mq899.includes('grid-template-columns:1fr'), '1-col at 899px');
    assert.ok(mq899.includes('flex:1'), 'buttons split evenly at 899px');
});

test('按钮使用 chat-logs-action-btn 类，最小宽度96px', () => {
    assert.ok(css.includes('chat-logs-action-btn'), 'action-btn class');
    assert.ok(css.includes('min-width:96px'), '96px min-width');
});

test('筛选网格子项允许收缩，避免日期框覆盖关键词输入框', () => {
    assert.match(css, /\.chat-logs-filter-grid\s*>\s*\*\s*\{\s*min-width:\s*0\s*;\s*\}/);
    const rangeRule = css.match(/\.chat-logs-fg-time\s+\.logs-filter-range\s*\{([^}]*)\}/)?.[1] || '';
    assert.match(rangeRule, /max-width:\s*100%/);
    assert.match(rangeRule, /box-sizing:\s*border-box/);
});

test('citation score semantics use display_score and never scale RRF', () => {
    const start = src.indexOf('function clampDisplayScore');
    const end = src.indexOf('const UUID_RE');
    const fmt = Function(`${src.slice(start, end)}; return fmtSourceScore;`)();
    assert.equal(fmt({ score_type: 'rerank', display_score: 0.863 }), '相关度 86.3%');
    assert.equal(fmt({ score_type: 'vector', display_score: 0.724 }), '向量相似度 72.4%');
    assert.equal(fmt({ score_type: 'rrf', display_score: 0.0164 }), '融合排序');
    assert.equal(fmt({ score_type: 'legacy', display_score: 0.9 }), '历史排序');
    assert.equal(fmt({ score_type: 'vector', display_score: 1.5 }), '向量相似度 100.0%');
    assert.equal(fmt({ score_type: 'vector', display_score: -0.1 }), '向量相似度 0.0%');
});

console.log('chat logs redesign test passed');
