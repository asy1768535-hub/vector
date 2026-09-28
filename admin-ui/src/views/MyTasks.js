import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { useRouter } from 'vue-router';

import * as api from '../api.js';
import { APP_PATHS } from '../domain_navigation.js';
import { selectableUploadLibraries } from '../my_files_ui.js';
import { store } from '../store.js';

const PAGE_SIZE = 50;

const STATUS_LABELS = Object.freeze({
    uploading: '上传中',
    queued: '待处理',
    processing: '处理中',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
    superseded: '已替换',
});

const STAGE_LABELS = Object.freeze({
    uploading: '保存文件',
    queued: '等待处理',
    converting: '转换文本',
    conversion_ready: '等待解析',
    validating: '校验文件',
    parsing: '解析内容',
    chunking: '切分内容',
    embedding: '生成向量',
    graph: '提取图谱',
    completed: '完成',
});

function emptySummary() {
    return { total: 0, pending: 0, processing: 0, succeeded: 0, failed: 0 };
}

function statusTag(status) {
    return ({ succeeded: 'success', failed: 'danger', processing: 'primary', queued: 'warning' })[status]
        || 'info';
}

function formatTime(value) {
    const time = new Date(value || '');
    return Number.isNaN(time.getTime()) ? '—' : time.toLocaleString('zh-CN');
}

function parentPath(relativePath) {
    const path = String(relativePath || '').replace(/^\/+/, '');
    const slash = path.lastIndexOf('/');
    return slash > 0 ? `/${path.slice(0, slash)}` : '';
}

