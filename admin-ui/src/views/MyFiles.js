import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { useRoute, useRouter } from 'vue-router';

import * as api from '../api.js';
import { APP_PATHS } from '../domain_navigation.js';
import { fileTypeIcon } from '../import_ui.js';
import {
    myFilesAncestorPaths,
    myFilesBreadcrumbs,
    myFilesExpandedFolderPaths,
    selectableUploadLibraries,
} from '../my_files_ui.js';
import { uploadEmpty } from '../illustrations.js';
import { store } from '../store.js';

const PERSONAL_TASK_POLL_DELAY = 5000;
const PERSONAL_TASK_STAGE_LABEL = {
    uploading: '正在上传',
    queued: '等待处理',
    converting: '正在解析',
    conversion_ready: '正在解析',
    validating: '正在解析',
    parsing: '正在解析',
    chunking: '正在生成知识片段',
    embedding: '正在生成向量',
    graph: '正在构建知识图谱',
    completed: '已完成',
};
const PERSONAL_TASK_STAGE_PROGRESS = {
    uploading: 20,
    queued: 35,
    converting: 45,
    conversion_ready: 45,
    validating: 45,
    parsing: 55,
    chunking: 70,
    embedding: 85,
    graph: 95,
    completed: 100,
};

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraryOptions = ref([]);
        const libraryLoading = ref(false);
        const selectedLibrarySlug = ref('');
        const taskPageSize = ref(20);
        const personalTasks = ref([]);
        const personalTaskSummary = ref(null);
        const personalTaskLoading = ref(false);
        const personalTaskError = ref('');
        const personalTaskPage = ref(0);
        const personalTaskPath = ref('');
        const personalTaskExpandedPaths = ref(new Set());
        const personalTaskFolders = ref([]);
        const personalTaskFolderChildren = ref({});
        const personalTaskFolderTotal = ref(0);
        const personalTaskFileTotal = ref(0);
        const personalTaskRetryingId = ref('');
        let personalTaskPollTimer = null;
        let personalTaskPollController = null;
        let personalTaskPollPromise = null;
        let personalTaskRequestSequence = 0;

        const personalTaskHasPreviousPage = computed(() => personalTaskPage.value > 0);
        const personalTaskTotal = computed(() => (
            personalTaskFolderTotal.value + personalTaskFileTotal.value
        ));
        const personalTaskTotalPages = computed(() => Math.max(
            1,
            Math.ceil(personalTaskTotal.value / taskPageSize.value),
        ));
        const personalTaskHasNextPage = computed(() => (
            personalTaskPage.value + 1 < personalTaskTotalPages.value
        ));
        const personalTaskBreadcrumbs = computed(() => myFilesBreadcrumbs(personalTaskPath.value));
        const personalTaskCurrentDirectoryName = computed(() => (
            personalTaskBreadcrumbs.value[personalTaskBreadcrumbs.value.length - 1]?.name
            || '我的任务'
        ));
        const personalTaskTreeFolders = computed(() => {
            const entries = [];
            const visitedPaths = new Set();

            function appendFolders(parentPath, depth) {
                for (const folder of personalTaskFolderChildren.value[parentPath] || []) {
                    if (!folder?.path || visitedPaths.has(folder.path)) continue;
                    visitedPaths.add(folder.path);
                    entries.push({ folder, depth });
                    if (personalTaskExpandedPaths.value.has(folder.path)) {
                        appendFolders(folder.path, depth + 1);
                    }
                }
            }

            appendFolders('', 0);
            return entries;
        });
        const personalTaskHasActiveWork = computed(() => {
            const summary = personalTaskSummary.value || {};
            if (Number(summary.pending || 0) + Number(summary.processing || 0) > 0) return true;
            return personalTasks.value.some((task) => ![
                'succeeded', 'failed', 'cancelled', 'superseded',
            ].includes(task?.status));
        });

        function resetPersonalTaskPagination() {
            personalTasks.value = [];
            personalTaskPage.value = 0;
            personalTaskFolders.value = [];
            personalTaskFolderTotal.value = 0;
            personalTaskFileTotal.value = 0;
        }

        function resetPersonalTaskDirectory() {
            personalTaskPath.value = '';
            personalTaskExpandedPaths.value = new Set();
            personalTaskFolderChildren.value = {};
            resetPersonalTaskPagination();
        }

        function expandPersonalTaskPath(path) {
            personalTaskExpandedPaths.value = new Set(myFilesExpandedFolderPaths(path));
        }

        function personalTaskStageLabel(task) {
            return PERSONAL_TASK_STAGE_LABEL[task?.stage] || '等待处理';
        }

        function personalTaskProgress(task) {
            if (task?.status === 'succeeded') return 100;
            return PERSONAL_TASK_STAGE_PROGRESS[task?.stage] || 10;
        }

        function personalTaskFileIsUsable(task) {
            return task?.stage === 'graph';
        }

        function personalTaskStatusLabel(status, stage = '') {
            if (status === 'failed' && stage === 'graph') return '图谱失败';
            return {
                pending: '等待处理',
                uploading: '正在上传',
                queued: '等待处理',
                processing: '处理中',
                succeeded: '已完成',
                failed: '处理失败',
                cancelled: '已取消',
                superseded: '已替换',
            }[status] || '处理中';
        }

        function personalTaskStatusTag(status) {
            return {
                succeeded: 'success',
                failed: 'danger',
                cancelled: 'info',
                superseded: 'info',
                pending: 'info',
                queued: 'info',
                uploading: 'warning',
                processing: 'warning',
            }[status] || 'info';
        }

        function formatPersonalTaskTime(value) {
            const time = new Date(value || '');
            return Number.isNaN(time.getTime()) ? '—' : time.toLocaleString('zh-CN');
        }

        function stopPersonalTaskPolling() {
            if (personalTaskPollTimer) clearTimeout(personalTaskPollTimer);
            personalTaskPollTimer = null;
            personalTaskRequestSequence += 1;
            if (personalTaskPollController) personalTaskPollController.abort();
            personalTaskPollController = null;
            personalTaskPollPromise = null;
        }

        function schedulePersonalTaskPolling(delay) {
            if (
                personalTaskPollTimer
                || personalTaskPollPromise
                || globalThis.document?.hidden
                || !selectedLibrarySlug.value
                || !personalTaskHasActiveWork.value
            ) return;
            personalTaskPollTimer = setTimeout(pollPersonalTasks, delay);
        }

        async function loadPersonalTaskSummary({ signal } = {}) {
            return api.getPersonalImportTaskSummary('all', { signal });
        }

        async function loadPersonalTasks({ signal } = {}) {
            return api.listPersonalImportTaskFiles({
                librarySlug: selectedLibrarySlug.value,
                scope: 'all',
                path: personalTaskPath.value,
                page: personalTaskPage.value + 1,
                pageSize: taskPageSize.value,
            }, { signal });
        }

        function cachePersonalTaskFolders(path, folders, page) {
            const cachedFolders = page === 1
                ? []
                : (personalTaskFolderChildren.value[path] || []);
            const folderByPath = new Map(
                cachedFolders.map((folder) => [folder.path, folder]),
            );
            for (const folder of folders) folderByPath.set(folder.path, folder);
            personalTaskFolderChildren.value = {
                ...personalTaskFolderChildren.value,
                [path]: [...folderByPath.values()],
            };
        }

        async function loadPersonalTaskTreePath(path) {
            const requestedPath = String(path || '');
            const requestedLibrarySlug = selectedLibrarySlug.value;
            const parentPaths = myFilesAncestorPaths(requestedPath);
            if (!requestedLibrarySlug || !parentPaths.length) return;
            try {
                const pages = await Promise.all(parentPaths.map((parentPath) => (
                    api.listPersonalImportTaskFiles({
                        librarySlug: requestedLibrarySlug,
                        scope: 'all',
                        path: parentPath,
                        page: 1,
                        pageSize: taskPageSize.value,
                    })
                )));
                if (
                    selectedLibrarySlug.value !== requestedLibrarySlug
                    || personalTaskPath.value !== requestedPath
                ) return;
                pages.forEach((page, index) => {
                    cachePersonalTaskFolders(
                        parentPaths[index],
                        Array.isArray(page?.folders) ? page.folders : [],
                        1,
                    );
                });
            } catch {
                // The current directory remains usable if supplementary tree data cannot load.
            }
        }

        async function refreshPersonalTasks({ reset = false } = {}) {
            if (!selectedLibrarySlug.value || globalThis.document?.hidden) return false;
            if (personalTaskPollPromise) return personalTaskPollPromise;
            if (reset) resetPersonalTaskPagination();
            const requestSequence = personalTaskRequestSequence;
            const controller = new AbortController();
            personalTaskPollController = controller;
            personalTaskLoading.value = true;
            const refresh = (async () => {
                try {
                    const [summary, page] = await Promise.all([
                        loadPersonalTaskSummary({ signal: controller.signal }),
                        loadPersonalTasks({ signal: controller.signal }),
                    ]);
                    if (requestSequence !== personalTaskRequestSequence) return false;
                    const items = Array.isArray(page?.items) ? page.items : [];
                    const folders = Array.isArray(page?.folders) ? page.folders : [];
                    personalTasks.value = items;
                    personalTaskFolders.value = folders;
                    personalTaskFolderTotal.value = Number(page?.folder_total || 0);
                    personalTaskFileTotal.value = Number(page?.file_total || 0);
                    cachePersonalTaskFolders(personalTaskPath.value, folders, personalTaskPage.value + 1);
                    personalTaskSummary.value = summary || null;
                    personalTaskError.value = '';
                    return true;
                } catch (error) {
                    if (
                        error?.name !== 'AbortError'
                        && requestSequence === personalTaskRequestSequence
                    ) {
                        personalTaskError.value = '任务列表加载失败，请稍后重试';
                    }
                    return false;
                }
            })();
            personalTaskPollPromise = refresh;
            try {
                return await refresh;
            } finally {
                if (personalTaskPollPromise === refresh) personalTaskPollPromise = null;
                if (personalTaskPollController === controller) personalTaskPollController = null;
                if (requestSequence === personalTaskRequestSequence) {
                    personalTaskLoading.value = false;
                }
            }
        }

        async function pollPersonalTasks() {
            personalTaskPollTimer = null;
            await refreshPersonalTasks();
            schedulePersonalTaskPolling(PERSONAL_TASK_POLL_DELAY);
        }

        function startPersonalTaskPolling() {
            schedulePersonalTaskPolling(PERSONAL_TASK_POLL_DELAY);
        }

        async function reloadPersonalTasks() {
            stopPersonalTaskPolling();
            personalTaskSummary.value = null;
            personalTaskError.value = '';
            await refreshPersonalTasks({ reset: true });
            startPersonalTaskPolling();
        }

        async function loadLibraries() {
            libraryLoading.value = true;
            try {
                const adminLibraries = store.user?.is_superuser ? await api.listLibraries() : null;
                libraryOptions.value = selectableUploadLibraries(store.permissions, adminLibraries);
                const requestedLibrarySlug = String(route.query.library || '');
                if (libraryOptions.value.some((item) => item.value === requestedLibrarySlug)) {
                    selectedLibrarySlug.value = requestedLibrarySlug;
                } else if (!libraryOptions.value.some((item) => item.value === selectedLibrarySlug.value)) {
                    selectedLibrarySlug.value = libraryOptions.value[0]?.value || '';
                }
                resetPersonalTaskDirectory();
                personalTaskPath.value = typeof route.query.path === 'string' ? route.query.path : '';
                expandPersonalTaskPath(personalTaskPath.value);
                if (selectedLibrarySlug.value) {
                    await reloadPersonalTasks();
                    await loadPersonalTaskTreePath(personalTaskPath.value);
                }
            } catch (error) {
                ElMessage.error(error?.message || '知识库列表加载失败');
                libraryOptions.value = [];
                selectedLibrarySlug.value = '';
            } finally {
                libraryLoading.value = false;
            }
        }

        async function changePersonalTaskLibrary() {
            stopPersonalTaskPolling();
            resetPersonalTaskDirectory();
            if (selectedLibrarySlug.value) await reloadPersonalTasks();
        }

        async function changePersonalTaskPageSize() {
            stopPersonalTaskPolling();
            resetPersonalTaskPagination();
            await refreshPersonalTasks();
            startPersonalTaskPolling();
        }

        async function changePersonalTaskPage(direction) {
            if (personalTaskLoading.value) return;
            const nextPage = personalTaskPage.value + direction;
            if (nextPage < 0 || (direction > 0 && !personalTaskHasNextPage.value)) return;
            const previousPage = personalTaskPage.value;
            personalTaskPage.value = nextPage;
            const loaded = await refreshPersonalTasks();
            if (!loaded) {
                personalTaskPage.value = previousPage;
                return;
            }
            startPersonalTaskPolling();
        }

        async function openPersonalTaskFolder(folder) {
            if (!folder?.path || personalTaskLoading.value) return;
            if (folder.path === personalTaskPath.value) {
                const expandedPaths = new Set(personalTaskExpandedPaths.value);
                if (expandedPaths.has(folder.path)) expandedPaths.delete(folder.path);
                else expandedPaths.add(folder.path);
                personalTaskExpandedPaths.value = expandedPaths;
                return;
            }
            stopPersonalTaskPolling();
            personalTaskPath.value = folder.path;
            expandPersonalTaskPath(folder.path);
            resetPersonalTaskPagination();
            await refreshPersonalTasks();
            startPersonalTaskPolling();
        }

        async function openPersonalTaskBreadcrumb(item) {
            if (!item || item.path === personalTaskPath.value || personalTaskLoading.value) return;
            stopPersonalTaskPolling();
            personalTaskPath.value = item.path;
            expandPersonalTaskPath(item.path);
            resetPersonalTaskPagination();
            await refreshPersonalTasks();
            startPersonalTaskPolling();
        }

        function openPersonalTaskFile(task) {
            if (!task?.document_id) {
                ElMessage.info('该文件尚未生成可打开的知识资产记录');
                return;
            }
            router.push({
                path: APP_PATHS.catalog,
                query: {
                    library: task.library_slug,
                    document: task.document_id,
                    from: 'my-tasks',
                    taskPath: personalTaskPath.value,
                },
            });
        }

        async function retryPersonalTask(task) {
            if (!task?.can_retry || personalTaskRetryingId.value) return;
            personalTaskRetryingId.value = String(task.id);
            try {
                await api.retryPersonalImportTask(task.id);
                ElMessage.success('任务已重新提交');
                await refreshPersonalTasks();
                startPersonalTaskPolling();
            } catch (error) {
                ElMessage.error(error?.message || '任务重试失败，请稍后重试');
            } finally {
                personalTaskRetryingId.value = '';
            }
        }

        function onVisibilityChange() {
            if (globalThis.document?.hidden) {
                stopPersonalTaskPolling();
                return;
            }
            void refreshPersonalTasks().then(startPersonalTaskPolling);
        }

        onMounted(() => {
            globalThis.document?.addEventListener('visibilitychange', onVisibilityChange);
            void loadLibraries();
        });
        onBeforeUnmount(() => {
            stopPersonalTaskPolling();
            globalThis.document?.removeEventListener('visibilitychange', onVisibilityChange);
        });

        return {
            libraryLoading, libraryOptions, selectedLibrarySlug, taskPageSize,
            personalTasks, personalTaskLoading, personalTaskError, personalTaskPage,
            personalTaskRetryingId, personalTaskPath, personalTaskFolders,
            personalTaskTreeFolders, personalTaskBreadcrumbs, personalTaskCurrentDirectoryName,
            personalTaskExpandedPaths,
            personalTaskTotal, personalTaskTotalPages, personalTaskHasPreviousPage,
            personalTaskHasNextPage, uploadEmpty, fileTypeIcon,
            reloadPersonalTasks, changePersonalTaskLibrary, changePersonalTaskPageSize,
            changePersonalTaskPage, openPersonalTaskFolder, openPersonalTaskBreadcrumb,
            openPersonalTaskFile, retryPersonalTask, personalTaskStageLabel,
            personalTaskProgress, personalTaskFileIsUsable, personalTaskStatusLabel,
            personalTaskStatusTag, formatPersonalTaskTime,
        };
    },
    template: `
    <section class="import-task-console">
        <div class="import-task-console-head">
            <h2>我的任务</h2>
            <div class="import-task-controls">
                <el-select v-model="selectedLibrarySlug" size="small" filterable placeholder="选择知识库"
                           class="import-task-library-select" aria-label="任务知识库"
                           :loading="libraryLoading" @change="changePersonalTaskLibrary">
                    <el-option v-for="library in libraryOptions" :key="library.value"
                               :label="library.label" :value="library.value" />
                </el-select>
                <el-select v-model="taskPageSize" size="small" aria-label="每页任务数"
                           @change="changePersonalTaskPageSize">
                    <el-option :value="20" label="20 条/页" />
                    <el-option :value="50" label="50 条/页" />
                </el-select>
                <el-tooltip content="刷新任务" placement="top">
                    <el-button circle text :loading="personalTaskLoading" aria-label="刷新任务"
                               @click="reloadPersonalTasks">
                        <local-icon icon="mdi:refresh" />
                    </el-button>
                </el-tooltip>
            </div>
        </div>
        <div v-if="selectedLibrarySlug" class="import-task-workspace">
            <aside class="import-task-tree" aria-label="上传任务文件夹">
                <button type="button" class="import-task-tree-item is-root"
                        :class="{ 'is-active': !personalTaskPath }"
                        @click="openPersonalTaskBreadcrumb({ path: '' })">
                    <local-icon icon="sidebar:task" />
                    <span>我的任务</span>
                </button>
                <button v-for="entry in personalTaskTreeFolders" :key="entry.folder.path"
                        type="button" class="import-task-tree-item"
                        :class="{ 'is-active': personalTaskPath === entry.folder.path }"
                        @click="openPersonalTaskFolder(entry.folder)">
                    <span v-for="level in entry.depth" :key="level"
                          class="import-task-tree-indent" aria-hidden="true"></span>
                    <local-icon icon="mdi:folder-outline" />
                    <span>{{ entry.folder.name }}</span>
                    <em>{{ entry.folder.file_total }}</em>
                </button>
                <div v-if="!personalTaskTreeFolders.length" class="import-task-tree-empty">
                    暂无上传文件夹
                </div>
            </aside>
            <div class="import-task-content">
                <el-alert v-if="personalTaskError" type="warning" :closable="false"
                          :title="personalTaskError" class="import-task-error" />
                <nav class="import-task-breadcrumb" aria-label="任务文件夹路径">
                    <template v-for="(item, index) in personalTaskBreadcrumbs" :key="item.path || 'root'">
                        <span v-if="index" class="import-task-breadcrumb-separator">/</span>
                        <button type="button" @click="openPersonalTaskBreadcrumb(item)">{{ item.name }}</button>
                    </template>
                </nav>
                <div class="import-task-directory-head">
                    <div>
                        <h3>{{ personalTaskCurrentDirectoryName }}</h3>
                        <span>{{ personalTaskTotal }} 项</span>
                    </div>
                    <span v-if="personalTaskLoading">正在刷新...</span>
                </div>
                <div v-if="personalTaskFolders.length" class="import-task-folder-entries">
                    <button v-for="folder in personalTaskFolders" :key="folder.path" type="button"
                            class="import-task-folder-entry" @click="openPersonalTaskFolder(folder)">
                        <local-icon icon="mdi:folder-outline" />
                        <span>{{ folder.name }}</span>
                        <em>{{ folder.file_total }} 个文件</em>
                    </button>
                </div>
                <div v-if="personalTasks.length" class="import-file-table-shell import-task-table-shell">
                    <el-table :data="personalTasks" v-loading="personalTaskLoading" row-key="id">
                        <el-table-column label="文件" min-width="230">
                            <template #default="{row: task}">
                                <button type="button" class="import-task-file-button"
                                        :disabled="!task.document_id" @click="openPersonalTaskFile(task)">
                                    <img v-if="fileTypeIcon({ name: task.file_name })" class="import-file-icon"
                                         :src="fileTypeIcon({ name: task.file_name })" alt="" aria-hidden="true" />
                                    <span class="import-file-name">{{ task.file_name }}</span>
                                </button>
                                <small v-if="task.relative_path" class="import-task-relative-path">
                                    {{ task.relative_path }}
                                </small>
                            </template>
                        </el-table-column>
                        <el-table-column label="知识库" min-width="140">
                            <template #default="{row: task}">{{ task.library_name || task.library_slug }}</template>
                        </el-table-column>
                        <el-table-column label="处理进度" min-width="250">
                            <template #default="{row: task}">
                                <div class="import-task-progress">
                                    <div class="import-task-progress-head">
                                        <span>{{ personalTaskStageLabel(task) }}</span>
                                        <span class="import-task-state-tags">
                                            <el-tag size="small" :type="personalTaskStatusTag(task.status)">
                                                {{ personalTaskStatusLabel(task.status, task.stage) }}
                                            </el-tag>
                                            <el-tag v-if="personalTaskFileIsUsable(task)" size="small" type="success" effect="plain">
                                                文件已可用
                                            </el-tag>
                                        </span>
                                    </div>
                                    <el-progress :percentage="personalTaskProgress(task)" :show-text="false" :stroke-width="6" />
                                    <div v-if="task.failure_message" class="import-task-failure">
                                        <span>{{ task.failure_message }}</span>
                                        <small v-if="task.status === 'failed' && task.stage === 'graph'">
                                            图谱失败不影响文件搜索和问答
                                        </small>
                                        <small v-if="task.failure_action">{{ task.failure_action }}</small>
                                        <el-button v-if="task.can_retry" link type="primary"
                                                   :loading="personalTaskRetryingId === String(task.id)"
                                                   @click="retryPersonalTask(task)">重试</el-button>
                                    </div>
                                </div>
                            </template>
                        </el-table-column>
                        <el-table-column label="提交时间" width="170">
                            <template #default="{row: task}">{{ formatPersonalTaskTime(task.created_at) }}</template>
                        </el-table-column>
                    </el-table>
                </div>
                <div v-else-if="!personalTaskFolders.length && !personalTaskLoading" class="import-task-empty">
                    <img :src="uploadEmpty" class="illustration-upload-empty" alt="" aria-hidden="true" />
                    <strong>这个文件夹暂无上传任务</strong>
                </div>
                <div v-if="personalTaskTotalPages > 1" class="import-task-pager">
                    <el-button :disabled="!personalTaskHasPreviousPage || personalTaskLoading"
                               @click="changePersonalTaskPage(-1)">上一页</el-button>
                    <span>第 {{ personalTaskPage + 1 }} 页</span>
                    <el-button :disabled="!personalTaskHasNextPage || personalTaskLoading"
                               @click="changePersonalTaskPage(1)">下一页</el-button>
                </div>
            </div>
        </div>
        <div v-else class="import-task-empty">
            <img :src="uploadEmpty" class="illustration-upload-empty" alt="" aria-hidden="true" />
            <strong>暂无可用知识库</strong>
        </div>
    </section>
    `,
};

