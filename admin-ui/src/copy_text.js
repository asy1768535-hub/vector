// 纯前端复制文本到剪贴板，优先使用 navigator.clipboard，降级到 document.execCommand。
// 不依赖 Vue / Element Plus，UI 提示由调用方处理。

export async function copyTextToClipboard(text) {
    const value = String(text || '').trim();
    if (!value) {
        throw new Error('暂无可复制内容');
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(value);
            return;
        } catch (_) {
            // 继续降级
        }
    }

    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    textarea.style.left = '-9999px';

    let copied = false;
    try {
        document.body.appendChild(textarea);
        textarea.select();
        copied = document.execCommand('copy');
    } catch (_) {
        // execCommand 本身失败，继续走 finally 清理后抛出中文提示
    } finally {
        if (textarea.parentNode) {
            textarea.parentNode.removeChild(textarea);
        }
    }

    if (!copied) {
        throw new Error('复制失败，请手动选择内容');
    }
}
