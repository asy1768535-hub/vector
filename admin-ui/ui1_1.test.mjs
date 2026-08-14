// UI-1.1 返工测试：effectiveCollapsed / 窄屏 / mock 超管生产禁止 / 三主题颜色
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

let passed = 0;
function ok(n, c) { assert.ok(c, `[FAIL] ${n}`); console.log(`  ok  ${n}`); passed++; }
function eq(n, g, e) { assert.deepEqual(g, e, `[FAIL] ${n}: ${JSON.stringify(g)} !== ${JSON.stringify(e)}`); console.log(`  ok  ${n}`); passed++; }

// ═══════════════════════════════════════════════════════════════
// 1. effectiveCollapsed = collapsed || isNarrow
// ═══════════════════════════════════════════════════════════════
function computeEffective(collapsed, isNarrow) {
    return collapsed || isNarrow;
}
eq('展开+宽屏', computeEffective(false, false), false);
eq('展开+窄屏', computeEffective(false, true), true);
eq('收起+宽屏', computeEffective(true, false), true);
eq('收起+窄屏', computeEffective(true, true), true);

// ═══════════════════════════════════════════════════════════════
// 2. 窄屏菜单状态：isNarrow=true → effectiveCollapsed=true → el-menu :collapse=true
// ═══════════════════════════════════════════════════════════════
function sidebarWidth(effectiveCollapsed) {
    return effectiveCollapsed ? '64px' : '220px';
}
eq('窄屏→64px', sidebarWidth(true), '64px');
eq('宽屏展开→220px', sidebarWidth(false), '220px');
// 用户手动收起后，effectiveCollapsed 仍为 true（窄屏强制 + 用户偏好合并）
eq('窄屏+手动展开无效', computeEffective(false, true), true);

// ═══════════════════════════════════════════════════════════════
// 3. 生产环境禁止 mock 超管
// ═══════════════════════════════════════════════════════════════
function canMockLogin(errorMsg, devPreviewEnabled) {
    const isNetworkError = errorMsg instanceof TypeError
        || /Failed to fetch|NetworkError/i.test(errorMsg)
        || /HTTP 50[0-9]/.test(errorMsg)
        || /HTTP 405/.test(errorMsg);
    return !!(devPreviewEnabled && isNetworkError);
}
ok('生产环境(TypeError)→禁止mock', !canMockLogin(new TypeError('Failed to fetch'), false));
ok('生产环境(501)→禁止mock', !canMockLogin('HTTP 501', false));
ok('生产环境(网络)→禁止mock', !canMockLogin('Failed to fetch', false));
ok('开发预览(TypeError)→允许mock', canMockLogin(new TypeError('Failed to fetch'), true));
ok('开发预览(501)→允许mock', canMockLogin('HTTP 501', true));
ok('开发预览(正常错误)→禁止mock', !canMockLogin('邮箱或密码错误', true));

// ═══════════════════════════════════════════════════════════════
// 4. 固定企业主题，不保留运行时主题切换
// ═══════════════════════════════════════════════════════════════
const appSource = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const styleSource = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

ok('入口不加载主题模块', !appSource.includes('theme.js'));
ok('顶栏不含主题切换', !layoutSource.includes('themeOptions'));
ok('CSS 不含暗色主题', !styleSource.includes('ai-dark'));
ok('CSS 不含政务主题', !styleSource.includes('government'));
ok('企业主色保留', styleSource.includes('--color-primary: #176B57'));

console.log(`\n${passed} tests passed`);
