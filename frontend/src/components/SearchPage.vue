<template>
  <div class="search-page">
    <n-space vertical>
      <n-input-group>
        <n-input
          v-model:value="query"
          placeholder="输入关键词或自然语句搜索…"
          @keydown.enter="doSearch"
          clearable
        />
        <n-select v-model:value="kind" :options="kindOptions" style="width: 110px" />
        <n-button type="primary" @click="doSearch" :loading="searching">搜索</n-button>
      </n-input-group>
      <n-alert v-if="notice" type="warning" size="small" style="margin-bottom: 8px">{{ notice }}</n-alert>
      <div v-if="results.length" class="search-results">
        <n-card v-for="(r, i) in results" :key="i" :title="r.title" size="small"
          style="margin-bottom: 8px; cursor: pointer" @click="openNote(r.path)">
          <template #header-extra>
            <n-space size="small">
              <n-tag v-for="ch in r.channels" :key="ch" size="tiny"
                :type="ch === 'fts' ? 'success' : 'info'">{{ ch }}</n-tag>
            </n-space>
          </template>
          <n-text depth="3" code style="font-size: 12px">{{ r.path }}</n-text>
          <n-progress v-if="r.score" :percentage="scorePercent(r.score)" :show-indicator="false"
            size="small" style="margin-top: 4px" />
          <n-alert v-for="w in (r.warnings || [])" :key="w" type="warning" size="small"
            style="margin-top: 4px">{{ w }}</n-alert>
        </n-card>
      </div>
      <n-empty v-else-if="searched" description="未找到相关笔记" />
    </n-space>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { NSpace, NInputGroup, NInput, NSelect, NButton, NCard, NTag, NText, NProgress, NAlert, NEmpty, useMessage } from 'naive-ui'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const query = ref('')
const kind = ref('hybrid')
const searching = ref(false)
const searched = ref(false)
const results = ref([])
const notice = ref('')

const kindOptions = [
  { label: '混合', value: 'hybrid' },
  { label: '全文', value: 'fts' },
  { label: '向量', value: 'vector' },
]

function scorePercent(score) {
  return Math.min(100, Math.round(score * 3000))
}

async function doSearch() {
  if (!query.value.trim()) return
  searching.value = true
  searched.value = true
  try {
    const data = await api(`/api/${props.user}/search${params({ q: query.value, kind: kind.value, limit: 20 })}`)
    results.value = data.results
    notice.value = data.notice || ''
  } catch (e) {
    message.error('搜索失败: ' + e.message)
  } finally {
    searching.value = false
  }
}

async function openNote(path) {
  window.open(`/ui/#note=${encodeURIComponent(path)}`, '_self')
}
</script>

<style scoped>
.search-page { max-width: 800px; margin: 0 auto; }
.search-results { margin-top: 12px; }
</style>
