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

      <!-- 历史审计目录 -->
      <n-card size="small" title="历史审计快照">
        <n-empty v-if="!runs.length" description="还没有审计记录，点上方「确定性审计」开始" />
        <n-list v-else hoverable clickable>
          <n-list-item v-for="r in runs" :key="r.file"
            :class="{ 'run-active': r.path === currentRun }" @click="viewRun(r)">
            <n-space justify="space-between" align="center">
              <n-text :depth="r.path === currentRun ? 1 : 2">{{ r.file }}</n-text>
              <n-text depth="3">{{ fmtSize(r.size) }}</n-text>
            </n-space>
          </n-list-item>
        </n-list>
      </n-card>

      <!-- 当前审计：结构化问题清单（可处置） -->
      <template v-if="auditData">
        <n-card v-if="auditData.git" size="small" :bordered="true">
          <n-text depth="3">git 快照：</n-text>
          <n-tag :type="auditData.git.includes('启用') && !auditData.git.includes('失败') ? 'success' : 'warning'" size="small">
            {{ auditData.git }}
          </n-tag>
        </n-card>

        <AuditSection title="新增文件（已入索引）" :items="auditData.added" />
        <AuditSection title="外部修改（已自动重建索引）" :items="auditData.resynced" />
        <AuditSection title="外部删除（已清理索引）" :items="auditData.missing" />

        <!-- D1 标题重复 -->
        <n-card v-if="openD1.length" size="small" title="标题重复（D1）">
          <n-list>
            <n-list-item v-for="(c, i) in openD1" :key="i">
              <n-space justify="space-between" align="center">
                <n-text>[[{{ c.a_title }}]] ↔ [[{{ c.b_title }}]] (score {{ c.score }})</n-text>
                <n-space>
                  <n-button size="tiny" type="success" @click="dispose(d1Id(c), 'resolved', '合并标题重复')">已处理</n-button>
                  <n-button size="tiny" @click="dispose(d1Id(c), 'dismissed', '标题重复误报')">忽略</n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </n-card>

        <!-- D2 语义撞车 -->
        <n-card v-if="openD2.length" size="small" title="语义撞车（D2）">
          <n-list>
            <n-list-item v-for="(c, i) in openD2" :key="i">
              <n-space vertical size="small">
                <n-text>{{ c.a_path }} ↔ {{ c.b_path }} (score {{ c.score }})</n-text>
                <n-text depth="3">A: {{ c.a_text?.slice(0, 60) }}</n-text>
                <n-text depth="3">B: {{ c.b_text?.slice(0, 60) }}</n-text>
                <n-space>
                  <n-button size="tiny" @click="dispose(`D2:${c.id}`, 'resolved', '合并撞车内容')">已处理</n-button>
                  <n-button size="tiny" @click="dispose(`D2:${c.id}`, 'dismissed', '撞车误报')">忽略</n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </n-card>

        <!-- D3 悬空链接 -->
        <n-card v-if="openD3.length" size="small" title="悬空链接（D3）">
          <n-list>
            <n-list-item v-for="(c, i) in openD3" :key="i">
              <n-space justify="space-between" align="center">
                <n-text>{{ c.path }}: [[{{ c.link }}]]</n-text>
                <n-space>
                  <n-button size="tiny" @click="dispose(d3Id(c), 'resolved', '链接已补齐')">已处理</n-button>
                  <n-button size="tiny" @click="dispose(d3Id(c), 'dismissed', '非笔记引用')">忽略</n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </n-card>

        <!-- D4 游离文件 -->
        <n-card v-if="openStray.length" size="small" title="游离文件（D4）">
          <n-list>
            <n-list-item v-for="(p, i) in openStray" :key="i">
              <n-space justify="space-between" align="center">
                <n-text>{{ p }}</n-text>
                <n-space>
                  <n-button size="tiny" @click="dispose(d4Id(p), 'resolved', '已归位')">已处理</n-button>
                  <n-button size="tiny" @click="dispose(d4Id(p), 'dismissed', '无需归位')">忽略</n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </n-card>

        <n-card v-if="!openD1.length && !openD2.length && !openD3.length && !openStray.length" size="small">
          <n-text depth="3">本次审计未发现待处理问题。</n-text>
        </n-card>

        <n-card size="small" title="守卫统计">
          <n-statistic label="拒绝" :value="auditData.guard_stats?.refused || 0" />
          <n-statistic label="force 越过" :value="auditData.guard_stats?.forced || 0" />
        </n-card>
      </template>

      <!-- 快照与处置记录（markdown 轨迹） -->
      <n-card v-if="snapshotMarkdown" size="small" :title="snapshotTitle">
        <div class="markdown-body" v-html="renderMarkdown(snapshotMarkdown)" />
      </n-card>

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
import { ref, computed, onMounted, h } from 'vue'
import {
  NSpace, NCard, NButton, NList, NListItem, NText, NTag, NTabs, NTabPane,
  NPopconfirm, NStatistic, NEmpty, useMessage,
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

// ---- 审计历史 / 快照 ----
const runs = ref([])
const currentRun = ref('')            // 当前展示的快照 path（journal/audit/xxx.md）
const snapshotMarkdown = ref('')
const snapshotTitle = ref('')
const disposed = ref(new Set())       // 本会话内已处置的问题 id（即时反馈）

const AuditSection = {
  props: ['title', 'items'],
  setup(p) {
    return () => p.items?.length
      ? h(NCard, { size: 'small', title: `${p.title}（${p.items.length}）` }, () =>
          h(NList, () => p.items.map((item, i) => h(NListItem, { key: i }, () => item))))
      : null
  },
}

const d1Id = c => 'D1:' + [c.a_title, c.b_title].sort().join('|')
const d3Id = c => `D3:${c.path}|${c.link}`
const d4Id = p => `D4:${p}`

const openD1 = computed(() => (auditData.value?.title_duplicates || []).filter(c => !disposed.value.has(d1Id(c))))
const openD2 = computed(() => (auditData.value?.collisions || []).filter(c => !disposed.value.has(`D2:${c.id}`)))
const openD3 = computed(() => (auditData.value?.dangling_links || []).filter(c => !disposed.value.has(d3Id(c))))
const openStray = computed(() => (auditData.value?.stray || []).filter(p => !disposed.value.has(d4Id(p))))

function fmtSize(n) { return n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B` }
function renderMarkdown(text) { return marked.parse(text || '') }
function renderProposal(p) { return p ? marked.parse(p) : '' }

async function loadRuns() {
  try {
    const data = await api(`/api/${props.user}/audit/runs`)
    runs.value = data.runs || []
  } catch (e) { /* silently ignore */ }
}

async function viewRun(r) {
  currentRun.value = r.path
  snapshotTitle.value = r.file
  try {
    const note = await api(`/api/${props.user}/note${params({ path: r.path })}`)
    snapshotMarkdown.value = note.content
  } catch (e) {
    message.error('加载快照失败: ' + e.message)
  }
}

async function runAudit() {
  auditing.value = true
  try {
    const data = await api(`/api/${props.user}/audit`, { method: 'POST' })
    auditData.value = data.audit
    message.success('审计完成')
    await loadRuns()
    if (auditData.value.audit_file) {
      const run = runs.value.find(r => r.path === auditData.value.audit_file)
      await viewRun(run || { path: auditData.value.audit_file, file: auditData.value.audit_file.split('/').pop() })
    }
  } catch (e) {
    message.error('审计失败: ' + e.message)
  } finally {
    auditing.value = false
  }
}

async function dispose(issueId, action, label) {
  if (!currentRun.value) return
  try {
    const data = await api(`/api/${props.user}/audit/action`, {
      method: 'POST',
      body: JSON.stringify({ file: currentRun.value, id: issueId, action, label }),
    })
    disposed.value.add(issueId)
    if (data.content) snapshotMarkdown.value = data.content
    message.success(action === 'resolved' ? '已标记处理' : '已忽略')
  } catch (e) {
    message.error(e.message)
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

onMounted(async () => {
  if (!props.user) return
  try {
    const overview = await api('/api/overview')
    const u = overview.users.find(x => x.id === props.user)
    if (u) curatorReady.value = u.curator_proposals >= 0
  } catch (e) { /* */ }
  await loadRuns()
  await loadProposals()
})
</script>

<style scoped>
.audit-page { max-width: 900px; margin: 0 auto; }
.markdown-body { line-height: 1.7; }
.run-active { background: rgba(51, 153, 255, 0.08); }
</style>