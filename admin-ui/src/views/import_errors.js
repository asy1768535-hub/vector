// 上传/导入错误 → 用户能理解的中文提示（纯函数，便于单测）。
// 入参 e：{ status?: number, message?: string }，message 为后端 detail 文案。
// 约定：
//   - 409 是「冲突」（内容重复 / 外部系统管理库 / 其它冲突），不是「重建」；重建是 503。
//   - 任何分支都不得把英文码、数组 detail（[object Object]）原样抛给用户。
function humanizeErrorReason(e) {
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
        case 415: {
            const responseDetail = e?.body?.detail;
            switch (responseDetail?.code) {
                case 'metadata_file':
                    return 'macOS 元数据文件，已忽略';
                case 'office_lock_file':
                    return 'Office 临时锁文件，已忽略';
                case 'encrypted_office_file':
                    return '检测到加密的 Office 文件，请在本地解密后重新上传';
                case 'file_signature_mismatch': {
                    const suggestedExtension = responseDetail.suggested_extension;
                    if (['.doc', '.docx', '.xls', '.xlsx', '.pptx'].includes(suggestedExtension)) {
                        return `文件后缀与实际格式不一致，请改为 ${suggestedExtension} 后重新上传`;
                    }
                    return '文件后缀与实际格式不一致，请检查后重新上传';
                }
                case 'file_signature_unconfirmed':
                    return '无法确认文件的真实格式，请检查文件后重新上传';
                default:
                    return '暂不支持该文件类型';
            }
        }
        case 422: {
            const code = e?.body?.detail?.code;
            if (code === 'relative_path_mismatch') {
                return '所选文件夹路径与文件名不一致，请重新选择原始文件夹';
            }
            if (code === 'invalid_relative_path') {
                return '所选文件夹路径无效，请重新选择原始文件夹';
            }
            const validation = Array.isArray(e?.body?.detail)
                ? e.body.detail[0]
                : null;
            const field = Array.isArray(validation?.loc)
                ? validation.loc.at(-1)
                : null;
            if (field === 'batch_id') {
                return '上传批次已失效，请刷新页面后重新选择文件';
            }
            if (field === 'file_name') {
                return '文件名为空或过长，请重命名后重试';
            }
            if (field === 'relative_path') {
                return '文件夹路径无效或过长，请缩短目录层级后重试';
            }
            if (field === 'size_bytes') {
                return '文件大小参数无效，请重新选择文件后重试';
            }
            if (field === 'last_modified_millis') {
                return '文件修改时间参数无效，请重新选择文件后重试';
            }
            // api.js has already translated FastAPI validation errors into a
            // safe Chinese field message.  Preserve that information for
            // fields added by a newer API instead of hiding it behind the
            // generic 422 fallback.
            if (/[一-鿿]/.test(detail)) return detail;
            return '文件或参数不符合要求，请检查后重试';
        }
        case 503: return '服务暂不可用，请稍后重试';
        // 兜底：英文/数组等不安全 detail 不外显，统一通用文案。
        default:  return /[一-鿿]/.test(detail) ? detail : '上传失败，请稍后重试';
    }
}

export function humanizeError(e) {
    const reason = humanizeErrorReason(e);
    const selectedPath = typeof e?.uploadRelativePath === 'string' && e.uploadRelativePath.trim()
        ? e.uploadRelativePath.trim()
        : (typeof e?.uploadFileName === 'string' ? e.uploadFileName.trim() : '');
    return selectedPath ? `${selectedPath}：${reason}` : reason;
}
