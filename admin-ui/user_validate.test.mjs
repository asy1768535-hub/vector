// 用户创建前端校验单测 — 直接导入生产代码 validateCreate
// 运行：node admin-ui/user_validate.test.mjs
import assert from 'node:assert/strict';
import { validateCreate } from './src/validate.js';

let passed = 0;

function ok(name, cond) {
    assert.ok(cond, `[FAIL] ${name}`);
    console.log(`  ok  ${name}`);
    passed++;
}

function deepEq(name, got, expected) {
    assert.deepEqual(got, expected, `[FAIL] ${name}: 期望「${JSON.stringify(expected)}」实得「${JSON.stringify(got)}」`);
    console.log(`  ok  ${name}`);
    passed++;
}

// ═══════════════════════════════════════════════════════════════
// validateCreate（生产代码）
// ═══════════════════════════════════════════════════════════════

// 合法输入：无错误
deepEq('合法全部填写 → 无错误', validateCreate({
    email: 'alice@example.com', password: '12345678', username: 'alice', display_name: 'Alice',
}), []);

// 邮箱校验
const badEmail = validateCreate({ email: 'not-an-email', password: '12345678' });
ok('非法邮箱 → 有中文提示', badEmail.some((e) => e.includes('邮箱')));

const emptyEmail = validateCreate({ email: '', password: '12345678' });
ok('空邮箱 → 有中文提示', emptyEmail.some((e) => e.includes('邮箱')));

// 密码校验
const shortPwd = validateCreate({ email: 'a@b.com', password: '123' });
ok('密码过短 → 有中文提示', shortPwd.some((e) => e.includes('密码') && e.includes('8')));

const longPwd = validateCreate({ email: 'a@b.com', password: 'x'.repeat(129) });
ok('密码过长 → 有中文提示', longPwd.some((e) => e.includes('密码') && e.includes('128')));

// 边界
deepEq('密码刚好 8 位', validateCreate({ email: 'a@b.com', password: '12345678' }), []);
deepEq('密码刚好 128 位', validateCreate({ email: 'a@b.com', password: 'x'.repeat(128) }), []);

// 用户名
const longUser = validateCreate({ email: 'a@b.com', password: '12345678', username: 'x'.repeat(65) });
ok('用户名过长', longUser.some((e) => e.includes('用户名') && e.includes('64')));

// 显示名
const longDisp = validateCreate({ email: 'a@b.com', password: '12345678', display_name: 'x'.repeat(129) });
ok('显示名过长', longDisp.some((e) => e.includes('显示名') && e.includes('128')));

// 可选字段允许为空
deepEq('空用户名 → 允许', validateCreate({ email: 'a@b.com', password: '12345678', username: '' }), []);
deepEq('空显示名 → 允许', validateCreate({ email: 'a@b.com', password: '12345678', display_name: '' }), []);

// 多错误
const multi = validateCreate({ email: 'bad', password: '12' });
ok('多错误 → 含邮箱', multi.some((e) => e.includes('邮箱')));
ok('多错误 → 含密码', multi.some((e) => e.includes('密码')));

// 通用守卫：所有错误信息都是中文
for (const tc of [
    validateCreate({ email: 'bad', password: '' }),
    validateCreate({ email: '', password: '12' }),
    validateCreate({ email: 'a@b.com', password: '12345678', username: 'x'.repeat(100) }),
]) {
    for (const msg of tc) {
        ok(`错误消息是中文: "${msg}"`, /[一-鿿]/.test(msg));
    }
}

console.log(`\n${passed} tests passed`);
