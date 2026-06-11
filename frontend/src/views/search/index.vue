<script setup lang="ts">
import { h, onMounted, ref } from 'vue';
import { NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';
import { useLibraryOptions } from '@/hooks/use-libs';

defineOptions({ name: 'SearchPage' });

const { options, loadFor } = useLibraryOptions();
const slug = ref<string | null>(null);
const query = ref('');
const limit = ref(5);
const results = ref<any[]>([]);
const loading = ref(false);

async function handleSearch() {
  if (!slug.value) { window.$message?.warning('请先选择库'); return; }
  if (!query.value.trim()) { window.$message?.warning('请输入关键词'); return; }
  loading.value = true;
  const { data, error }: any = await biz.queryLibrary(slug.value, { query: query.value.trim(), limit: limit.value });
  loading.value = false;
  if (error) return;
  results.value = data.results || [];
  if (!results.value.length) window.$message?.info('未找到相似分片');
}

const columns: any[] = [
  { title: '相似度', key: 'similarity', width: 120, align: 'center', render: (r: any) => h(NTag, { type: r.similarity >= 0.7 ? 'success' : r.similarity >= 0.5 ? 'warning' : 'default', size: 'small' }, { default: () => `${(r.similarity * 100).toFixed(1)}%` }) },
  { title: '分片正文', key: 'text', render: (r: any) => h('div', { style: 'white-space:pre-wrap;font-size:13px;line-height:1.6' }, r.text) }
];

onMounted(async () => {
  await loadFor('read');
  if (options.value.length) slug.value = options.value[0].value;
});
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace align="center" :wrap="false">
        <span>目标库：</span>
        <NSelect v-model:value="slug" :options="options" placeholder="选择库" style="width: 220px" />
        <span>关键词：</span>
        <NInput v-model:value="query" placeholder="输入关键词查询向量库" style="width: 300px" @keyup.enter="handleSearch" />
        <span>返回数：</span>
        <NInputNumber v-model:value="limit" :min="1" :max="20" style="width: 120px" />
        <NButton type="primary" :loading="loading" @click="handleSearch">搜索</NButton>
      </NSpace>
    </NCard>
    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="results" :loading="loading" size="small" />
    </NCard>
  </NSpace>
</template>
