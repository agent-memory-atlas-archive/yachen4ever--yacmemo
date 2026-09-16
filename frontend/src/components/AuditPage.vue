<template>
  <div class="audit-page">
    <n-space vertical size="large">
      <!-- 操作栏 -->
      <n-card size="small">
        <n-space>
          <n-button @click="runAudit" :loading="auditing">确定性审计</n-button>
          <n-button @click="runCurator" :loading="curatorRunning"
            :disabled="!curatorReady">深度审查 (curator)</n-button>
          <n-popconfirm @positive-click="runReindex">
            <template #trigger><n-button type="warning" ghost>全量重建索引</n-button></template>
            确认删除全部派生索引并从 markdown 全量重建？
          </n-popconfirm>
        </n-space>
      </n-card>

      <!-- 审计结果 -->
      <template v-if="auditData">
        <n-card v-if="auditData.git" size="small" :bordered="true">
          <n-text depth="3">git 快照：</n-text>
          <n-tag :type="auditData.git.includes('启用') && !auditData.git.includes('失败') ? 'success' : 'warning'" size="small">
            {{ auditData.git }}
          </n-tag>
        </n-card>

        <AuditSection title="新发现文件（已建立索引）" :items="auditData.added" />
        <AuditSection title="外部修改（已自动重建索引）" :items="auditData.resynced" />
        <AuditSection title="外部删除（已清理索引）" :items="auditData.missing" />

        <n-card v-if="auditData.title_duplicates?.length" size="small" title="标题重复（D1）">
          <n-list>
            <n-list-item v-for="(c, i) in auditData.title_duplicates" :key="i">
              [[{{ c.a_title }}]] ↔ [[{{ c.b_title }}]] (score {{ c.score }})
            </n-list-item>
          </n-list>
        </n-card>

        <n-card v-if="auditData.collisions?.length" size="small" title="语义撞车（D2）">
          <n-list>
            <n-list-item v-for="(c, i) in auditData.collisions" :key="i">
              <n-space vertical size="small">
                <n-text>{{ c.a_path }} ↔ {{ c.b_path }} (score {{ c.score }})</n-text>
                <n-text depth="3">A: {{ c.a_text?.slice(0, 60) }}</n-text>
                <n-text depth="3">B: {{ c.b_text?.slice(0, 60) }}</n-text>
                <n-space>
                  <n-button size="tiny" @click="resolveCollision(c.id, 'resolved')">已处理</n-button>
                  <n-button size="tiny" @click="resolveCollision(c.id, 'dismissed')">忽略</n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </n-card>

        <AuditSection title="悬空链接（D3）" :items="auditData.dangling_links?.map(d => `${d.path}: [[${d.link}]]`)" />
        <AuditSection title="游离文件（D4）" :items="auditData.stray" />

        <n-card size="small" title="守卫统计">
          <n-statistic label="拒绝" :value="auditData.guard_stats?.refused || 0" />
          <n-statistic label="force 越过" :value="auditData.guard_stats?.forced || 0" />
        </n-card>
      </template>

      <!-- Curator 提案 -->
      <n-card v-if="proposals.length" size="small" title="质量提案">
        <n-tabs type="segment">
          <n-tab-pane v-for="p in proposals" :key="p.file" :name="p.file" :tab="p.file">
            <div class="markdown-body" v-html="renderProposal(p.content)" />
          </n-tab-pane>
        </n-tabs>
      </n-card>

      <n-card v-if="curatorReport" size="small" title="本次深度审查报告">
        <div class="markdown-body" v-html="renderMarkdown(curatorReport)" />
      </n-card>
    </n-space>
  </div>
</template>

<script setup>
import { ref, onMounted, h } from 'vue'
import {
  NSpace, NCard, NButton, NList, NListItem, NText, NTag, NTabs, NTabPane,
  NPopconfirm, NStatistic, useMessage,
} from 'naive-ui'
import { marked } from 'marked'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const auditing = ref(false)
const curatorRunning = ref(false)
const curatorReady = ref(false)
const auditData = ref(null)
const proposals = ref([])
const curatorReport = ref('')

const AuditSection = {
  props: ['title', 'items'],
  setup(p) {
    return () => p.items?.length
      ? h(NCard, { size: 'small', title: `${p.title}（${p.items.length}）` }, () =>
          h(NList, () => p.items.map((item, i) => h(NListItem, { key: i }, () => item))))
      : null
  },
}

function renderMarkdown(text) { return marked.parse(text || '') }
function renderProposal(p) { return p ? marked.parse(p) : '' }

async function runAudit() {
  auditing.value = true
  try {
    const data = await api(`/api/${props.user}/audit`, { method: 'POST' })
    auditData.value = data.audit
    message.success('审计完成')
  } catch (e) {
    message.error('审计失败: ' + e.message)
  } finally {
    auditing.value = false
  }
}

async function runCurator() {
  curatorRunning.value = true
  try {
    const data = await api(`/api/${props.user}/curator`, { method: 'POST' })
    curatorReport.value = data.report
    message.success('深度审查完成')
    await loadProposals()
  } catch (e) {
    message.error('深度审查失败: ' + e.message)
  } finally {
    curatorRunning.value = false
  }
}

async function loadProposals() {
  try {
    const data = await api(`/api/${props.user}/proposals`)
    const loaded = []
    for (const p of data.proposals) {
      const note = await api(`/api/${props.user}/note${params({ path: p.path })}`)
      loaded.push({ ...p, content: note.content })
    }
    proposals.value = loaded
  } catch (e) { /* silently ignore */ }
}

async function runReindex() {
  try {
    await api(`/api/${props.user}/reindex`, { method: 'POST' })
    message.success('索引已全量重建')
  } catch (e) {
    message.error('重建失败: ' + e.message)
  }
}

async function resolveCollision(id, status) {
  try {
    await api(`/api/${props.user}/collision`, {
      method: 'POST',
      body: JSON.stringify({ id, status }),
    })
    message.success('已标记')
    await runAudit()
  } catch (e) {
    message.error(e.message)
  }
}

onMounted(async () => {
  try {
    const overview = await api('/api/overview')
    const u = overview.users.find(x => x.id === props.user)
    if (u) curatorReady.value = u.curator_proposals >= 0  // curator is configured if overview doesn't error
  } catch (e) { /* */ }
  await loadProposals()
})
</script>

<style scoped>
.audit-page { max-width: 900px; margin: 0 auto; }
.markdown-body { line-height: 1.7; }
</style>
