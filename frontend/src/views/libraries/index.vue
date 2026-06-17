<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import { NButton, NSpace, NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'LibrariesPage' });

const loading = ref(false);
const showDeleted = ref(false);
const rows = ref<any[]>([]);

async function load() {
  loading.value = true;
  const { data, error }: any = await biz.fetchLibraries({ include_deleted: showDeleted.value ? 'true' : 'false', limit: 500 });
  loading.value = false;
  if (error) return;
  rows.value = data || [];
}

function srcSummary(cfg: any) {
  if (!cfg) return '';
  const tbl = cfg.db_name ? `${cfg.db_name}.${cfg.table}` : cfg.table;
  return `${tbl} · ${cfg.key_field}→${cfg.text_column}`;
}

const createModal = reactive<any>({ show: false, form: {} });
function openCreate() {
  createModal.form = { slug: '', name: '', description: '', embedding_model: '', embedding_dim: null, vector_distance: 'cosine', embedding_base_url: '', chunk_size: 1000, chunk_overlap: 120 };
  createModal.show = true;
}
async function submitCreate() {
  const body: any = { ...createModal.form };
  for (const k of ['embedding_model', 'embedding_base_url']) if (!body[k]) body[k] = null;
  if (!body.embedding_dim) body.embedding_dim = null;
  const { error }: any = await biz.createLibrary(body);
  if (error) return;
  window.$message?.success('库已创建并已建 Qdrant collection');
  createModal.show = false;
  load();
}

const editModal = reactive<any>({ show: false, slug: '', form: {}, initial: {} });
function openEdit(row: any) {
  editModal.slug = row.slug;
  const snap = { name: row.name, description: row.description || '', embedding_model: row.embedding_model, embedding_dim: row.embedding_dim, vector_distance: row.vector_distance, embedding_base_url: row.embedding_base_url || '', chunk_size: row.chunk_size, chunk_overlap: row.chunk_overlap };
  editModal.form = { ...snap };
  editModal.initial = { ...snap };
  editModal.show = true;
}
async function submitEdit() {
  const diff: any = {};
  for (const k of Object.keys(editModal.form)) if (editModal.form[k] !== editModal.initial[k]) diff[k] = editModal.form[k];
  if (!Object.keys(diff).length) { window.$message?.info('未做任何修改'); editModal.show = false; return; }
  const { error }: any = await biz.updateLibrary(editModal.slug, diff);
  if (error) return;
  window.$message?.success('已保存');
  editModal.show = false;
  load();
}

function rebuild(row: any) {
  window.$dialog?.warning({
    title: '重建 collection',
    content: `确认重建 ${row.slug} 的 Qdrant collection？会删旧建新，并把该库所有文档重置为 pending 重新 embed。`,
    positiveText: '确认重建', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.rebuildLibrary(row.slug); if (!error) { window.$message?.success('已重建并重置 jobs'); load(); } }
  });
}
function del(row: any) {
  window.$dialog?.error({
    title: '删除库', content: `软删除库 ${row.slug}？Qdrant collection 也会异步清理。`,
    positiveText: '删除', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.deleteLibrary(row.slug); if (!error) { window.$message?.success('已删除'); load(); } }
  });
}

const columns: any[] = [
  { title: '库唯一ID', key: 'slug', width: 150 },
  { title: '名称', key: 'name', width: 130 },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
  { title: '向量参数', key: 'vec', width: 190, render: (r: any) => h('span', { class: 'mono' }, `${r.embedding_model} / ${r.embedding_dim}d / ${r.vector_distance}`) },
  { title: '切分', key: 'chunk', width: 100, render: (r: any) => `${r.chunk_size} / ${r.chunk_overlap}` },
  { title: 'Qdrant Collection', key: 'qdrant_collection', width: 170, render: (r: any) => h('span', { class: 'mono' }, r.qdrant_collection) },
  { title: '全文源(PGSQL)', key: 'src', width: 230, ellipsis: { tooltip: true }, render: (r: any) => r.source_config ? h(NTag, { type: 'success', size: 'small', bordered: false }, { default: () => srcSummary(r.source_config) }) : h('span', { style: 'color:#999' }, '向量自带正文') },
  { title: '状态', key: 'status', width: 90, render: (r: any) => h(NTag, { type: r.deleted_at ? 'default' : 'success', size: 'small' }, { default: () => (r.deleted_at ? '已删' : '正常') }) },
  {
    title: '操作', key: 'actions', width: 210, fixed: 'right', render: (r: any) => h(NSpace, { size: 6 }, {
      default: () => [
        h(NButton, { size: 'small', disabled: !!r.deleted_at, onClick: () => openEdit(r) }, { default: () => '编辑' }),
        h(NButton, { size: 'small', type: 'warning', disabled: !!r.deleted_at, onClick: () => rebuild(r) }, { default: () => '重建' }),
        h(NButton, { size: 'small', type: 'error', disabled: !!r.deleted_at, onClick: () => del(r) }, { default: () => '删除' })
      ]
    })
  }
];

