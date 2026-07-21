import assert from 'node:assert/strict';
import test from 'node:test';
import { paginate, inDateRange, csvEscape, filterAuditLogs, filterChatLogs } from './src/logs_ui.js';

test('paginate basics', () => {
    const p = paginate(Array.from({ length: 25 }, (_, i) => i), 1, 10);
    assert.equal(p.items.length, 10); assert.equal(p.total, 25); assert.equal(p.pageCount, 3);
});

test('csvEscape: formula injection', () => {
    for (const v of ['=SUM(A1)', '+trigger', '-cmd', '@ref'])
        assert.ok(csvEscape(v).startsWith("'"));
});

// ── filterAuditLogs: 统一时间范围 + targetType ──
test('filterAuditLogs: 统一 datetimerange 过滤', () => {
    const logs = [
        { at: '2026-06-01T00:00:00Z', id: '1', action: 'a', target: {} },
        { at: '2026-07-15T00:00:00Z', id: '2', action: 'b', target: {} },
    ];
    const r = filterAuditLogs(logs, { range: ['2026-07-01T00:00:00', '2026-07-31T00:00:00'] });
    assert.equal(r.length, 1);
    assert.equal(r[0].id, '2');
});

test('filterAuditLogs: targetType 筛选', () => {
    const logs = [
        { at: 'x', id: '1', action: 'library.create', target: { slug: 'a' } },
        { at: 'y', id: '2', action: 'user.create', target: { email: 'b' } },
        { at: 'z', id: '3', action: 'permission.grant', target: {} },
    ];
    assert.equal(filterAuditLogs(logs, { targetType: 'library' }).length, 1);
    assert.equal(filterAuditLogs(logs, { targetType: 'user' })[0].id, '2');
    assert.equal(filterAuditLogs(logs, { targetType: 'permission' }).length, 1);
});

test('filterAuditLogs: keyword searches target JSON', () => {
    const logs = [{ at: 'x', id: '1', action: 'a', target: { slug: 'medical' } }];
    assert.equal(filterAuditLogs(logs, { keyword: 'medical' }).length, 1);
    assert.equal(filterAuditLogs(logs, { keyword: 'none' }).length, 0);
});

// ── filterChatLogs: 组合筛选 ──
test('filterChatLogs: keyword + rewrite + hasSources 组合', () => {
    const rows = [
        { question: '高血压', rewritten_query: '高血压治疗', sources: [{ title: 'a' }] },
        { question: '感冒', rewritten_query: null, sources: [] },
        { question: '高血压药物', rewritten_query: null, sources: [{ title: 'b' }] },
    ];
    // 仅关键词
    assert.equal(filterChatLogs(rows, { keyword: '高血压' }).length, 2);
    // 关键词 + 已改写
    assert.equal(filterChatLogs(rows, { keyword: '高血压', rewrite: 'yes' }).length, 1);
    // 关键词 + 有引用
    assert.equal(filterChatLogs(rows, { keyword: '高血压', hasSources: 'yes' }).length, 2);
    // 全部组合
    assert.equal(filterChatLogs(rows, { keyword: '高血压', rewrite: 'yes', hasSources: 'yes' }).length, 1);
    // 仅改写筛选
    assert.equal(filterChatLogs(rows, { rewrite: 'no' }).length, 2);
    // 仅引用筛选
    assert.equal(filterChatLogs(rows, { hasSources: 'yes' }).length, 2);
    // 无筛选 = 全部
    assert.equal(filterChatLogs(rows, {}).length, 3);
});

console.log('logs_ui test passed');
