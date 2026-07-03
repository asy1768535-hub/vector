// 统一 API 错误 → 用户可理解的中文提示（纯函数，便于单测）。
//
// 覆盖场景：
//   - detail 字符串（后端多数错误）
//   - FastAPI 422 detail 数组 [{loc,msg,type}]
//   - 普通 JSON 对象（无 detail 字段）
//   - 非 JSON 响应（HTML/纯文本）
//   - 网络错误
//
// 核心约定：任何路径都不得把 [object Object]、英文错误码、堆栈信息原样抛给用户。

/**
 * @param {*} body    响应体（已解析的 JSON 对象、字符串、或 null）
 * @param {number} status  HTTP 状态码
 * @param {string} fallback 无可识别 detail 时的兜底文案
 * @returns {string} 面向用户的中文提示
 */
export function humanizeApiError(body, status = 0, fallback = '') {
    // 1) FastAPI 422 数组 detail
    if (status === 422 && Array.isArray(body && body.detail)) {
        return format422(body.detail);
    }

    // 2) 字符串 detail
    const detail = body && typeof body.detail === 'string' ? body.detail : '';

    // 3) 对象 detail（非标准响应）→ 尝试 message/error 字段
    if (!detail && body && typeof body === 'object') {
        const msg = body.message || body.error || body.msg || '';
        if (typeof msg === 'string' && msg.trim()) {
            // 安全中文 → 透传；否则给通用文案
            return /[一-鿿]/.test(msg) ? msg : (fallback || '操作失败，请稍后重试');
        }
    }

    // 4) detail 是纯英文/错误码/空 → 回退
    if (!detail || !/[一-鿿]/.test(detail)) {
        return fallback || '操作失败，请稍后重试';
    }

    // 5) 安全中文 detail → 直接使用
    return detail;
}

const FIELD_CN = {
    email: '邮箱',
    password: '密码',
    username: '用户名',
    display_name: '显示名',
    is_superuser: '超管标记',
    is_active: '启用标记',
};

// FastAPI 常见英文校验消息 → 中文
const MSG_CN_PATTERNS = [
    [/not a valid email/i, '格式不正确，请输入合法邮箱'],
    [/ensure.*at least (\d+) character/i, (_, n) => `长度不够，至少需要 ${n} 个字符`],
    [/ensure.*at most (\d+) character/i, (_, n) => `超出长度限制，最多 ${n} 个字符`],
    [/value is not a valid integer/i, '请输入整数'],
    [/value is not a valid number/i, '请输入数字'],
    [/field required/i, '此项为必填'],
    [/string type/i, '请输入文本'],
];

function _translateMsg(msg) {
    if (!msg) return '格式不正确';
    // 已经是中文就直接用
    if (/[一-鿿]/.test(msg)) return msg;
    for (const [pat, repl] of MSG_CN_PATTERNS) {
        const m = msg.match(pat);
        if (m) {
            if (typeof repl === 'function') return repl(...m);
            return repl;
        }
    }
    // 无匹配的英文 → 安全中文回退，不直接展示英文码
    return '格式不符合要求，请检查后重试';
}

/**
 * 422 FastAPI detail 数组 → 按字段拼中文提示
 * 例：[{loc:['body','email'],msg:'value is not a valid email'}]
 *   → "邮箱：value is not a valid email"
 */
function format422(errors) {
    const parts = [];
    for (const e of errors) {
        const loc = (e && e.loc) || [];
        const field = loc[loc.length - 1] || '';
        const cnField = FIELD_CN[field] || field || '字段';
        const msg = _translateMsg((e && e.msg) || '');
        parts.push(`${cnField}：${msg}`);
    }
    return parts.join('；') || '请求参数不符合要求，请检查后重试';
}

/**
 * 合并 humanizeError（导入类 error）+ humanizeApiError → 按 status 优先匹配，
 * 用作 api.js request() 等统一错误出口。
 *
 * status → 中文映射表（按需扩展）。未在表中的 status 走 humanizeApiError。
 */
const STATUS_MESSAGES = {
    400: '请求参数不正确，请检查输入',
    401: '登录已过期，请重新登录',
    403: '你没有执行此操作的权限',
    404: '请求的资源不存在',
    409: null,  // 409 语义复杂，由调用方根据 detail 自行细分
    413: '上传文件超过大小限制',
    415: '不支持的文件类型',
    422: null,  // 交给 humanizeApiError 的数组处理
    429: '请求过于频繁，请稍后重试',
    500: '服务器内部错误，请稍后重试',
    502: '服务暂不可用，请稍后重试',
    503: '服务暂不可用，请稍后重试',
};

export function humanizeFetchError(err) {
    if (!err) return '发生未知错误';

    // 如果已经是 humanizeError 处理过的（有 message 且是中文），优先用
    const msg = err.message || '';
    const status = err.status || 0;

    // status 409/422 需特殊处理，不在此处覆盖
    if (status === 409 || status === 422) {
        // 尝试解析 body
        return humanizeApiError(err.body, status, '请求发生冲突，请刷新后重试');
    }

    const statusMsg = STATUS_MESSAGES[status];
    if (statusMsg) return statusMsg;

    // 其他非 2xx：安全中文 detail 优先，否则通用
    return humanizeApiError(err.body, status, `请求失败（HTTP ${status}），请稍后重试`);
}
