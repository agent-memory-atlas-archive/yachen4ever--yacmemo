<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NCard, NDataTable, NTag, NSpace, NStatistic, NGrid, NGridItem, NCode } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { api, type SystemStatus, type UserIndex } from '../api'

const status = ref<SystemStatus | null>(null)
const loading = ref(true)

const serviceColumns: DataTableColumns = [
  { title: '服务', key: 'name', width: 100 },
  {
    title: '状态',
    key: 'ok',
    width: 80,
    render: (row: any) => h(NTag, { type: row.ok ? 'success' : 'error', size: 'small' },
      { default: () => row.ok ? '正常' : '异常' }),
  },
  { title: '端点', key: 'url', width: 200 },
  { title: '模型', key: 'model', width: 200 },
  { title: '详情', key: 'detail', width: 200, render: (row: any) => h('span', { style: 'font-size: 12px; color: #888' }, row.detail) },
]

const services = ref<any[]>([])

const cronColumns: DataTableColumns = [
  { title: '任务', key: 'name', width: 120 },
  { title: 'Cron', key: 'cron', render: (row: any) => h(NCode, null, { default: () => row.cron }) },
]

const crons = ref<any[]>([])

const indexColumns: DataTableColumns<UserIndex> = [
  { title: '用户', key: 'user_id', width: 100 },
  { title: 'SQLite 大小', key: 'sqlite_size', width: 100 },
  { title: '实体', key: 'nodes', width: 60 },
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
]

async function load() {
  loading.value = true
  try {
    status.value = await api.getStatus()
    services.value = [
      { name: 'LLM', ok: status.value.llm_ok, url: status.value.llm_base_url, model: status.value.llm_model, detail: status.value.llm_detail },
      { name: 'Embedding', ok: status.value.emb_ok, url: status.value.emb_base_url, model: status.value.emb_model, detail: status.value.emb_detail },
    ]
    crons.value = [
      { name: '提取扫描', cron: status.value.extract_cron },
      { name: '一致性校验', cron: status.value.consistency_cron },
    ]
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <n-space vertical :size="20">
    <h1 style="margin: 0;">系统状态</h1>

    <n-grid :cols="2" :x-gap="12">
      <n-grid-item>
        <n-card>
          <n-statistic label="LLM 服务" :value="status?.llm_ok ? '✓ 正常' : '✗ 异常'" />
        </n-card>
      </n-grid-item>
      <n-grid-item>
        <n-card>
          <n-statistic label="Embedding 服务" :value="status?.emb_ok ? '✓ 正常' : '✗ 异常'" />
        </n-card>
      </n-grid-item>
    </n-grid>

    <n-card title="服务连通性">
      <n-data-table :columns="serviceColumns" :data="services" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <n-card title="定时任务">
      <n-data-table :columns="cronColumns" :data="crons" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <n-card title="索引健康">
      <n-data-table :columns="indexColumns" :data="status?.user_indexes ?? []" :loading="loading" :bordered="false" size="small" />
    </n-card>
  </n-space>
</template>
