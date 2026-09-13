<script setup lang="ts">
import { ref, onMounted, watch, h } from 'vue'
import { useRoute } from 'vue-router'
import {
  NCard, NDataTable, NTag, NButton, NSpace, NEmpty, useMessage, useDialog,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  api, type ConsistencyPending, type ConsistencyAutoLog, type InvalidatedNode,
} from '../api'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()
const userId = route.params.userId as string

const pending = ref<ConsistencyPending[]>([])
const autoLogs = ref<ConsistencyAutoLog[]>([])
const invalidated = ref<InvalidatedNode[]>([])
const loading = ref(true)

const pendingColumns: DataTableColumns<ConsistencyPending> = [
  { title: 'ID', key: 'id', width: 70, render: (r) => r.id.slice(0, 8) },
  { title: '旧来源', key: 'old_source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.old_source_path) },
  { title: '新来源', key: 'new_source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.new_source_path) },
  { title: '理由', key: 'reason', width: 200 },
  {
    title: '置信度',
    key: 'confidence',
    width: 80,
    render: (r) => h(NTag, {
      type: r.confidence >= 0.8 ? 'success' : 'warning',
      size: 'small',
    }, { default: () => r.confidence.toFixed(2) }),
  },
  { title: '时间', key: 'checked_at', width: 100, render: (r) => r.checked_at?.slice(0, 10) ?? '-' },
  {
    title: '操作',
    key: 'actions',
    width: 160,
    render: (r) => h(NSpace, { size: 'small' }, {
      default: () => [
        h(NButton, {
          size: 'small', text: true, type: 'warning',
          onClick: () => resolveItem(r.id, 'confirm'),
        }, { default: () => '确认失效' }),
        h(NButton, {
          size: 'small', text: true,
          onClick: () => resolveItem(r.id, 'dismiss'),
        }, { default: () => '忽略' }),
      ],
    }),
  },
]

const autoLogColumns: DataTableColumns<ConsistencyAutoLog> = [
  { title: '旧来源', key: 'old_source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.old_source_path) },
  { title: '新来源', key: 'new_source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.new_source_path) },
  { title: '理由', key: 'reason', width: 200 },
  { title: '置信度', key: 'confidence', width: 80, render: (r) => r.confidence.toFixed(2) },
  { title: '时间', key: 'checked_at', width: 100, render: (r) => r.checked_at?.slice(0, 10) ?? '-' },
]

const invalidatedColumns: DataTableColumns<InvalidatedNode> = [
  { title: '名称', key: 'name', width: 120, render: (r) => h('strong', null, r.name) },
  { title: '类型', key: 'type', width: 80 },
  { title: '摘要', key: 'summary', width: 200, render: (r) => r.summary ?? '-' },
  { title: '失效时间', key: 'invalid_at', width: 100, render: (r) => r.invalid_at?.slice(0, 10) ?? '-' },
  { title: '失效原因', key: 'invalid_reason', width: 200, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.invalid_reason ?? '-') },
  { title: '来源', key: 'source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.source_path) },
]

async function resolveItem(logId: string, action: string) {
  dialog.warning({
    title: action === 'confirm' ? '确认旧事实失效' : '忽略此矛盾',
    content: action === 'confirm' ? '旧事实将被标记为已失效。' : '此矛盾将被忽略，不执行失效操作。',
    positiveText: '确认',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const resp = await api.resolveConsistency(userId, logId, action)
        message.success(resp.message ?? '操作完成')
        await load()
      } catch (e: any) {
        message.error(e.message)
      }
    },
  })
}

async function load() {
  loading.value = true
  try {
    const resp = await api.getConsistency(userId)
    pending.value = resp.pending
    autoLogs.value = resp.auto_logs
    invalidated.value = resp.invalidated
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

watch(() => route.params.userId, () => load())
onMounted(load)
</script>

<template>
  <n-space vertical :size="20">
    <h1 style="margin: 0;">一致性校验 · {{ userId }}</h1>

    <n-card title="待确认矛盾">
      <template #header-extra>{{ pending.length }}</template>
      <n-data-table v-if="pending.length" :columns="pendingColumns" :data="pending" :loading="loading" :bordered="false" size="small" />
      <n-empty v-else description="无待确认项" />
    </n-card>

    <n-card title="自动失效历史">
      <template #header-extra>{{ autoLogs.length }}</template>
      <n-data-table v-if="autoLogs.length" :columns="autoLogColumns" :data="autoLogs" :loading="loading" :bordered="false" size="small" />
      <n-empty v-else description="无自动失效记录" />
    </n-card>

    <n-card title="已失效实体">
      <template #header-extra>{{ invalidated.length }}</template>
      <n-data-table v-if="invalidated.length" :columns="invalidatedColumns" :data="invalidated" :loading="loading" :bordered="false" size="small" />
      <n-empty v-else description="无已失效实体" />
    </n-card>
  </n-space>
</template>
