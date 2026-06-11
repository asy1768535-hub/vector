<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import { NButton, NSpace, NTag } from 'naive-ui';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'UsersPage' });

const loading = ref(false);
const rows = ref<any[]>([]);

async function load() {
  loading.value = true;
  const { data, error }: any = await biz.fetchUsers({ include_deleted: 'true', limit: 500 });
  loading.value = false;
  if (!error) rows.value = data || [];
}

const createModal = reactive<any>({ show: false, form: {} });
function openCreate() {
  createModal.form = { email: '', password: '', username: '', display_name: '', is_superuser: false };
  createModal.show = true;
}
async function submitCreate() {
  const { error }: any = await biz.createUser(createModal.form);
  if (error) return;
  window.$message?.success('用户已创建');
  createModal.show = false;
  load();
}

async function toggleActive(u: any) {
  const { error }: any = await biz.updateUser(u.id, { is_active: !u.is_active });
  if (!error) { window.$message?.success('已更新'); load(); }
}
function toggleSuper(u: any) {
  window.$dialog?.warning({
    title: '确认', content: `确认${u.is_superuser ? '取消' : '授予'} ${u.email} 的超管权限？`,
    positiveText: '确认', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.updateUser(u.id, { is_superuser: !u.is_superuser }); if (!error) { window.$message?.success('已更新'); load(); } }
  });
}
function disable(u: any) {
  window.$dialog?.error({
    title: '禁用用户', content: `确认禁用 ${u.email}？`,
    positiveText: '禁用', negativeText: '取消',
    onPositiveClick: async () => { const { error }: any = await biz.disableUser(u.id); if (!error) { window.$message?.success('已禁用'); load(); } }
  });
}

const columns: any[] = [
  { title: '邮箱', key: 'email', minWidth: 200 },
  { title: '用户名', key: 'username', width: 120 },
  { title: '显示名', key: 'display_name', width: 130 },
  { title: '角色', key: 'role', width: 90, render: (r: any) => h(NTag, { type: r.is_superuser ? 'error' : 'info', size: 'small' }, { default: () => (r.is_superuser ? '超管' : '普通') }) },
  { title: '状态', key: 'status', width: 90, render: (r: any) => h(NTag, { type: r.is_active ? 'success' : 'default', size: 'small' }, { default: () => (r.is_active ? '启用' : '禁用') }) },
  { title: '创建时间', key: 'created_at', width: 180 },
  {
    title: '操作', key: 'actions', width: 250, fixed: 'right', render: (r: any) => h(NSpace, { size: 6 }, {
      default: () => [
        h(NButton, { size: 'small', onClick: () => toggleActive(r) }, { default: () => (r.is_active ? '禁用' : '启用') }),
        h(NButton, { size: 'small', type: 'warning', onClick: () => toggleSuper(r) }, { default: () => (r.is_superuser ? '取消超管' : '设为超管') }),
        h(NButton, { size: 'small', type: 'error', disabled: !r.is_active, onClick: () => disable(r) }, { default: () => '软删' })
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
        <h3 style="margin: 0">用户管理</h3>
        <NSpace>
          <NButton :loading="loading" @click="load">刷新</NButton>
          <NButton type="primary" @click="openCreate">新建用户</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NCard :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="rows" :loading="loading" :scroll-x="1100" size="small" />
    </NCard>

    <NModal v-model:show="createModal.show" preset="card" title="新建用户" style="width: 480px">
      <NForm label-placement="left" :label-width="90">
        <NFormItem label="邮箱" required><NInput v-model:value="createModal.form.email" placeholder="user@example.com" /></NFormItem>
        <NFormItem label="初始密码" required><NInput v-model:value="createModal.form.password" type="password" show-password-on="click" /></NFormItem>
        <NFormItem label="用户名"><NInput v-model:value="createModal.form.username" /></NFormItem>
        <NFormItem label="显示名"><NInput v-model:value="createModal.form.display_name" /></NFormItem>
        <NFormItem label="超级管理员"><NSwitch v-model:value="createModal.form.is_superuser" /></NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end"><NButton @click="createModal.show = false">取消</NButton><NButton type="primary" @click="submitCreate">创建</NButton></NSpace>
      </template>
    </NModal>
  </NSpace>
</template>
