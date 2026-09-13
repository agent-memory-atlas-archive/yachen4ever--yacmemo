<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { NCard, NInput, NButton, NSpace, useMessage } from 'naive-ui'

const router = useRouter()
const message = useMessage()
const token = ref('')

async function doLogin() {
  if (!token.value) {
    message.warning('请输入 Token')
    return
  }
  // Submit as form data to match backend
  const form = new FormData()
  form.append('token', token.value)
  const resp = await fetch('/admin/api/login', { method: 'POST', body: form })
  if (resp.ok) {
    router.push('/')
  } else {
    message.error('Token 无效')
  }
}
</script>

<template>
  <div style="max-width: 400px; margin: 100px auto;">
    <n-card title="管理员登录">
      <n-space vertical>
        <n-input
          v-model:value="token"
          type="password"
          placeholder="管理员 Token"
          @keyup.enter="doLogin"
        />
        <n-button type="primary" block @click="doLogin">登录</n-button>
      </n-space>
    </n-card>
  </div>
</template>
