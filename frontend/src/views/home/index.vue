<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useAuthStore } from '@/store/modules/auth';
import { fetchHealth } from '@/service/api/system';

const authStore = useAuthStore();
const health = ref<any>(null);
const loading = ref(true);
const err = ref('');

onMounted(async () => {
  const { data, error }: any = await fetchHealth();
  if (error) {
    err.value = error.message || '探活失败';
  } else {
    health.value = data;
  }
  loading.value = false;
});
</script>

<template>
  <NSpace vertical :size="16">
    <NGrid :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGi span="24 s:24 m:8">
        <NCard :bordered="false" class="card-wrapper" title="当前用户">
          <NSpace vertical :size="12">
            <div><span class="label">邮箱：</span>{{ authStore.userInfo.email || authStore.userInfo.userName }}</div>
            <div>
              <span class="label">角色：</span>
              <NTag :type="authStore.userInfo.isSuperuser ? 'error' : 'info'" size="small" round>
                {{ authStore.userInfo.isSuperuser ? '超级管理员' : '普通用户' }}
              </NTag>
            </div>
          </NSpace>
        </NCard>
      </NGi>
      <NGi span="24 s:24 m:16">
        <NCard :bordered="false" class="card-wrapper" title="服务状态">
          <NSpin :show="loading">
            <NAlert v-if="err" type="error" :bordered="false">{{ err }}</NAlert>
            <NDescriptions v-else-if="health" :column="2" label-placement="left" bordered>
              <NDescriptionsItem label="API">
                <NTag :type="health.status === 'ok' ? 'success' : 'warning'" size="small">{{ health.status }}</NTag>
              </NDescriptionsItem>
              <NDescriptionsItem label="数据库">
                <NTag :type="health.db ? 'success' : 'error'" size="small">{{ health.db ? 'OK' : 'DOWN' }}</NTag>
              </NDescriptionsItem>
              <NDescriptionsItem label="Qdrant">
                <NTag :type="health.qdrant ? 'success' : 'error'" size="small">{{ health.qdrant ? 'OK' : 'DOWN' }}</NTag>
              </NDescriptionsItem>
              <NDescriptionsItem label="Embedding 模型">
                {{ health.embedding_model }} ({{ health.embedding_dim }} 维)
              </NDescriptionsItem>
            </NDescriptions>
          </NSpin>
        </NCard>
      </NGi>
    </NGrid>

    <NCard :bordered="false" class="card-wrapper" title="使用提示">
      <ol class="tips">
        <li>左侧菜单按角色显示：管理员可见全部，普通用户仅「文档 / 数据检索 / 我的 API Key」。</li>
        <li>外部调用（Dify 等）在「我的 API Key」生成 Bearer Key。</li>
        <li>管理员可在「权限矩阵」给用户授权 read / insert / delete 三种动作。</li>
        <li>Dify 检索端点：<NText code>POST /retrieval</NText>。</li>
      </ol>
    </NCard>
  </NSpace>
</template>

<style scoped>
.label {
  color: var(--n-text-color-3, #888);
}

.tips {
  margin: 0;
  padding-left: 20px;
  line-height: 2;
}
</style>
