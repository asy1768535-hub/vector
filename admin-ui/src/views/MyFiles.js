import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { useRoute, useRouter } from 'vue-router';

import * as api from '../api.js';
import { APP_PATHS } from '../domain_navigation.js';
import {
    myFilesBreadcrumbs,
    myFilesPageCount,
    selectableUploadLibraries,
} from '../my_files_ui.js';
import { store } from '../store.js';
import {
    capabilityEntries,
    capabilityStateLabel,
    catalogOverallLabel,
    catalogOverallTag,
} from '../catalog_ui.js';
import { documentStatusLabel, documentStatusTag, documentTypeIcon } from '../documents_ui.js';

const PAGE_SIZE = 50;

function emptyPage() {
    return {
        folders: [],
        files: [],
        folder_total: 0,
        file_total: 0,
    };
}

function formatTime(value) {
    const time = new Date(value || '');
    return Number.isNaN(time.getTime()) ? '—' : time.toLocaleString('zh-CN');
}

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraryOptions = ref([]);
        const libraryLoading = ref(false);
        const selectedLibrarySlug = ref('');
        const selectedPath = ref('');
        const currentPage = ref(1);
        const pageData = ref(emptyPage());
        const rootFolders = ref([]);
        const selectedFileResourceIds = ref(new Set());
        const loaded = ref(false);
        const loading = ref(false);
        const error = ref('');
        let requestSequence = 0;
        let requestController = null;

        const breadcrumbs = computed(() => myFilesBreadcrumbs(selectedPath.value));
        const currentDirectoryName = computed(() => (
            breadcrumbs.value[breadcrumbs.value.length - 1]?.name || '我的文件'
        ));
        const currentTotal = computed(() => (
            Number(pageData.value.folder_total || 0) + Number(pageData.value.file_total || 0)
        ));
        const totalPages = computed(() => myFilesPageCount(
            pageData.value.folder_total,
            pageData.value.file_total,
            PAGE_SIZE,
        ));
        const paginationTotal = computed(() => currentTotal.value);
        const directoryFolders = computed(() => pageData.value.folders);
        const selectedCount = computed(() => selectedFileResourceIds.value.size);

        async function loadLibraries() {
            libraryLoading.value = true;
            try {
                const adminLibraries = store.user?.is_superuser ? await api.listLibraries() : null;
                libraryOptions.value = selectableUploadLibraries(store.permissions, adminLibraries);
            } catch (loadError) {
                ElMessage.error(loadError?.message || '知识库列表加载失败');
                libraryOptions.value = [];
            } finally {
                libraryLoading.value = false;
            }
        }

        async function loadFiles() {
            if (!selectedLibrarySlug.value) {
                pageData.value = emptyPage();
                loaded.value = false;
                error.value = '';
                return;
            }
            if (requestController) requestController.abort();
            requestController = new AbortController();
            const sequence = ++requestSequence;
            loading.value = true;
            loaded.value = false;
            error.value = '';
            pageData.value = emptyPage();
            try {
                const [result, catalog] = await Promise.all([
                    api.listStoredFiles({
                        librarySlug: selectedLibrarySlug.value,
                        path: selectedPath.value,
                        page: currentPage.value,
                        pageSize: PAGE_SIZE,
                    }, { signal: requestController.signal }),
                    api.listCatalogDocuments(selectedLibrarySlug.value, { limit: 50 }).catch(() => null),
                ]);
                if (sequence !== requestSequence) return;
                const personalFolders = Array.isArray(result?.folders) ? result.folders : [];
                const catalogById = new Map(
                    (catalog?.items || []).map((item) => [String(item.document_id), item]),
                );
                pageData.value = {
                    folders: personalFolders,
                    files: (Array.isArray(result?.files) ? result.files : []).map((file) => ({
                        ...file,
                        catalog: catalogById.get(String(file.document_id)) || null,
                    })),
                    folder_total: Number(result?.folder_total || 0),
                    file_total: Number(result?.file_total || 0),
                };
                if (!selectedPath.value && currentPage.value === 1) {
                    rootFolders.value = [...personalFolders];
                }
                loaded.value = true;
            } catch (loadError) {
                if (sequence === requestSequence && loadError?.name !== 'AbortError') {
                    error.value = loadError?.message || '文件列表加载失败，请稍后重试';
                }
            } finally {
                if (sequence === requestSequence) loading.value = false;
            }
        }

        function changeLibrary() {
            selectedPath.value = '';
            currentPage.value = 1;
            rootFolders.value = [];
            selectedFileResourceIds.value = new Set();
            loadFiles();
        }

        function openFolder(folder) {
            selectedPath.value = folder.path;
            currentPage.value = 1;
            loadFiles();
        }

        function openBreadcrumb(item) {
            if (item.path === selectedPath.value) return;
            selectedPath.value = item.path;
            currentPage.value = 1;
            selectedFileResourceIds.value = new Set();
            loadFiles();
        }

        function changePage(page) {
            currentPage.value = page;
            selectedFileResourceIds.value = new Set();
            loadFiles();
        }

        function toggleFile(fileResourceId) {
            const next = new Set(selectedFileResourceIds.value);
            if (next.has(fileResourceId)) next.delete(fileResourceId);
            else next.add(fileResourceId);
            selectedFileResourceIds.value = next;
        }

        function isFileSelected(fileResourceId) {
            return selectedFileResourceIds.value.has(fileResourceId);
        }

        async function deleteFile(file) {
            try {
                const isKnowledgeAsset = Boolean(file.document_id);
                await ElMessageBox.confirm(
                    isKnowledgeAsset
                        ? `删除文件“${file.file_name}”？知识资产和原文件都会删除。`
                        : `删除原文件“${file.file_name}”？删除后无法恢复。`,
                    '确认删除',
                    { type: 'warning' },
                );
                if (isKnowledgeAsset) {
                    await api.deleteDocument(selectedLibrarySlug.value, file.document_id);
                } else {
                    await api.deleteStoredFile(selectedLibrarySlug.value, file.file_resource_id);
                }
                selectedFileResourceIds.value = new Set();
                ElMessage.success(isKnowledgeAsset ? '文件已删除' : '已提交原文件删除');
                await loadFiles();
            } catch (error) {
                if (error !== 'cancel') ElMessage.error(error?.message || '删除文件失败');
            }
        }

        async function downloadFile(file) {
            try {
                const result = await api.getStoredFileDownloadUrl(
                    selectedLibrarySlug.value,
                    file.file_resource_id,
                );
                if (!result?.url) throw new Error('下载链接不可用');
                window.location.assign(result.url);
            } catch (error) {
                ElMessage.error(error?.message || '获取下载链接失败');
            }
        }

        async function deleteSelected() {
            const filesById = new Map(pageData.value.files.map((file) => [file.file_resource_id, file]));
            const files = [...selectedFileResourceIds.value].map((id) => filesById.get(id)).filter(Boolean);
            if (!files.length) return;
            try {
                await ElMessageBox.confirm(`删除选中的 ${files.length} 个文件？`, '确认批量删除', { type: 'warning' });
                const results = await Promise.allSettled(files.map((file) => (
                    file.document_id
                        ? api.deleteDocument(selectedLibrarySlug.value, file.document_id)
                        : api.deleteStoredFile(selectedLibrarySlug.value, file.file_resource_id)
                )));
                const failed = results.filter((item) => item.status === 'rejected').length;
                selectedFileResourceIds.value = new Set();
                await loadFiles();
                if (failed) ElMessage.warning(`${files.length - failed} 个文件已删除，${failed} 个失败`);
                else ElMessage.success('选中文件已删除');
            } catch (error) {
                if (error !== 'cancel') ElMessage.error(error?.message || '批量删除失败');
            }
        }

        async function deleteCurrentFolder() {
            if (!selectedPath.value) return;
            try {
                await ElMessageBox.confirm(
                    `删除文件夹“${currentDirectoryName.value}”及其中所有文件？删除后无法恢复。`,
                    '确认删除文件夹',
                    { type: 'warning' },
                );
                const result = await api.deletePersonalFileFolder(selectedLibrarySlug.value, selectedPath.value);
                selectedPath.value = breadcrumbs.value[breadcrumbs.value.length - 2]?.path || '';
                currentPage.value = 1;
                selectedFileResourceIds.value = new Set();
                ElMessage.success(`已提交删除 ${Number(result?.deleted_count || 0)} 个文件`);
                await loadFiles();
            } catch (error) {
                if (error !== 'cancel') ElMessage.error(error?.message || '删除文件夹失败');
            }
        }

        function openFile(file) {
            if (!file.document_id || !selectedLibrarySlug.value) {
                ElMessage.info('该文件暂时没有可打开的知识资产记录');
                return;
            }
            router.push({
                path: APP_PATHS.catalog,
                query: {
                    library: selectedLibrarySlug.value,
                    document: file.document_id,
                    from: 'my-tasks',
                    taskPath: selectedPath.value,
                },
            });
        }

        function fileCapabilities(file) {
            const labels = { search: '向量', graph: '图谱' };
            return capabilityEntries(file?.catalog?.capabilities)
                .filter((item) => ['search', 'graph'].includes(item.key))
                .map((item) => ({ ...item, label: labels[item.key] }));
        }

        function fileProcessingLabel(file) {
            if (file?.result_operation === 'stored_only') return '仅保存，尚未转写/向量化';
            const status = String(file?.processing_status || '');
            if (status === 'succeeded') return '知识处理完成';
            if (status === 'failed') return '知识处理失败，原文件仍已保存';
            if (status) return `知识处理：${status}`;
            return '文件已保存';
        }

        function openReplace(file) {
            if (!file?.document_id || !selectedLibrarySlug.value) return;
            router.push({
                path: APP_PATHS.importData,
                query: {
                    library: selectedLibrarySlug.value,
                    mode: 'replace',
                    replaceDocumentId: file.document_id,
                    replaceTitle: file.catalog?.title || file.file_name || '',
                },
            });
        }

        onMounted(async () => {
            await loadLibraries();
            const returnLibrary = String(route.query.library || '');
            if (!libraryOptions.value.some((item) => item.value === returnLibrary)) return;
            const returnPath = String(route.query.path || '');
            selectedLibrarySlug.value = returnLibrary;
            selectedPath.value = returnPath;
            if (returnPath) {
                try {
                    const root = await api.listStoredFiles({
                        librarySlug: returnLibrary,
                        path: '',
                        page: 1,
                        pageSize: PAGE_SIZE,
                    });
                    rootFolders.value = Array.isArray(root?.folders) ? root.folders : [];
                } catch {
                    rootFolders.value = [];
                }
            }
            await loadFiles();
        });
        onBeforeUnmount(() => {
            requestSequence += 1;
            if (requestController) requestController.abort();
        });

        return {
            breadcrumbs,
            changeLibrary,
            changePage,
            currentDirectoryName,
            currentPage,
            currentTotal,
            deleteCurrentFolder,
            deleteFile,
            deleteSelected,
            downloadFile,
            directoryFolders,
            error,
            formatTime,
            capabilityStateLabel,
            catalogOverallLabel,
            catalogOverallTag,
            documentStatusLabel,
            documentStatusTag,
            documentTypeIcon,
            fileCapabilities,
            fileProcessingLabel,
            libraryLoading,
            libraryOptions,
            loaded,
            loading,
            loadFiles,
            openBreadcrumb,
            openFile,
            openReplace,
            openFolder,
            pageData,
            pageSize: PAGE_SIZE,
            paginationTotal,
            rootFolders,
            selectedLibrarySlug,
            selectedCount,
            selectedFileResourceIds,
            selectedPath,
            totalPages,
            toggleFile,
            isFileSelected,
        };
    },
    template: `
    <section class="my-files-page">
        <header class="my-files-header">
            <div>
                <h2>我的文件</h2>
                <p>选择知识库后按文件夹查看已验证保存的原文件；知识处理失败不会影响文件保存。</p>
            </div>
            <div class="my-files-actions">
                <el-select v-model="selectedLibrarySlug" class="my-files-library-select"
                           clearable filterable placeholder="请选择知识库"
                           :loading="libraryLoading" @change="changeLibrary">
                    <el-option v-for="library in libraryOptions" :key="library.value"
                               :label="library.label" :value="library.value" />
                </el-select>
                <el-button @click="loadFiles" :loading="loading" :disabled="!selectedLibrarySlug">
                    <local-icon icon="mdi:refresh" />刷新
                </el-button>
            </div>
        </header>

        <div v-if="selectedLibrarySlug" class="my-files-summary">
            <span>当前目录 <b>{{ currentTotal }}</b> 项</span>
            <span>{{ pageData.folder_total }} 个文件夹 · {{ pageData.file_total }} 个文件</span>
            <span v-if="totalPages > 1">第 {{ currentPage }} / {{ totalPages }} 页</span>
        </div>

        <div v-if="!selectedLibrarySlug" class="my-files-state my-files-empty">
            <local-icon icon="sidebar:document" />
            <strong>请先选择知识库</strong>
            <span>选择后只加载该知识库的当前文件夹。</span>
        </div>
        <div v-else-if="error" class="my-files-state my-files-error">
            <strong>{{ error }}</strong>
            <el-button type="primary" plain @click="loadFiles">重新加载</el-button>
        </div>
        <div v-else-if="loading && !loaded" class="my-files-state">正在加载当前文件夹…</div>
        <div v-else class="my-files-browser">
            <aside class="my-files-tree" aria-label="我的文件夹">
                <button type="button" class="my-files-tree-item is-root"
                        :class="{ 'is-active': !selectedPath }"
                        @click="openBreadcrumb({ path: '' })">
                    <span>我的文件</span>
                </button>
                <button v-for="folder in rootFolders" :key="folder.path" type="button"
                        class="my-files-tree-item"
                        :class="{ 'is-active': selectedPath === folder.path || selectedPath.startsWith(folder.path + '/') }"
                        @click="openFolder(folder)">
                    <span>{{ folder.name }}</span>
                </button>
                <div v-if="!rootFolders.length" class="my-files-tree-empty">暂无根目录文件夹</div>
            </aside>

            <main class="my-files-content">
                <nav class="my-files-breadcrumb" aria-label="文件夹路径">
                    <template v-for="(item, index) in breadcrumbs" :key="item.path || 'root'">
                        <span v-if="index" class="my-files-breadcrumb-separator">/</span>
                        <button type="button" @click="openBreadcrumb(item)">{{ item.name }}</button>
                    </template>
                </nav>
                <div class="my-files-content-head">
                    <div>
                        <h3>{{ currentDirectoryName }}</h3>
                        <span>{{ currentTotal }} 项</span>
                    </div>
                    <div class="my-files-content-tools">
                        <el-button v-if="selectedPath" type="danger" plain size="small" @click="deleteCurrentFolder">
                            删除当前文件夹
                        </el-button>
                        <el-button v-if="selectedCount" type="danger" plain size="small" @click="deleteSelected">
                            删除选中文件 ({{ selectedCount }})
                        </el-button>
                        <span v-if="loading" class="my-files-loading-label">正在刷新…</span>
                    </div>
                </div>

                <div v-if="!directoryFolders.length && !pageData.files.length" class="my-files-state my-files-empty">
                    <local-icon icon="sidebar:document" />
                    <strong>这个文件夹是空的</strong>
                    <span>这里只显示当前账号已成功保存的文件。</span>
                </div>
                <div v-else class="my-files-entries">
                    <div v-for="folder in directoryFolders" :key="folder.path" class="my-files-entry my-files-folder-entry">
                        <button type="button" class="my-files-entry-main" @click="openFolder(folder)">
                            <span class="my-files-entry-name">{{ folder.name }}</span>
                            <span class="my-files-entry-meta">文件夹</span>
                        </button>
                        <span class="my-files-entry-meta">{{ folder.file_total }} 个文件</span>
                    </div>
                    <div v-for="file in pageData.files" :key="file.file_resource_id"
                         class="my-files-entry my-files-file-entry">
                        <el-checkbox :model-value="isFileSelected(file.file_resource_id)"
                                     @change="toggleFile(file.file_resource_id)" @click.stop />
                        <button type="button" class="my-files-entry-main" @click="openFile(file)">
                            <img v-if="documentTypeIcon(file.catalog || { title: file.file_name })" class="my-files-entry-icon"
                                 :src="documentTypeIcon(file.catalog || { title: file.file_name })" alt="" aria-hidden="true" />
                            <local-icon v-else icon="mdi:file-document-outline" />
                            <span class="my-files-entry-copy">
                                <span class="my-files-entry-name">{{ file.catalog?.title || file.file_name }}</span>
                                <span v-if="file.catalog" class="my-files-asset-meta">
                                    <el-tag :type="documentStatusTag(file.catalog.document_status)" size="small">
                                        {{ documentStatusLabel(file.catalog.document_status) }}
                                    </el-tag>
                                    <el-tag :type="catalogOverallTag(file.catalog.overall_state)" size="small">
                                        {{ catalogOverallLabel(file.catalog.overall_state) }}
                                    </el-tag>
                                    <span v-for="capability in fileCapabilities(file)" :key="capability.key"
                                          class="my-files-capability" :title="capability.label + '：' + capabilityStateLabel(capability.state)">
                                        {{ capability.label }}：{{ capabilityStateLabel(capability.state) }}
                                    </span>
                                    <span class="my-files-version">v{{ file.catalog.revision_no }}</span>
                                </span>
                                <span v-else class="my-files-asset-meta">
                                    <el-tag type="success" size="small">文件已保存</el-tag>
                                    <span class="my-files-capability">{{ fileProcessingLabel(file) }}</span>
                                </span>
                            </span>
                        </button>
                        <span class="my-files-entry-date" :title="formatTime(file.created_at)">
                            <small>上传时间</small>
                            <span>{{ formatTime(file.created_at) }}</span>
                        </span>
                        <div class="my-files-entry-actions">
                            <el-button text size="small" @click.stop="downloadFile(file)">下载</el-button>
                            <template v-if="file.document_id">
                                <el-button text size="small" @click.stop="openFile(file)">详情</el-button>
                                <el-button text size="small" :disabled="!file.catalog" @click.stop="openReplace(file)">替换</el-button>
                            </template>
                            <el-button text type="danger" size="small" @click.stop="deleteFile(file)">删除</el-button>
                        </div>
                    </div>
                </div>

                <el-pagination v-if="totalPages > 1" class="my-files-pagination"
                               background layout="prev, pager, next"
                               :current-page="currentPage" :page-size="pageSize"
                               :total="paginationTotal" @current-change="changePage" />
            </main>
        </div>
    </section>
    `,
};
