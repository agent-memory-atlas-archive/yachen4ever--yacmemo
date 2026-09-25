<template>
  <div class="dashboard-page">
    <n-space vertical size="large">
      <!-- 系统状态卡 -->
      <n-card size="small">
        <n-space align="center" justify="space-between">
          <n-space align="center" :size="10">
            <n-tag size="small" :type="overview ? 'success' : 'error'">
              {{ overview ? '服务运行中' : '连接失败' }}
            </n-tag>
            <n-tag size="small" :type="overview?.embedding?.configured ? 'success' : 'warning'">
              Embedding：{{ overview?.embedding?.configured ? overview.embedding.model : '未配置（FTS-only）' }}
            </n-tag>
            <n-tag size="small" :type="overview?.curator?.enabled ? 'success' : 'default'">
              Curator：{{ overview?.curator?.enabled ? (overview.curator.model || '已启用') : '未启用' }}
            </n-tag>
            <n-tag size="small">今日调用 {{ overview?.calls_today ?? 0 }}</n-tag>
          </n-space>
          <n-button size="small" @click="load" :loading="loading">刷新</n-button>
        </n-space>
      </n-card>

      <!-- 用户卡片 -->
      <n-grid :cols="3" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
        <n-gi v-for="u in overview?.users || []" :key="u.id" span="3 m:1">
          <n-card size="small" :title="u.id">
            <template #header-extra>
              <n-space :size="4">
                <n-button size="tiny" @click="go(u.id, 'audit')">审计</n-button>
                <n-button size="tiny" quaternary @click="go(u.id, 'topics')">笔记</n-button>
              </n-space>
            </template>
            <n-grid :cols="4" :x-gap="8">
              <n-gi><n-statistic label="笔记" :value="u.note_count" /></n-gi>
              <n-gi><n-statistic label="主题" :value="u.topics.length" /></n-gi>
              <n-gi><n-statistic label="待处理" :value="u.open_issues ?? '—'" /></n-gi>
              <n-gi><n-statistic label="提案" :value="u.curator_proposals" /></n-gi>
            </n-grid>
            <n-space size="small" style="margin-top: 8px" :wrap="true">
              <n-tag size="tiny" :type="(u.open_issues ?? 0) > 0 ? 'warning' : 'success'">
                审计待办 {{ u.open_issues ?? '—' }}
              </n-tag>
              <n-tag size="tiny">最近审计 {{ fmtTs(u.last_audit_ts) || '重启后未审计' }}</n-tag>
              <n-tag size="tiny" :type="(u.git_status || '').includes('失败') ? 'error' : 'success'">
                git 正常
              </n-tag>
            </n-space>
          </n-card>
        </n-gi>
      </n-grid>

      <!-- 最近活动 -->
      <n-card size="small" title="最近活动">
        <n-empty v-if="!recent.length" description="暂无调用记录" />
        <n-list v-else>
          <n-list-item v-for="r in recent" :key="r.id">
            <n-space justify="space-between" align="center" :wrap="false">
              <n-space align="center" :size="8" :wrap="false">
                <n-tag size="tiny" :type="r.ok ? 'success' : 'error'">{{ r.ok ? '✓' : '✗' }}</n-tag>
                <n-text depth="2" style="font-size: 12px">{{ r.user }}</n-text>
                <n-text style="font-size: 13px">{{ r.tool }}</n-text>
                <n-text depth="3" style="font-size: 12px">{{ r.summary }}</n-text>
              </n-space>
              <n-text depth="3" style="font-size: 11px">{{ r.ts?.slice(5, 16) }}</n-text>
            </n-space>
          </n-list-item>
        </n-list>
      </n-card>
    </n-space>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import {
  NSpace, NCard, NButton, NTag, NGrid, NGi, NStatistic, NList, NListItem,
  NText, NEmpty,
} from 'naive-ui'
import { api, params } from '../composables/api.js'

defineProps({ build: { type: Object, default: () => ({}) } })
const emit = defineEmits(['navigate'])

const overview = ref(null)
const recent = ref([])
const loading = ref(false)

function fmtTs(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return isNaN(d) ? '' : d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function go(user, page) {
  emit('navigate', { user, page })
}

async function load() {
  loading.value = true
  try {
    const [ov, usage] = await Promise.all([
      api('/api/overview'),
      api(`/api/usage${params({ limit: 8 })}`),
    ])
    overview.value = ov
    recent.value = usage.rows || []
  } catch (e) { /* 未登录等场景由 App 层处理 */ }
  loading.value = false
}

onMounted(load)
</script>

<style scoped>
.dashboard-page { max-width: 1080px; margin: 0 auto; }
</style>
