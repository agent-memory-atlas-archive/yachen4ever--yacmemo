<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { NCard, NSpace, NInput, NButton, NCode, NEmpty, useMessage } from 'naive-ui'
import { api, type SearchResult } from '../api'

const route = useRoute()
const message = useMessage()
const userId = route.params.userId as string

const searchQuery = ref('')
const searchResults = ref<SearchResult[]>([])
const searching = ref(false)

const grepPattern = ref('')
const grepResults = ref('')
const grepping = ref(false)

async function doSearch() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  try {
    const resp = await api.search(userId, searchQuery.value, 10)
    if (resp.error) {
      message.error(resp.error)
    }
    searchResults.value = resp.results ?? []
  } catch (e: any) {
    message.error(e.message)
  } finally {
    searching.value = false
  }
}

async function doGrep() {
  if (!grepPattern.value.trim()) return
  grepping.value = true
  try {
    const resp = await api.grep(userId, grepPattern.value)
    if (resp.error) {
      message.error(resp.error)
    }
    grepResults.value = resp.matches ?? '(no output)'
  } catch (e: any) {
    message.error(e.message)
  } finally {
    grepping.value = false
  }
}
</script>

<template>
  <n-space vertical :size="20">
    <h1 style="margin: 0;">搜索 · {{ userId }}</h1>

    <n-card title="语义搜索">
      <n-space align="center">
        <n-input
          v-model:value="searchQuery"
          placeholder="输入查询文本..."
          style="width: 500px;"
          @keyup.enter="doSearch"
        />
        <n-button type="primary" :loading="searching" @click="doSearch">搜索</n-button>
      </n-space>

      <n-space vertical :size="8" style="margin-top: 16px;" v-if="searchResults.length">
        <n-card v-for="(r, i) in searchResults" :key="r.id" size="small" :bordered="true">
          <template #header>
            <span style="font-size: 13px;">
              {{ i + 1 }}. [{{ r.kind }}] {{ r.text }}
            </span>
          </template>
          <span style="font-size: 12px; color: #888;">
            source: {{ r.source_path }} | distance: {{ r._distance?.toFixed(4) }}
          </span>
        </n-card>
      </n-space>
      <n-empty v-else-if="!searching" description="输入查询后点击搜索" style="margin-top: 20px;" />
    </n-card>

    <n-card title="正则搜索 (grep)">
      <n-space align="center">
        <n-input
          v-model:value="grepPattern"
          placeholder="输入正则表达式..."
          style="width: 500px;"
          @keyup.enter="doGrep"
        />
        <n-button type="primary" :loading="grepping" @click="doGrep">搜索</n-button>
      </n-space>
      <n-code v-if="grepResults" :code="grepResults" language="text" word-wrap style="margin-top: 16px;" />
    </n-card>
  </n-space>
</template>
