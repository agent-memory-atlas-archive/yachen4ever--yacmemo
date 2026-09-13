<script setup lang="ts">
import { ref, onMounted, h, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  NCard, NDataTable, NTag, NSpace, NTree, NCode, NModal, NButton, NEmpty,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns, TreeOption } from 'naive-ui'
import { api, type EntityNode, type EntityEvent, type FileTreeItem } from '../api'

const route = useRoute()
const message = useMessage()
const userId = route.params.userId as string

const fileTree = ref<FileTreeItem[]>([])
const nodes = ref<EntityNode[]>([])
const events = ref<EntityEvent[]>([])
const loading = ref(true)

const fileContent = ref<string | null>(null)
const fileName = ref<string>('')
const showFile = ref(false)

const historyData = ref<any[]>([])
const showHistory = ref(false)
const historyName = ref('')

function toTreeOptions(items: FileTreeItem[]): TreeOption[] {
  return items.map(item => {
    if (item.type === 'dir') {
      return {
        key: item.path,
        label: item.name,
        children: toTreeOptions(item.children ?? []),
        isLeaf: false,
      }
    }
    return {
      key: item.path,
      label: item.name,
      isLeaf: true,
    }
  })
}

async function loadFile(path: string) {
  try {
    const resp = await api.readFile(userId, path)
    fileContent.value = resp.content
    fileName.value = path
    showFile.value = true
  } catch (e: any) {
    message.error(e.message)
  }
}

async function loadHistory(name: string) {
  try {
    const resp = await api.getHistory(userId, name)
    historyData.value = resp.history
    historyName.value = name
    showHistory.value = true
  } catch (e: any) {
    message.error(e.message)
  }
}

const nodeColumns: DataTableColumns<EntityNode> = [
  { title: '名称', key: 'name', width: 120, render: (r) => h('strong', null, r.name) },
  { title: '类型', key: 'type', width: 80 },
  { title: '摘要', key: 'summary', width: 200, render: (r) => r.summary ?? '-' },
  { title: '来源', key: 'source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.source_path) },
  {
    title: '状态',
    key: 'valid',
    width: 70,
    render: (r) => h(NTag, { type: r.valid ? 'success' : 'error', size: 'small' },
      { default: () => r.valid ? '有效' : '已失效' }),
  },
  { title: '创建', key: 'created_at', width: 100, render: (r) => r.created_at?.slice(0, 10) ?? '-' },
  {
    title: '历史',
    key: 'actions',
    width: 60,
    render: (r) => h(NButton, { size: 'small', text: true, type: 'primary',
      onClick: () => loadHistory(r.name) }, { default: () => '查看' }),
  },
]

const eventColumns: DataTableColumns<EntityEvent> = [
  { title: '日期', key: 'date', width: 100, render: (r) => r.date ?? '-' },
  { title: '类型', key: 'type', width: 80, render: (r) => h(NTag, { size: 'small' }, { default: () => r.type ?? '-' }) },
  { title: '描述', key: 'summary', width: 300 },
  { title: '来源', key: 'source_path', width: 150, render: (r) => h('span', { style: 'font-size: 11px; color: #888' }, r.source_path) },
]

const historyColumns: DataTableColumns = [
  {
    title: '状态',
    key: 'valid',
    width: 70,
    render: (row: any) => h(NTag, { type: row.valid ? 'success' : 'error', size: 'small' },
      { default: () => row.valid ? '有效' : '已失效' }),
  },
  { title: '名称', key: 'name', width: 100 },
  { title: '类型', key: 'type', width: 80 },
  { title: '摘要', key: 'summary', width: 200, render: (r: any) => r.summary ?? '-' },
  { title: '创建', key: 'created_at', width: 100, render: (r: any) => r.created_at?.slice(0, 10) ?? '-' },
  { title: '来源', key: 'source_path', width: 150, render: (r: any) => h('span', { style: 'font-size: 11px; color: #888' }, r.source_path) },
]

async function load() {
  loading.value = true
  try {
    const resp = await api.getMemory(userId)
    fileTree.value = resp.file_tree
    nodes.value = resp.nodes
    events.value = resp.events
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

watch(() => route.params.userId, (newUid) => {
  if (newUid) {
    load()
  }
})

onMounted(load)
</script>

<template>
  <n-space vertical :size="20">
    <h1 style="margin: 0;">记忆浏览 · {{ userId }}</h1>

    <n-card title="文件树">
      <n-tree
        :options="toTreeOptions(fileTree)"
        block-line
        expand-on-click
        @update:selectedKeys="(keys: string[]) => { if (keys.length) loadFile(keys[0]) }"
      />
    </n-card>

    <n-card title="实体列表" >
      <template #header-extra>{{ nodes.length }}</template>
      <n-data-table :columns="nodeColumns" :data="nodes" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <n-card title="事件列表">
      <template #header-extra>{{ events.length }}</template>
      <n-data-table :columns="eventColumns" :data="events" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <!-- File content modal -->
    <n-modal v-model:show="showFile" preset="card" :title="fileName" style="width: 700px;">
      <n-code :code="fileContent ?? ''" language="markdown" word-wrap />
    </n-modal>

    <!-- History modal -->
    <n-modal v-model:show="showHistory" preset="card" :title="`实体历史 · ${historyName}`" style="width: 700px;">
      <n-data-table :columns="historyColumns" :data="historyData" :bordered="false" size="small" />
    </n-modal>
  </n-space>
</template>
