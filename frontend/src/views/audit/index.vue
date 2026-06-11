<script setup lang="ts">
import { h, onMounted, ref } from 'vue';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'AuditPage' });

const logs = ref<any[]>([]);
const loading = ref(false);
const action = ref('');

async function load() {
  loading.value = true;
  const params: any = { limit: 200 };
  if (action.value) params.action = action.value;
  const { data, error }: any = await biz.fetchAuditLog(params);
  loading.value = false;
  if (!error) logs.value = data || [];
}
function fmt(t: any) {
  if (!t) return '';
  try { return JSON.stringify(t); } catch { return String(t); }
}

const columns: any[] = [
  { title: '时间', key: 'at', width: 200 },
  { title: '操作者', key: 'actor_user_id', width: 200, render: (r: any) => h('span', { class: 'mono' }, r.actor_user_id || '-') },
  { title: '动作', key: 'action', width: 220 },
  { title: 'target', key: 'target', minWidth: 320, render: (r: any) => h('span', { class: 'mono' }, fmt(r.target)) }
];

onMounted(load);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <h3 style="margin: 0">审计日志</h3>
        <NSpace align="center">
          <NInput v-model:value="action" placeholder="action（如 library.create）" clearable style="width: 240px" />
          <NButton :loading="loading" @click="load">查询</NButton>
        </NSpace>
      </NSpace>
    </NCard>
    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="logs" :loading="loading" :scroll-x="1000" size="small" />
    </NCard>
  </NSpace>
</template>

<style scoped>
.mono { font-family: Consolas, monospace; font-size: 12px; }
</style>
