<script setup lang="ts">
import { reactive } from 'vue';
import { useAuthStore } from '@/store/modules/auth';
import { useNaiveForm } from '@/hooks/common/form';

defineOptions({
  name: 'PwdLogin'
});

const authStore = useAuthStore();
const { formRef, validate } = useNaiveForm();

interface FormModel {
  userName: string;
  password: string;
}

// 本后端用邮箱 + 密码登录（fastapi-users），不预填、不使用演示账号
const model: FormModel = reactive({
  userName: '',
  password: ''
});

const rules = {
  userName: [{ required: true, message: '请输入邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
};

async function handleSubmit() {
  await validate();
  // authStore.login 的第一个参数作为 username（=邮箱）提交给后端
  await authStore.login(model.userName, model.password);
}
</script>

<template>
  <NForm ref="formRef" :model="model" :rules="rules" size="large" :show-label="false" @keyup.enter="handleSubmit">
    <NFormItem path="userName">
      <NInput v-model:value="model.userName" placeholder="邮箱，如 admin@example.com" />
    </NFormItem>
    <NFormItem path="password">
      <NInput v-model:value="model.password" type="password" show-password-on="click" placeholder="请输入密码" />
    </NFormItem>
    <NSpace vertical :size="24">
      <NButton type="primary" size="large" round block :loading="authStore.loginLoading" @click="handleSubmit">
        登 录
      </NButton>
    </NSpace>
  </NForm>
</template>

<style scoped></style>