onMounted(load);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <NSpace align="center">
          <h3 style="margin: 0">库管理</h3>
          <NCheckbox v-model:checked="showDeleted" @update:checked="load">显示已删除</NCheckbox>
        </NSpace>
        <NSpace>
          <NButton :loading="loading" @click="load">刷新</NButton>
          <NButton type="primary" @click="openCreate">新建库</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="rows" :loading="loading" :scroll-x="1400" size="small" />
    </NCard>

    <NModal v-model:show="createModal.show" preset="card" title="新建库" style="width: 560px">
      <NForm label-placement="left" :label-width="100">
        <NFormItem label="库唯一ID" required>
          <NInput v-model:value="createModal.form.slug" placeholder="只能大小写字母和下划线，如 medical / legal_cn" />
        </NFormItem>
        <NFormItem label="名称" required><NInput v-model:value="createModal.form.name" /></NFormItem>
        <NFormItem label="描述"><NInput v-model:value="createModal.form.description" type="textarea" :rows="2" /></NFormItem>
        <NFormItem label="向量模型"><NInput v-model:value="createModal.form.embedding_model" placeholder="默认：bge-m3" /></NFormItem>
        <NFormItem label="向量维度"><NInputNumber v-model:value="createModal.form.embedding_dim" :min="64" :max="8192" placeholder="默认：1024" style="width: 100%" /></NFormItem>
        <NFormItem label="模型接口地址"><NInput v-model:value="createModal.form.embedding_base_url" placeholder="默认：http://10.0.10.2:8111/v1/embeddings" /></NFormItem>
        <NFormItem label="分片大小"><NInputNumber v-model:value="createModal.form.chunk_size" :min="200" :max="8000" style="width: 100%" /></NFormItem>
        <NFormItem label="分片重叠"><NInputNumber v-model:value="createModal.form.chunk_overlap" :min="0" :max="2000" style="width: 100%" /></NFormItem>
      </NForm>
      <NAlert type="info" :bordered="false" :show-icon="false">
        PGSQL 全文源按<b>约定</b>自动配置：源表 = 本库唯一ID，外键 <code>text_id</code> → 正文列 <code>content</code>，类型 bigint，源库取 .env。
      </NAlert>
      <template #footer>
        <NSpace justify="end"><NButton @click="createModal.show = false">取消</NButton><NButton type="primary" @click="submitCreate">创建</NButton></NSpace>
      </template>
    </NModal>

    <NModal v-model:show="editModal.show" preset="card" :title="'编辑库 / ' + editModal.slug" style="width: 560px">
      <NAlert type="warning" :bordered="false" style="margin-bottom: 12px">
        改向量维度后需点表格「重建」。改向量模型/接口只影响之后摄入的文档。
      </NAlert>
      <NForm label-placement="left" :label-width="100">
        <NFormItem label="名称"><NInput v-model:value="editModal.form.name" /></NFormItem>
        <NFormItem label="描述"><NInput v-model:value="editModal.form.description" type="textarea" :rows="2" /></NFormItem>
        <NFormItem label="向量模型"><NInput v-model:value="editModal.form.embedding_model" /></NFormItem>
        <NFormItem label="向量维度"><NInputNumber v-model:value="editModal.form.embedding_dim" :min="64" :max="8192" style="width: 100%" /></NFormItem>
        <NFormItem label="模型接口地址"><NInput v-model:value="editModal.form.embedding_base_url" /></NFormItem>
        <NFormItem label="分片大小"><NInputNumber v-model:value="editModal.form.chunk_size" :min="200" :max="8000" style="width: 100%" /></NFormItem>
        <NFormItem label="分片重叠"><NInputNumber v-model:value="editModal.form.chunk_overlap" :min="0" :max="2000" style="width: 100%" /></NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end"><NButton @click="editModal.show = false">取消</NButton><NButton type="primary" @click="submitEdit">保存</NButton></NSpace>
      </template>
    </NModal>
  </NSpace>
</template>

<style scoped>
.mono {
  font-family: Consolas, monospace;
  font-size: 12px;
}
</style>
