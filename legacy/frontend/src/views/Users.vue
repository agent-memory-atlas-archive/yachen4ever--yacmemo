<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import {
  NCard, NDataTable, NButton, NSpace, NTag, NModal, NForm, NFormItem,
  NInput, useMessage, useDialog,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { api, type UserStats } from '../api'

const message = useMessage()
const dialog = useDialog()

const users = ref<UserStats[]>([])
const loading = ref(true)

// Create user modal
const showCreate = ref(false)
const form = ref({
  id: '',
  display_name: '',
  memory_root: '',
  llm_api_key: '',
  embedding_api_key: '',
})

const columns: DataTableColumns<UserStats> = [
  { title: '用户 ID', key: 'id', width: 120, render: (r) => h('strong', null, r.id) },
  { title: '显示名', key: 'display_name', width: 100 },
  { title: 'MD 文件', key: 'md_files', width: 80 },
  { title: '有效实体', key: 'valid_nodes', width: 80 },
  { title: '事件', key: 'events', width: 60 },
  { title: '已处理', key: 'processed', width: 60 },
  {
    title: '失败',
    key: 'failed',
    width: 60,
    render(row) {
      const v = row.failed ?? 0
      if (row.error) return h(NTag, { type: 'error', size: 'small' }, { default: () => '错误' })
      return h(NTag, { type: v > 0 ? 'error' : 'success', size: 'small' }, { default: () => String(v) })
    },
  },
  {
    title: '待确认',
    key: 'pending_consistency',
    width: 70,
    render(row) {
      const v = row.pending_consistency ?? 0
      return h(NTag, { type: v > 0 ? 'warning' : 'success', size: 'small' }, { default: () => String(v) })
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 160,
    render(row) {
      return h(NSpace, { size: 'small' }, {
        default: () => [
          h(NButton, {
            size: 'small', text: true, type: 'info',
            onClick: () => triggerScan(row.id),
          }, { default: () => '触发扫描' }),
          h(NButton, {
            size: 'small', text: true, type: 'error',
            onClick: () => confirmDelete(row.id),
          }, { default: () => '删除' }),
        ],
      })
    },
  },
]

async function load() {
  loading.value = true
  try {
    const resp = await api.listUsers()
    users.value = resp.users
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function triggerScan(userId: string) {
  try {
    await api.triggerScan(userId)
    message.success(`已触发 ${userId} 的扫描`)
  } catch (e: any) {
    message.error(e.message)
  }
}

function confirmDelete(userId: string) {
  dialog.warning({
    title: '确认删除',
    content: `确定删除用户 "${userId}" 及其所有数据？此操作不可撤销。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.deleteUser(userId)
        message.success(`已删除用户 ${userId}`)
        await load()
      } catch (e: any) {
        message.error(e.message)
      }
    },
  })
}

async function handleCreate() {
  if (!form.value.id || !form.value.memory_root) {
    message.warning('用户 ID 和记忆目录不能为空')
    return
  }
  try {
    await api.createUser({
      id: form.value.id,
      display_name: form.value.display_name || form.value.id,
      memory_root: form.value.memory_root,
      llm_api_key: form.value.llm_api_key,
      embedding_api_key: form.value.embedding_api_key,
    })
    message.success(`已创建用户 ${form.value.id}`)
    showCreate.value = false
    form.value = { id: '', display_name: '', memory_root: '', llm_api_key: '', embedding_api_key: '' }
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

onMounted(load)
</script>

<template>
  <n-space vertical :size="20">
    <n-space justify="space-between" align="center">
      <h1 style="margin: 0;">用户管理</h1>
      <n-button type="primary" @click="showCreate = true">添加用户</n-button>
    </n-space>

    <n-card>
      <n-data-table
        :columns="columns"
        :data="users"
        :loading="loading"
        :bordered="false"
        size="small"
      />
    </n-card>

    <n-modal v-model:show="showCreate" preset="card" title="添加用户" style="width: 500px;">
      <n-form label-placement="top">
        <n-form-item label="用户 ID">
          <n-input v-model:value="form.id" placeholder="如：alice" />
        </n-form-item>
        <n-form-item label="显示名">
          <n-input v-model:value="form.display_name" placeholder="留空则用 ID" />
        </n-form-item>
        <n-form-item label="记忆目录">
          <n-input v-model:value="form.memory_root" placeholder="./data/alice/memory" />
        </n-form-item>
        <n-form-item label="LLM API Key（可选，覆盖全局）">
          <n-input v-model:value="form.llm_api_key" placeholder="留空则继承全局配置" />
        </n-form-item>
        <n-form-item label="Embedding API Key（可选）">
          <n-input v-model:value="form.embedding_api_key" placeholder="留空则继承全局配置" />
        </n-form-item>
        <n-space>
          <n-button type="primary" @click="handleCreate">创建</n-button>
          <n-button @click="showCreate = false">取消</n-button>
        </n-space>
      </n-form>
    </n-modal>
  </n-space>
</template>
