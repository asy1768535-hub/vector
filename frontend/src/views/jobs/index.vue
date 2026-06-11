<script setup lang="ts">
import { h, onMounted, ref } from 'vue';
import { NButton, NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'JobsPage' });

const jobs = ref<any[]>([]);
const loading = ref(false);
const status = ref<string | null>(null);

const statusOptions = [
  { label: 'pending', value: 'pending' },
  { label: 'processing', value: 'processing' },
  { label: 'done', value: 'done' },
  { label: 'failed', value: 'failed' }
];

async function load() {
  loading.value = true;
  const params: any = { limit: 200 };
  if (status.value) params.status = status.value;
  const { data, error }: any = await biz.fetchJobs(params);
  loading.value = false;
  if (!error) jobs.value = data || [];
}
function retry(row: any) {
  biz.retryJob(row.id).then(({ error }: any) => { if (!error) { window.$message?.success('已重置为 pending'); load(); } });
}

const columns: any[] = [
  { title: 'Job', key: 'id', width: 110, render: (r: any) => h('span', { class: 'mono' }, `${r.id.slice(0, 8)}…`) },
  { title: 'Document', key: 'document_id', width: 110, render: (r: any) => h('span', { class: 'mono' }, `${(r.document_id || '').slice(0, 8)}…`) },
  { title: '状态', key: 'status', width: 100, render: (r: any) => h(NTag, { type: r.status === 'done' ? 'success' : r.status === 'failed' ? 'error' : 'warning', size: 'small' }, { default: () => r.status }) },
  { title: 'Worker', key: 'worker_id', width: 160 },
  { title: '尝试', key: 'attempt_count', width: 70 },
  { title: '最后错误', key: 'last_error', minWidth: 200, ellipsis: { tooltip: true } },
  { title: '创建', key: 'created_at', width: 180 },
  { title: '完成', key: 'finished_at', width: 180 },
  { title: '操作', key: 'actions', width: 90, fixed: 'right', render: (r: any) => h(NButton, { size: 'small', disabled: !['failed', 'processing'].includes(r.status), onClick: () => retry(r) }, { default: () => '重试' }) }
];

onMounted(load);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <h3 style="margin: 0">任务监控 (embedding_jobs)</h3>
        <NSpace align="center">
          <NSelect v-model:value="status" :options="statusOptions" placeholder="全部状态" clearable style="width: 160px" />
          <NButton :loading="loading" @click="load">查询</NButton>
        </NSpace>
      </NSpace>
    </NCard>
    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="jobs" :loading="loading" :scroll-x="1300" size="small" />
    </NCard>
  </NSpace>
</template>

<style scoped>
.mono { font-family: Consolas, monospace; font-size: 12px; }
</style>
