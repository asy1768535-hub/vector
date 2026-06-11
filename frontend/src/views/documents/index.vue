<script setup lang="ts">
import { computed, h, onMounted, reactive, ref, watch } from 'vue';
import { NButton, NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';
import { useLibraryOptions } from '@/hooks/use-libs';

defineOptions({ name: 'DocumentsPage' });

const { options, isSuper, loadFor, can } = useLibraryOptions();
const slug = ref<string | null>(null);
const docs = ref<any[]>([]);
const stats = ref<any>(null);
const loading = ref(false);

const canInsert = computed(() => isSuper.value || (slug.value ? can(slug.value, 'insert') : false));
const canDelete = computed(() => isSuper.value || (slug.value ? can(slug.value, 'delete') : false));

async function loadDocs() {
  if (!slug.value) return;
  loading.value = true;
  const [d, s]: any = await Promise.all([biz.fetchDocuments(slug.value, { limit: 200 }), biz.fetchLibraryStats(slug.value)]);
  loading.value = false;
  docs.value = d.data || [];
  stats.value = s.data || null;
}
watch(slug, loadDocs);

const modal = reactive<any>({ show: false, form: {} });
function openIngest() {
  modal.form = { title: '', external_id: '', text: '', splitter: 'text', metadata_json: '' };
  modal.show = true;
}
async function submitIngest() {
  if (!modal.form.text.trim()) { window.$message?.warning('请输入正文'); return; }
  let metadata = null;
  if (modal.form.metadata_json.trim()) {
    try { metadata = JSON.parse(modal.form.metadata_json); } catch { window.$message?.error('metadata 不是合法 JSON'); return; }
  }
  const body = { title: modal.form.title || null, external_id: modal.form.external_id || null, text: modal.form.text, splitter: modal.form.splitter, metadata };
  const { data, error }: any = await biz.ingestDocument(slug.value as string, body);
  if (error) return;
  window.$message?.success(`已入队 ${data.chunk_count} 个分片`);
  modal.show = false;
  loadDocs();
}
function del(row: any) {
  window.$dialog?.error({
    title: '删除文档', content: `删除文档 "${row.title || row.id.slice(0, 8)}"？`,
    positiveText: '删除', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.deleteDocument(slug.value as string, row.id); if (!error) { window.$message?.success('已删除'); loadDocs(); } }
  });
}

const columns: any[] = [
  { title: 'ID', key: 'id', width: 100, render: (r: any) => h('span', { class: 'mono' }, `${r.id.slice(0, 8)}…`) },
  { title: '标题', key: 'title', minWidth: 180 },
  { title: 'external_id', key: 'external_id', width: 140 },
  { title: '状态', key: 'status', width: 100, render: (r: any) => h(NTag, { type: r.status === 'ready' ? 'success' : r.status === 'failed' ? 'error' : 'warning', size: 'small' }, { default: () => r.status }) },
  { title: 'content_hash', key: 'content_hash', width: 130, render: (r: any) => h('span', { class: 'mono' }, `${(r.content_hash || '').slice(0, 12)}…`) },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'actions', width: 90, fixed: 'right', render: (r: any) => h(NButton, { size: 'small', type: 'error', disabled: !canDelete.value, onClick: () => del(r) }, { default: () => '删除' }) }
];

onMounted(async () => {
  await loadFor('read');
  if (options.value.length) slug.value = options.value[0].value;
});
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <NSpace align="center">
          <h3 style="margin: 0">文档</h3>
          <NSelect v-model:value="slug" :options="options" placeholder="选择库" style="width: 240px" />
        </NSpace>
        <NSpace>
          <NButton :loading="loading" @click="loadDocs">刷新</NButton>
          <NButton type="primary" :disabled="!canInsert" @click="openIngest">提交文档</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NGrid v-if="stats" :x-gap="16" :y-gap="16" :cols="4" responsive="screen">
      <NGi><NCard :bordered="false" class="card-wrapper"><div class="stat-label">文档数</div><div class="stat-value">{{ stats.document_count }}</div></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><div class="stat-label">分片数</div><div class="stat-value">{{ stats.chunk_count }}</div></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><div class="stat-label">排队中</div><div class="stat-value">{{ stats.pending_jobs }}</div></NCard></NGi>
      <NGi><NCard :bordered="false" class="card-wrapper"><div class="stat-label">失败</div><div class="stat-value" style="color: #e88080">{{ stats.failed_jobs }}</div></NCard></NGi>
    </NGrid>

    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="docs" :loading="loading" :scroll-x="1000" size="small" />
    </NCard>

    <NModal v-model:show="modal.show" preset="card" :title="'向 ' + slug + ' 提交文档'" style="width: 640px">
      <NForm label-placement="left" :label-width="90">
        <NFormItem label="标题"><NInput v-model:value="modal.form.title" /></NFormItem>
        <NFormItem label="external_id"><NInput v-model:value="modal.form.external_id" placeholder="可选；用于追踪" /></NFormItem>
        <NFormItem label="切分方式">
          <NRadioGroup v-model:value="modal.form.splitter">
            <NRadio value="text">text</NRadio><NRadio value="markdown">markdown</NRadio><NRadio value="none">none</NRadio>
          </NRadioGroup>
        </NFormItem>
        <NFormItem label="metadata"><NInput v-model:value="modal.form.metadata_json" type="textarea" :rows="2" placeholder='可选 JSON，如 {&quot;author&quot;:&quot;...&quot;,&quot;year&quot;:2024}' /></NFormItem>
        <NFormItem label="正文" required><NInput v-model:value="modal.form.text" type="textarea" :rows="8" placeholder="粘贴文本 / Markdown / JSON" /></NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end"><NButton @click="modal.show = false">取消</NButton><NButton type="primary" @click="submitIngest">提交（异步 embed）</NButton></NSpace>
      </template>
    </NModal>
  </NSpace>
</template>

<style scoped>
.mono { font-family: Consolas, monospace; font-size: 12px; }
.stat-label { color: #888; font-size: 13px; }
.stat-value { font-size: 26px; font-weight: 700; margin-top: 6px; }
</style>
