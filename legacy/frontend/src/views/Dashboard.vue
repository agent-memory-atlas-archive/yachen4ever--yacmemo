<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NGrid, NGridItem, NStatistic, NDataTable, NTag, NSpace, NButton } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useRouter } from 'vue-router'
import { api, type UserStats } from '../api'

const router = useRouter()
const users = ref<UserStats[]>([])
const loading = ref(true)

const totalNodes = ref(0)
const totalEvents = ref(0)
const totalPending = ref(0)

const columns: DataTableColumns<UserStats> = [
  { title: '用户', key: 'id', width: 100 },
  { title: '显示名', key: 'display_name', width: 100 },
  { title: 'MD 文件', key: 'md_files', width: 80 },
  { title: '有效实体', key: 'valid_nodes', width: 80 },
  { title: '事件', key: 'events', width: 60 },
  { title: '已处理', key: 'processed', width: 60 },
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
    title: '自定义 Key',
    key: 'has_custom_key',
    width: 80,
    render(row) {
      return h(NTag, { type: row.has_custom_key ? 'success' : 'default', size: 'small' },
        { default: () => row.has_custom_key ? '是' : '否' })
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 180,
    render(row) {
      return h(NSpace, { size: 'small' }, {
        default: () => [
          h(NButton, { size: 'small', text: true, type: 'primary',
            onClick: () => router.push(`/memory/${row.id}`) }, { default: () => '浏览' }),
          h(NButton, { size: 'small', text: true, type: 'primary',
            onClick: () => router.push(`/search/${row.id}`) }, { default: () => '搜索' }),
          h(NButton, { size: 'small', text: true, type: 'primary',
            onClick: () => router.push(`/consistency/${row.id}`) }, { default: () => '校验' }),
        ],
      })
    },
  },
]

import { h } from 'vue'

async function load() {
  loading.value = true
  try {
    const resp = await api.listUsers()
    users.value = resp.users
    totalNodes.value = resp.users.reduce((s, u) => s + (u.valid_nodes ?? 0), 0)
    totalEvents.value = resp.users.reduce((s, u) => s + (u.events ?? 0), 0)
    totalPending.value = resp.users.reduce((s, u) => s + (u.pending_consistency ?? 0), 0)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <n-space vertical :size="20">
    <h1 style="margin: 0;">仪表盘</h1>

    <n-grid :cols="4" :x-gap="12" :y-gap="12">
      <n-grid-item>
        <n-card><n-statistic label="用户" :value="users.length" /></n-card>
      </n-grid-item>
      <n-grid-item>
        <n-card><n-statistic label="有效实体" :value="totalNodes" /></n-card>
      </n-grid-item>
      <n-grid-item>
        <n-card><n-statistic label="事件" :value="totalEvents" /></n-card>
      </n-grid-item>
      <n-grid-item>
        <n-card><n-statistic label="待确认矛盾" :value="totalPending" /></n-card>
      </n-grid-item>
    </n-grid>

    <n-card title="用户概览">
      <n-data-table
        :columns="columns"
        :data="users"
        :loading="loading"
        :bordered="false"
        size="small"
      />
    </n-card>
  </n-space>
</template>
