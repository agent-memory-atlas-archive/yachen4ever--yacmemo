<template>
  <div class="settings-page">
    <n-tabs type="line" animated>
      <!-- 健康总览 -->
      <n-tab-pane name="health" tab="健康总览">
        <n-spin v-if="!overview" size="medium" />
        <template v-else>
          <n-card size="small" title="Embedding" style="margin-bottom: 12px">
            <n-descriptions :column="3" size="small" label-placement="left">
              <n-descriptions-item label="状态">
                <n-tag :type="overview.embedding.configured ? 'success' : 'warning'" size="small">
                  {{ overview.embedding.configured ? '已配置' : '未配置 (FTS-only)' }}
                </n-tag>
              </n-descriptions-item>
              <n-descriptions-item label="模型">{{ overview.embedding.model || '—' }}</n-descriptions-item>
              <n-descriptions-item label="维度">{{ overview.embedding.dimensions || '—' }}</n-descriptions-item>
            </n-descriptions>
          </n-card>

          <n-card v-for="u in overview.users" :key="u.id" size="small"
            :title="u.id" style="margin-bottom: 12px">
            <n-grid :cols="4" :x-gap="12" :y-gap="8" responsive="screen">
              <n-gi><n-statistic label="笔记" :value="u.note_count" /></n-gi>
              <n-gi><n-statistic label="撞车" :value="u.open_collisions" /></n-gi>
              <n-gi><n-statistic label="活跃主题" :value="u.topics.length" /></n-gi>
              <n-gi><n-statistic label="已归档" :value="u.archived_topics?.length || 0" /></n-gi>
            </n-grid>
            <n-divider style="margin: 8px 0" />
            <n-space size="small">
              <n-tag size="tiny">拒绝 {{ u.guard.refused }}</n-tag>
              <n-tag size="tiny" type="warning">force {{ u.guard.forced }}</n-tag>
              <n-tag size="tiny" :type="u.git_status?.includes('失败') ? 'error' : 'success'">
                {{ u.git_status ? u.git_status.slice(0, 30) : 'git: —' }}
              </n-tag>
            </n-space>
          </n-card>

          <n-card size="small" title="维护">
            <n-space align="center" justify="space-between">
              <n-text depth="3">
                全量重建派生索引（SQLite/LanceDB，从 markdown 重建）：索引异常或大规模外部改动后使用，日常无需执行。
              </n-text>
              <n-popconfirm @positive-click="runReindex">
                <template #trigger>
                  <n-button type="warning" ghost size="small">全量重建索引</n-button>
                </template>
                确认对用户「{{ user || '—' }}」执行全量重建？运行指标（守卫统计等）会一并清零。
              </n-popconfirm>
            </n-space>
          </n-card>
        </template>
      </n-tab-pane>

      <!-- 使用记录 -->
      <n-tab-pane name="usage" tab="使用记录">
        <n-space vertical>
          <n-card size="small">
            <n-space align="center">
              <n-select v-model:value="usageFilter" :options="toolOptions"
                placeholder="过滤工具" clearable size="small" style="width: 200px" />
              <n-button size="small" @click="loadUsage">刷新</n-button>
            </n-space>
          </n-card>
          <n-data-table :columns="usageColumns" :data="usageRows"
            :max-height="500" size="small" striped />
        </n-space>
      </n-tab-pane>

      <!-- config.toml -->
      <n-tab-pane name="config" tab="config.toml">
        <n-space vertical>
          <n-space>
            <n-button type="primary" @click="saveConfig" :loading="configSaving">保存</n-button>
            <n-checkbox v-model:checked="configRestart">保存并重启服务</n-checkbox>
          </n-space>
          <n-input v-model:value="configText" type="textarea" :rows="24"
            style="font-family: monospace; font-size: 13px" />
          <n-text depth="3" style="font-size: 12px">
            保存前自动校验 TOML 语法与配置结构，并备份原文件。
          </n-text>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<script setup>
import { ref, onMounted, h } from 'vue'
import {
  NTabs, NTabPane, NCard, NSpin, NDescriptions, NDescriptionsItem, NTag,
  NGrid, NGi, NStatistic, NDivider, NSpace, NSelect, NButton, NDataTable,
  NInput, NCheckbox, NText, NPopconfirm, useMessage,
} from 'naive-ui'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const overview = ref(null)
const usageRows = ref([])
const usageFilter = ref(null)
const configText = ref('')
const configSaving = ref(false)
const configRestart = ref(false)

const toolOptions = [
  'memory_search', 'memory_read', 'memory_write', 'memory_edit',
  'memory_edit_section', 'memory_move', 'memory_delete', 'memory_audit',
  'memory_list', 'memory_context', 'topic_list', 'topic_register',
  'topic_unregister', 'archive_topic', 'get_user_preference', 'update_user_preference',
].map(t => ({ label: t, value: t }))

const usageColumns = [
  { title: '时间', key: 'ts', width: 150, render: (r) => r.ts?.slice(5, 19) || '' },
  { title: '用户', key: 'user', width: 70 },
  { title: '工具', key: 'tool', width: 140 },
  { title: '摘要', key: 'summary', ellipsis: { tooltip: true } },
  { title: '耗时', key: 'ms', width: 60, render: (r) => r.ok ? `${r.ms}ms` : '—' },
  { title: '状态', key: 'ok', width: 50,
    render: (r) => h(NTag, { size: 'tiny', type: r.ok ? 'success' : 'error' }, () => r.ok ? '✓' : '✗') },
]

async function loadOverview() {
  try {
    overview.value = await api('/api/overview')
  } catch (e) { message.error(e.message) }
}

async function loadUsage() {
  try {
    const data = await api(`/api/usage${params({ limit: 100, tool: usageFilter.value })}`)
    usageRows.value = data.rows
  } catch (e) { message.error(e.message) }
}

async function loadConfig() {
  try {
    const data = await api('/api/config')
    configText.value = data.content
  } catch (e) { message.error(e.message) }
}

async function saveConfig() {
  configSaving.value = true
  try {
    const data = await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({ content: configText.value, restart: configRestart.value }),
    })
    if (data.backup) message.success(`已保存（备份: ${data.backup}）`)
    else message.success('已保存')
    if (data.restarting) message.info('服务重启中…')
  } catch (e) {
    message.error('保存失败: ' + e.message)
  } finally {
    configSaving.value = false
  }
}

async function runReindex() {
  try {
    await api(`/api/${props.user}/reindex`, { method: 'POST' })
    message.success('索引已全量重建')
    await loadOverview()
  } catch (e) {
    message.error('重建失败: ' + e.message)
  }
}

onMounted(() => {
  loadOverview()
  loadUsage()
  if (props.user) loadConfig()
})
</script>

<style scoped>
.settings-page { max-width: 900px; margin: 0 auto; }
</style>
