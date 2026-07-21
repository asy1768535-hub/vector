// 用户创建前端预校验（纯函数，可被 Users.js 和测试直接导入）
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateCreate(form) {
    const errs = [];
    if (!form.email || !EMAIL_RE.test(form.email.trim())) {
        errs.push('请输入合法完整的邮箱地址');
    }
    const pwd = form.password || '';
    if (pwd.length < 8 || pwd.length > 128) {
        errs.push('密码长度需为 8～128 位');
    }
    if (form.username && form.username.length > 64) {
        errs.push('用户名最多 64 个字符');
    }
    if (form.display_name && form.display_name.length > 128) {
        errs.push('显示名最多 128 个字符');
    }
    return errs;
}
