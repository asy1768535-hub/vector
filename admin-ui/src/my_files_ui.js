export function myFilesBreadcrumbs(path = '') {
    const parts = String(path || '').split('/').filter(Boolean);
    return [
        { name: '我的任务', path: '' },
        ...parts.map((name, index) => ({
            name,
            path: `/${parts.slice(0, index + 1).join('/')}`,
        })),
    ];
}

export function myFilesExpandedFolderPaths(path = '') {
    return myFilesBreadcrumbs(path).slice(1).map((item) => item.path);
}

export function myFilesAncestorPaths(path = '') {
    return myFilesBreadcrumbs(path).slice(0, -1).map((item) => item.path);
}

export function selectableUploadLibraries(permissionRows = [], adminLibraries = null) {
    const source = Array.isArray(adminLibraries)
        ? adminLibraries
            .filter((library) => library && !library.deleted_at)
            .map((library) => ({
                value: String(library.slug || ''),
                label: String(library.name || library.slug || ''),
            }))
        : (Array.isArray(permissionRows) ? permissionRows : [])
            .filter((permission) => (permission?.actions || []).includes('insert'))
            .map((permission) => ({
                value: String(permission.library_slug || ''),
                label: String(permission.library_name || permission.library_slug || ''),
            }));
    const unique = new Map(source.filter((item) => item.value).map((item) => [item.value, item]));
    return Array.from(unique.values()).sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'));
}

export function myFilesPageCount(folderTotal, fileTotal, pageSize) {
    const size = Number(pageSize) > 0 ? Number(pageSize) : 50;
    return Math.max(1, Math.ceil(((Number(folderTotal) || 0) + (Number(fileTotal) || 0)) / size));
}

