<script setup lang="ts">
import { h, onMounted, ref } from 'vue';
import { NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';
import { useLibraryOptions } from '@/hooks/use-libs';

defineOptions({ name: 'ImportPage' });

const { options, loadFor } = useLibraryOptions();
const slug = ref<string | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const selectedFile = ref<File | null>(null);
const loading = ref(false);
const result = ref<any>(null);

function trigger() {
  fileInput.value?.click();
}
function onFileChange(e: any) {
  const f = e.target.files;
  if (f && f.length) { selectedFile.value = f[0]; result.value = null; }
}

async function handleImport() {
  if (!slug.value) { window.$message?.warning('请选择目标库'); return; }
  if (!selectedFile.value) { window.$message?.warning('请先选择文件'); return; }
  loading.value = true;
  const { data, error }: any = await biz.importFile(slug.value, selectedFile.value);
  loading.value = false;
  if (error) return;
  result.value = data;
  window.$message?.success(`导入成功！共 ${data.imported_count} 篇`);
  selectedFile.value = null;
  if (fileInput.value) fileInput.value.value = '';
}

function formatSize(b: number) {
  if (!b) return '0 B';
  const k = 1024;
  const s = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(b) / Math.log(k));
  return `${parseFloat((b / k ** i).toFixed(2))} ${s[i]}`;
}

const columns: any[] = [
  { title: '文档标题', key: 'title', minWidth: 180, ellipsis: { tooltip: true } },
  { title: '分片数', key: 'chunk_count', width: 90, align: 'center' },
  { title: '状态', key: 'status', width: 100, align: 'center', render: (r: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => r.status }) },
  { title: '文档 ID', key: 'document_id', width: 110, render: (r: any) => h('span', { class: 'mono' }, `${(r.document_id || '').slice(0, 8)}…`) }
];

onMounted(async () => {
  await loadFor('insert');
  if (options.value.length) slug.value = options.value[0].value;
});
</script>

<template>
  <NGrid :x-gap="16" :y-gap="16" :cols="24" responsive="screen" item-responsive>
    <NGi span="24 m:10">
      <NCard :bordered="false" class="card-wrapper" title="文件导入">
        <NSpace vertical :size="16">
          <div>
            <div class="lbl">选择目标库</div>
            <NSelect v-model:value="slug" :options="options" placeholder="请选择库" />
          </div>
          <div>
            <div class="lbl">选择文件</div>
            <input ref="fileInput" type="file" style="display: none" accept=".txt,.md,.json,.csv" @change="onFileChange" />
            <NButton type="primary" @click="trigger">选择本地文件</NButton>
            <div v-if="selectedFile" class="file-info">文件名：<b>{{ selectedFile.name }}</b><br />大小：{{ formatSize(selectedFile.size) }}</div>
            <div class="hint">支持 .txt / .md / .json / .csv。txt/md 作单篇摄入；json 支持对象或数组(含 text)；csv 每行一篇。</div>
          </div>
          <NButton type="primary" :disabled="!selectedFile" :loading="loading" block @click="handleImport">开始导入</NButton>
        </NSpace>
      </NCard>
    </NGi>
    <NGi span="24 m:14">
      <NCard v-if="result" :bordered="false" class="card-wrapper" title="导入结果">
        <NAlert type="success" :bordered="false" style="margin-bottom: 12px">导入完成！Worker 正在异步生成向量，可在「任务监控」查看。</NAlert>
        <div style="margin-bottom: 10px">成功摄入：<b>{{ result.imported_count }}</b> 篇</div>
        <NDataTable :columns="columns" :data="result.documents" size="small" />
      </NCard>
      <NCard v-else :bordered="false" class="card-wrapper empty-card">
        <div style="text-align: center; color: #999">
          <p style="font-size: 16px">暂无导入记录</p>
          <p style="font-size: 13px">请在左侧选择库并上传文件。</p>
        </div>
      </NCard>
    </NGi>
  </NGrid>
</template>

<style scoped>
.lbl { margin-bottom: 6px; font-size: 13px; color: #888; }
.file-info { background: rgba(128, 128, 128, 0.08); padding: 10px; border-radius: 6px; font-size: 13px; margin-top: 8px; line-height: 1.6; }
.hint { font-size: 12px; color: #999; margin-top: 6px; line-height: 1.6; }
.mono { font-family: Consolas, monospace; font-size: 12px; }
.empty-card { min-height: 300px; display: flex; align-items: center; justify-content: center; }
</style>