export default {
    setup() {
        const router = useRouter();
        const scope = ref('all');
        const selectedLibrarySlug = ref('');
        const statusFilter = ref('all');
        const libraryOptions = ref([]);
        const libraryLoading = ref(false);
        const currentPage = ref(1);
        const tasks = ref([]);
        const summary = ref(emptySummary());
        const loading = ref(false);
        const retryingId = ref('');
        const error = ref('');
        let controller = null;
        let requestSequence = 0;

        const statCards = computed(() => ([
            { key: 'pending', label: '待处理', icon: 'status:pending', type: 'warning' },
            { key: 'processing', label: '处理中', icon: 'status:processing', type: 'primary' },
            { key: 'succeeded', label: '已完成', icon: 'status:success', type: 'success' },
            { key: 'failed', label: '失败', icon: 'status:failed', type: 'danger' },
        ].map((item) => ({ ...item, value: Number(summary.value[item.key] || 0) }))));
        const totalTasks = computed(() => Number(summary.value.total || 0));
        const totalPages = computed(() => Math.max(1, Math.ceil(Number(summary.value.total || 0) / PAGE_SIZE)));

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

        async function loadTasks() {
            if (controller) controller.abort();
            const activeController = new AbortController();
            controller = activeController;
            const sequence = ++requestSequence;
            loading.value = true;
            error.value = '';
            try {
                const [page, nextSummary] = await Promise.all([
                    api.listPersonalImportTasks({
                        scope: scope.value,
                        limit: PAGE_SIZE,
                        page: currentPage.value,
                        librarySlug: selectedLibrarySlug.value,
                        status: statusFilter.value,
                    }, { signal: activeController.signal }),
                    api.getPersonalImportTaskSummary({
                        scope: scope.value,
                        librarySlug: selectedLibrarySlug.value,
                        status: statusFilter.value,
                    }, { signal: activeController.signal }),
                ]);
                if (sequence !== requestSequence) return;
                tasks.value = page.items || [];
                if (nextSummary) summary.value = { ...emptySummary(), ...nextSummary };
            } catch (loadError) {
                if (loadError?.name !== 'AbortError' && sequence === requestSequence) {
                    error.value = loadError?.message || '任务加载失败，请稍后重试';
                }
            } finally {
                if (sequence === requestSequence) {
                    loading.value = false;
                }
            }
        }

        function refreshTasks() {
            currentPage.value = 1;
            return loadTasks();
        }

        function changeFilters() {
            currentPage.value = 1;
            return loadTasks();
        }

        function changePage(page) {
            currentPage.value = page;
            return loadTasks();
        }

        async function retryTask(task) {
            if (!task?.can_retry || retryingId.value) return;
            retryingId.value = task.id;
            try {
                await api.retryPersonalImportTask(task.id);
                ElMessage.success('已提交重试');
                await loadTasks();
            } catch (retryError) {
                ElMessage.error(retryError?.message || '重试失败，请稍后再试');
            } finally {
                retryingId.value = '';
            }
        }

        function openFiles(task) {
            router.push({
                path: APP_PATHS.myFiles,
                query: {
                    library: task.library_slug,
                    path: parentPath(task.relative_path),
                },
            });
        }

        function taskStageLabel(task) {
            if (task?.result_operation === 'duplicate_source') return '已跳过：库内已有相同文件';
            return STAGE_LABELS[task?.stage] || task?.stage || '—';
        }

        onMounted(async () => {
            await loadLibraries();
            await loadTasks();
        });
        onBeforeUnmount(() => controller?.abort());

        return {
            changeFilters,
            changePage,
            currentPage,
            error,
            formatTime,
            libraryLoading,
            libraryOptions,
            loadTasks,
            loading,
            openFiles,
            PAGE_SIZE,
            refreshTasks,
            retryTask,
            retryingId,
            router,
            selectedLibrarySlug,
            scope,
            statCards,
            statusTag,
            statusLabel: (status) => STATUS_LABELS[status] || status || '—',
            statusFilter,
            taskStageLabel,
            tasks,
            totalPages,
            totalTasks,
        };
    },
    template: `
        <section class="my-tasks-page">
            <header class="my-tasks-header">
                <div>
                    <h2>我的任务</h2>
                    <p>只展示你本人上传或替换文件产生的处理任务；异常文件也在这里统一显示，可查看原因并重试。</p>
                </div>
                <div class="my-tasks-actions">
                    <el-select v-model="selectedLibrarySlug" clearable filterable :loading="libraryLoading" placeholder="全部知识库" aria-label="选择知识库" @change="changeFilters">
                        <el-option label="全部知识库" value="" />
                        <el-option v-for="library in libraryOptions" :key="library.value" :label="library.label" :value="library.value" />
                    </el-select>
                    <el-select v-model="statusFilter" aria-label="任务状态" @change="changeFilters">
                        <el-option label="全部状态" value="all" />
                        <el-option label="待处理" value="pending" />
                        <el-option label="处理中" value="processing" />
                        <el-option label="已完成" value="succeeded" />
                        <el-option label="失败" value="failed" />
                    </el-select>
                    <el-select v-model="scope" aria-label="任务时间范围" @change="changeFilters">
                        <el-option label="全部任务" value="all" />
                        <el-option label="近 30 天" value="30d" />
                    </el-select>
                    <el-button :loading="loading" @click="refreshTasks"><local-icon icon="mdi:refresh" />刷新</el-button>
                </div>
            </header>

            <div class="my-tasks-stats" aria-label="个人任务统计">
                <article v-for="card in statCards" :key="card.key" class="my-tasks-stat-card" :class="'is-' + card.type">
                    <local-icon :icon="card.icon" />
                    <div><strong>{{ card.value }}</strong><span>{{ card.label }}</span></div>
                </article>
            </div>

            <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="my-tasks-alert" />

            <div class="my-tasks-table-card" v-loading="loading">
                <div class="my-tasks-table-head">
                    <div><strong>上传任务</strong><span>共 {{ totalTasks }} 条 · 第 {{ currentPage }} / {{ totalPages }} 页</span></div>
                    <el-button text type="primary" @click="router.push({ path: APP_PATHS.myFiles })">我的文件</el-button>
                </div>
                <el-table :data="tasks" empty-text="暂时没有你的上传任务" class="my-tasks-table">
                    <el-table-column label="文件 / 知识库" min-width="260">
                        <template #default="{ row }">
                            <div class="my-tasks-file">
                                <strong>{{ row.file_name || '未命名文件' }}</strong>
                                <span>{{ row.library_name || row.library_slug || '—' }}<template v-if="row.relative_path"> · {{ row.relative_path }}</template></span>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="类型" width="78">
                        <template #default="{ row }">{{ row.operation_type === 'replace' ? '替换' : '导入' }}</template>
                    </el-table-column>
                    <el-table-column label="状态" width="100">
                        <template #default="{ row }"><el-tag size="small" :type="statusTag(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
                    </el-table-column>
                    <el-table-column label="当前阶段" min-width="190">
                        <template #default="{ row }">{{ taskStageLabel(row) }}</template>
                    </el-table-column>
                    <el-table-column label="提交时间" width="174">
                        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
                    </el-table-column>
                    <el-table-column label="失败原因" min-width="220">
                        <template #default="{ row }">
                            <div v-if="row.failure_message">
                                <span class="my-tasks-failure">{{ row.failure_message }}</span>
                                <div v-if="row.failure_action" class="my-tasks-failure-action">{{ row.failure_action }}</div>
                                <div v-if="row.status === 'failed' && !String(row.failure_action || '').includes('联系')" class="my-tasks-failure-action">若仍失败，请联系管理员</div>
                            </div>
                            <span v-else class="my-tasks-failure">—</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="操作" width="144" fixed="right">
                        <template #default="{ row }">
                            <el-button text size="small" @click="openFiles(row)">查看文件</el-button>
                            <el-button v-if="row.can_retry" text type="primary" size="small" :loading="retryingId === row.id" @click="retryTask(row)">重试</el-button>
                        </template>
                    </el-table-column>
                </el-table>
                <div v-if="totalTasks > PAGE_SIZE" class="my-tasks-pagination">
                    <el-pagination
                        :current-page="currentPage"
                        :page-size="PAGE_SIZE"
                        :total="totalTasks"
                        layout="prev, pager, next"
                        @current-change="changePage"
                    />
                </div>
            </div>
        </section>
    `,
};
