export function buildFolderTree(folders, documents = []) {
    const counts = new Map();
    for (const document of documents || []) {
        const key = document.folder_id ? String(document.folder_id) : 'root';
        counts.set(key, (counts.get(key) || 0) + 1);
    }
    const nodes = new Map(
        (folders || []).map((folder) => [
            String(folder.id),
            {
                id: String(folder.id),
                parentId: folder.parent_id ? String(folder.parent_id) : null,
                label: folder.name,
                path: folder.path,
                count: counts.get(String(folder.id)) || 0,
                children: [],
            },
        ]),
    );
    const roots = [];
    for (const node of nodes.values()) {
        const parent = node.parentId ? nodes.get(node.parentId) : null;
        if (parent) parent.children.push(node);
        else roots.push(node);
    }
    const sortNodes = (items) => {
        items.sort((left, right) => left.path.localeCompare(right.path, 'zh-CN'));
        for (const item of items) sortNodes(item.children);
    };
    sortNodes(roots);
    return roots;
}

export function documentsInFolder(documents, selectedFolderId) {
    if (selectedFolderId === 'all') return documents || [];
    if (selectedFolderId === 'root') {
        return (documents || []).filter((document) => !document.folder_id);
    }
    return (documents || []).filter(
        (document) => String(document.folder_id || '') === String(selectedFolderId),
    );
}
