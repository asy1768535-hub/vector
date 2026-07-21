// 上传/导入错误 → 用户能理解的中文提示（纯函数，便于单测）。
// 入参 e：{ status?: number, message?: string }，message 为后端 detail 文案。
// 约定：
//   - 409 是「冲突」（内容重复 / 外部系统管理库 / 其它冲突），不是「重建」；重建是 503。
//   - 任何分支都不得把英文码、数组 detail（[object Object]）原样抛给用户。
export function humanizeError(e) {
    const detail = e && typeof e.message === 'string' ? e.message : '';
    const status = e && e.status;

    if (status === 409) {
        if (detail.includes('相同内容已存在')) return '相同内容已存在，请选择其他文件';
        if (detail.includes('external') || detail.includes('外部系统管理')) {
            return '该知识库由外部系统管理，不能在本系统上传';
        }
        // 其它 409：优先显示安全的后端中文 detail；否则给通用冲突文案。
        if (/[一-鿿]/.test(detail)) return detail;
        return '上传发生冲突，请刷新后重试';
    }

    switch (status) {
        case 400: return '文件内容无法解析或不符合格式';
        case 403: return '你没有该知识库的上传权限';
        case 413: return '文件超过上传大小限制';
        case 415: return '暂不支持该文件类型';
        case 422: return '文件或参数不符合要求，请检查后重试';
        case 503: return '服务暂不可用，请稍后重试';
        // 兜底：英文/数组等不安全 detail 不外显，统一通用文案。
        default:  return /[一-鿿]/.test(detail) ? detail : '上传失败，请稍后重试';
    }
}
