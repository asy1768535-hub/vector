import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { actorDisplay, targetTypeMeta, targetDisplay, targetTypeKey, actionTone } from './src/admin_activity_ui.js';

const src = readFileSync(new URL('./src/views/Audit.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── targetTypeMeta / targetDisplay 行为测试 ──
test('targetTypeMeta: library → 知识库 + icon', () => {
    const m = targetTypeMeta('library.create');
    assert.equal(m.type, '知识库');
    assert.equal(m.icon, 'sidebar:library');
});
test('targetTypeMeta: user → 用户', () => {
    assert.equal(targetTypeMeta('user.create').type, '用户');
});
test('targetTypeMeta: unknown → 其他', () => {
    assert.equal(targetTypeMeta('unknown.action').type, '其他');
});
test('targetTypeMeta: null → 其他', () => {
    assert.equal(targetTypeMeta(null).type, '其他');
});

test('targetDisplay: extracts name/email/slug priority', () => {
    assert.equal(targetDisplay('a', { name: 'Alice' }), 'Alice');
    assert.equal(targetDisplay('a', { email: 'a@b.com' }), 'a@b.com');
    assert.equal(targetDisplay('a', { slug: 'lib1' }), 'lib1');
    assert.equal(targetDisplay('a', { user_id: 'u1' }), 'u1');
    assert.equal(targetDisplay('a', { document_id: 'd1' }), 'd1');
    assert.equal(targetDisplay('a', null), '');
});

test('actor and UUID projections prefer readable values and shorten unresolved IDs', () => {
    const id = '12345678-1234-1234-1234-123456789abc';
    assert.equal(actorDisplay({ actor_user_id: id }, [{ id, email: 'alice@example.com' }]), 'alice@example.com');
    assert.equal(actorDisplay({ actor_user_id: id }), '12345678…9abc');
    assert.equal(targetDisplay('job.retry', { job_id: id }), '12345678…9abc');
});

test('targetTypeKey: extracts prefix for filtering', () => {
    assert.equal(targetTypeKey('library.create'), 'library');
    assert.equal(targetTypeKey('user.update'), 'user');
    assert.equal(targetTypeKey('permission.grant'), 'permission');
    assert.equal(targetTypeKey('zzz'), 'other');
});

test('actionTone: create/grant → green', () => {
    assert.equal(actionTone('library.create'), 'action-tone--create');
    assert.equal(actionTone('permission.grant'), 'action-tone--create');
});
test('actionTone: update/retry → blue', () => {
    assert.equal(actionTone('library.update'), 'action-tone--update');
    assert.equal(actionTone('job.retry'), 'action-tone--update');
});
test('actionTone: delete/revoke/disable → red', () => {
    assert.equal(actionTone('library.delete'), 'action-tone--delete');
    assert.equal(actionTone('permission.revoke'), 'action-tone--delete');
    assert.equal(actionTone('user.disable'), 'action-tone--delete');
});
test('actionTone: other → grey-blue', () => {
    assert.equal(actionTone('unknown.action'), 'action-tone--other');
    assert.equal(actionTone(null), 'action-tone--other');
});

// ── 模板回归 ──
test('标题 sidebar:audit 图标', () => {
    assert.ok(src.includes('sidebar:audit'), 'sidebar:audit icon');
});
test('使用 datetimerange 替代两个 date picker', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('datetimerange'), 'single datetimerange');
});
test('目标类型筛选', () => {
    assert.ok(src.includes('targetType'), 'targetType filter');
});
test('目标列使用 local-icon + targetDisplay', () => {
    assert.ok(src.includes('audit-target-icon'), 'target icon');
    assert.ok(src.includes('targetDisplay('), 'targetDisplay call');
});
test('详情区使用 logs-detail-grid 四栏', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('logs-detail-grid'), 'detail grid');
    assert.ok(tpl.includes('logs-detail-cell--full'), 'full-width cell');
});

test('主列表使用可读投影，原始数据保留在技术详情', () => {
    assert.ok(src.includes('actorDisplay(row, users, store.user)'));
    assert.ok(src.includes('技术详情'));
    assert.ok(src.includes('prettyTarget(row.target)'));
});
test('受控展开保留 row-key=id', () => {
    assert.ok(src.includes('row-key="id"'), 'row-key id');
    assert.ok(src.includes('expandRowKeys'), 'expandRowKeys');
});
test('零内联 style', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.equal((tpl.match(/\sstyle="/g) || []).length, 0);
});
test('模板使用 actionTone 和 action-tone CSS 类', () => {
    const tpl = src.slice(src.indexOf('template:'));
    assert.ok(tpl.includes('actionTone('), 'template calls actionTone');
    assert.ok(tpl.includes('action-tone'), 'template uses action-tone class');
});

test('CSS 定义 action-tone 四个变体', () => {
    for (const v of ['create', 'update', 'delete', 'other']) {
        assert.ok(css.includes(`action-tone--${v}`), `CSS: action-tone--${v}`);
    }
});

test('CSS: detail grid 1199px 2列 899px 1列', () => {
    const mq1199 = css.slice(css.indexOf('1199px'));
    assert.ok(mq1199.includes('grid-template-columns:repeat(2,'), 'detail 2col at 1199px');
    const mq899 = css.slice(css.indexOf('899px'));
    assert.ok(mq899.includes('grid-template-columns:1fr'), 'detail 1col at 899px');
});

console.log('audit redesign test passed');
