<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue';
import { NCheckbox } from 'naive-ui';
import * as biz from '@/service/api/biz';

defineOptions({ name: 'PermissionsPage' });

const ACTIONS = ['read', 'insert', 'delete'];
const users = ref<any[]>([]);
const libs = ref<any[]>([]);
const selectedUser = ref<string | null>(null);
const matrix = reactive<any>({});
const initial = reactive<any>({});
const loading = ref(false);
const userOptions = ref<any[]>([]);

async function load() {
  loading.value = true;
  const [u, l]: any = await Promise.all([biz.fetchUsers({ limit: 500 }), biz.fetchLibraries({ limit: 500 })]);
  loading.value = false;
  users.value = u.data || [];
  libs.value = (l.data || []).filter((x: any) => !x.deleted_at);
  userOptions.value = users.value.map((x: any) => ({ label: (x.username || x.email) + (x.is_superuser ? ' [超管]' : ''), value: x.id }));
}

async function reloadPerms() {
  if (!selectedUser.value) return;
  const { data }: any = await biz.fetchUserPerms(selectedUser.value);
  const next: any = {};
  for (const lib of libs.value) next[lib.slug] = { read: false, insert: false, delete: false };
  for (const row of data || []) {
    if (!next[row.library_slug]) continue;
    for (const a of row.actions) if (ACTIONS.includes(a)) next[row.library_slug][a] = true;
  }
  Object.keys(matrix).forEach(k => delete matrix[k]);
  Object.assign(matrix, next);
  Object.keys(initial).forEach(k => delete initial[k]);
  for (const slug of Object.keys(next)) initial[slug] = { ...next[slug] };
}

function onUserChange(v: string) {
  selectedUser.value = v;
  reloadPerms();
}

async function save() {
  if (!selectedUser.value) { window.$message?.warning('请先选择用户'); return; }
  const grants: any = {};
  const revokes: any = {};
  for (const slug of Object.keys(matrix)) {
    const cur = matrix[slug];
    const old = initial[slug] || { read: false, insert: false, delete: false };
    for (const a of ACTIONS) {
      if (cur[a] && !old[a]) (grants[slug] ||= []).push(a);
      else if (!cur[a] && old[a]) (revokes[slug] ||= []).push(a);
    }
  }
  for (const [slug, acts] of Object.entries(grants)) await biz.grantPerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
  for (const [slug, acts] of Object.entries(revokes)) await biz.revokePerms({ user_id: selectedUser.value, library_slug: slug, actions: acts });
  window.$message?.success(`已保存（授权 ${Object.values(grants).flat().length} / 撤销 ${Object.values(revokes).flat().length}）`);
  reloadPerms();
}

const columns: any[] = [
  { title: '库唯一ID', key: 'slug', width: 200 },
  { title: '名称', key: 'name' },
  { title: 'read 读取', key: 'read', width: 110, align: 'center', render: (r: any) => (matrix[r.slug] ? h(NCheckbox, { checked: matrix[r.slug].read, 'onUpdate:checked': (v: boolean) => (matrix[r.slug].read = v) }) : null) },
  { title: 'insert 写入', key: 'insert', width: 110, align: 'center', render: (r: any) => (matrix[r.slug] ? h(NCheckbox, { checked: matrix[r.slug].insert, 'onUpdate:checked': (v: boolean) => (matrix[r.slug].insert = v) }) : null) },
  { title: 'delete 删除', key: 'delete', width: 110, align: 'center', render: (r: any) => (matrix[r.slug] ? h(NCheckbox, { checked: matrix[r.slug].delete, 'onUpdate:checked': (v: boolean) => (matrix[r.slug].delete = v) }) : null) }
];

onMounted(load);
</script>

<template>
  <NSpace vertical :size="16">
    <NCard :bordered="false" class="card-wrapper">
      <NSpace justify="space-between" align="center">
        <h3 style="margin: 0">权限矩阵</h3>
        <NButton type="primary" :disabled="!selectedUser" @click="save">保存变更</NButton>
      </NSpace>
    </NCard>

    <NCard :bordered="false" class="card-wrapper">
      <NSpace align="center">
        <span>选择用户：</span>
        <NSelect :value="selectedUser" :options="userOptions" filterable placeholder="点击选择" style="width: 340px" @update:value="onUserChange" />
        <NTag v-if="selectedUser && users.find(u => u.id === selectedUser)?.is_superuser" type="warning">
          该用户为超管，所有库直通；下表仅作显式记录
        </NTag>
      </NSpace>
    </NCard>

    <NCard v-if="selectedUser" :bordered="false" class="card-wrapper">
      <NDataTable :columns="columns" :data="libs" :loading="loading" size="small" />
    </NCard>
    <NEmpty v-else description="请先选择一个用户" style="margin-top: 40px" />
  </NSpace>
</template>
