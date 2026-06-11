<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import { NButton, NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'ApiKeysPage' });

const keys = ref<any[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  const { data, error }: any = await biz.fetchApiKeys();
  loading.value = false;
  if (!error) keys.value = data || [];
}

const createModal = reactive<any>({ show: false, name: '' });
const reveal = reactive<any>({ show: false, plaintext: '' });

function openCreate() {
  createModal.name = '';
  createModal.show = true;
}
async function submit() {
  if (!createModal.name.trim()) { window.$message?.warning('请输入名称'); return; }
  const { data, error }: any = await biz.createApiKey(createModal.name);
  if (error) return;
  createModal.show = false;
  reveal.plaintext = data.plaintext_key;
  reveal.show = true;
  load();
}
function revoke(row: any) {
  window.$dialog?.warning({
    title: '撤销 Key', content: `撤销 ${row.name} (${row.key_prefix}…)？`,
    positiveText: '撤销', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.revokeApiKey(row.id); if (!error) { window.$message?.success('已撤销'); load(); } }
  });
}
function copyPlain() {
  navigator.clipboard.writeText(reveal.plaintext);
  window.$message?.success('已复制到剪贴板');
}

const columns: any[] = [
  { title: '名称', key: 'name', width: 180 },
  { title: '前缀', key: 'key_prefix', width: 170, render: (r: any) => h('span', { class: 'mono' }, `${r.key_prefix}…`) },
  { title: '状态', key: 'status', width: 100, render: (r: any) => h(NTag, { type: r.revoked_at ? 'default' : 'success', size: 'small' }, { default: () => (r.revoked_at ? '已撤销' : '有效') }) },
  { title: '最近使用', key: 'last_used_at', width: 180 },
  { title: '过期时间', key: 'expires_at', width: 170 },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'actions', width: 90, fixed: 'right', render: (r: any) => h(NButton, { size: 'small', type: 'error', disabled: !!r.revoked_at, onClick: () => revoke(r) }, { default: () => '撤销' }) }
];

onMounted(load);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <h3 style="margin: 0">我的 API Key</h3>
        <NSpace>
          <NButton :loading="loading" @click="load">刷新</NButton>
          <NButton type="primary" @click="openCreate">生成新 Key</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NAlert type="info" :bordered="false">API Key 仅在创建时返回明文一次，请妥善保存。撤销后立即失效。</NAlert>

    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="keys" :loading="loading" :scroll-x="1000" size="small" />
    </NCard>

    <NModal v-model:show="createModal.show" preset="card" title="生成新 API Key" style="width: 420px">
      <NForm label-placement="left" :label-width="60">
        <NFormItem label="名称"><NInput v-model:value="createModal.name" placeholder="例如：dify-prod / my-app" /></NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end"><NButton @click="createModal.show = false">取消</NButton><NButton type="primary" @click="submit">生成</NButton></NSpace>
      </template>
    </NModal>

    <NModal v-model:show="reveal.show" preset="card" title="新 Key（明文仅本次显示）" style="width: 560px">
      <NAlert type="warning" :bordered="false" style="margin-bottom: 12px">请立即复制并妥善保存。关闭后将无法再次查看明文。</NAlert>
      <div class="api-key-box">{{ reveal.plaintext }}</div>
      <template #footer>
        <NSpace justify="end"><NButton @click="copyPlain">复制到剪贴板</NButton><NButton type="primary" @click="reveal.show = false">我已保存</NButton></NSpace>
      </template>
    </NModal>
  </NSpace>
</template>

<style scoped>
.mono { font-family: Consolas, monospace; font-size: 12px; }
.api-key-box { padding: 12px 14px; background: rgba(100, 108, 255, 0.08); border: 1px dashed #646cff; border-radius: 8px; word-break: break-all; font-family: monospace; font-size: 13px; }
</style>
