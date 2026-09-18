<template>
  <div class="audit-page">
    <n-tabs type="line" v-model:value="activeTab">
      <!-- ============ Tab 1: 确定性审计 ============ -->
      <n-tab-pane name="audit" tab="确定性审计">
        <n-space vertical size="large">
          <n-card size="small">
            <n-space justify="space-between" align="center">
              <n-space align="center">
                <n-button type="primary" @click="runAudit" :loading="auditing">立即审计</n-button>
                <n-text depth="3" v-if="lastAuditTime">上次审计：{{ lastAuditTime }}</n-text>
                <n-text v-else depth="3">本页显示最近一次审计结果；服务重启后请重新审计</n-text>
              </n-space>
              <n-text depth="3">快照每日一份，同日重跑自动追加"复审"</n-text>
            </n-space>
          </n-card>

          <template v-if="auditData">
            <n-card v-if="auditData.git" size="small">
              <n-space align="center">
                <n-text depth="3">git 快照：</n-text>
                <n-tag :type="auditData.git.includes('启用') && !auditData.git.includes('失败') ? 'success' : 'warning'" size="small">
                  {{ auditData.git }}
                </n-tag>
              </n-space>
            </n-card>

            <AuditSection title="新增文件（已入索引）" :items="auditData.added" />
            <AuditSection title="外部修改（已自动重建索引）" :items="auditData.resynced" />
            <AuditSection title="外部删除（已清理索引）" :items="auditData.missing" />
            <AuditSection title="缺向量笔记（已重试自愈）" :items="auditData.missing_vectors" />

            <n-card v-if="openD1.length" size="small" title="标题重复（D1）">
              <n-list>
                <n-list-item v-for="(c, i) in openD1" :key="i">
                  <n-space justify="space-between" align="center">
                    <n-text>{{ c.a_path }} ↔ {{ c.b_path }} (score {{ c.score }})</n-text>
                    <n-space>
                      <n-button size="tiny" type="success" @click="dispose(d1Id(c), 'resolved', '合并标题重复')">已处理</n-button>
                      <n-button size="tiny" @click="dispose(d1Id(c), 'dismissed', '标题重复误报')">忽略</n-button>
                    </n-space>
                  </n-space>
                </n-list-item>
              </n-list>
            </n-card>

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

            <n-card v-if="openD5.length" size="small" title="悬空主题卡（D5）">
              <n-list>
                <n-list-item v-for="(c, i) in openD5" :key="i">
                  <n-space justify="space-between" align="center">
                    <n-text>{{ d5Parts(c).title }}：卡路径不存在（{{ d5Parts(c).card }}）</n-text>
                    <n-space>
                      <n-button size="tiny" @click="dispose(c, 'resolved', '注册表已修正')">已处理</n-button>
                      <n-button size="tiny" @click="dispose(c, 'dismissed', '暂不处理')">忽略</n-button>
                    </n-space>
                  </n-space>
                </n-list-item>
              </n-list>
            </n-card>

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

            <n-card v-if="!hasOpenIssues" size="small">
              <n-text depth="3">当前无待处理问题。</n-text>
            </n-card>

            <n-card size="small" title="守卫统计">
              <n-space>
                <n-statistic label="拒绝" :value="auditData.guard_stats?.refused || 0" />
                <n-statistic label="force 越过" :value="auditData.guard_stats?.forced || 0" />
                <n-statistic label="未覆盖拦截" :value="auditData.guard_stats?.uncovered || 0" />
              </n-space>
            </n-card>
          </template>
          <n-card v-else size="small">
            <n-empty description="还没有审计结果——点上方「立即审计」开始" />
          </n-card>

          <!-- 处置历史：权威数据源是 audit_actions 表，不是快照文件 -->
          <n-card size="small" title="处置历史">
            <n-empty v-if="!actions.length" description="暂无处置记录" />
            <n-list v-else>
              <n-list-item v-for="a in actions.slice(0, 50)" :key="a.id + a.acted_at">
                <n-space justify="space-between" align="center">
                  <n-text :depth="2" style="font-size: 13px">
                    <n-tag size="tiny" :type="a.action === 'resolved' || a.action === 'adopted' ? 'success' : 'default'">
                      {{ actionVerb(a) }}
                    </n-tag>
                    {{ a.id }}
                  </n-text>
                  <n-text depth="3" style="font-size: 12px">{{ a.acted_at }}{{ a.note ? ` · ${a.note}` : '' }}</n-text>
                </n-space>
              </n-list-item>
            </n-list>
          </n-card>

          <!-- 历史快照 -->
          <n-card size="small" title="历史审计快照">
            <n-empty v-if="!runs.length" description="还没有审计记录" />
            <n-list v-else hoverable clickable>
              <n-list-item v-for="r in runs" :key="r.file"
                :class="{ 'run-active': r.path === currentRun }" @click="viewRun(r)">
                <n-space justify="space-between" align="center">
                  <n-text :depth="r.path === currentRun ? 1 : 2">{{ fmtRunName(r.file) }}</n-text>
                  <n-text depth="3">{{ fmtSize(r.size) }}</n-text>
                </n-space>
              </n-list-item>
            </n-list>
            <n-card v-if="snapshotMarkdown" size="small" :title="snapshotTitle"
              style="margin-top: 12px">
              <div class="markdown-body" v-html="renderMarkdown(snapshotMarkdown)" />
            </n-card>
          </n-card>
        </n-space>
      </n-tab-pane>

      <!-- ============ Tab 2: 质量提案 ============ -->
      <n-tab-pane name="proposals" tab="质量提案">
        <n-space vertical size="large">
          <n-card size="small">
            <n-space justify="space-between" align="center">
              <n-space align="center">
                <n-button type="primary" @click="runCurator" :loading="curatorRunning">立即深度审查</n-button>
                <n-text depth="3">调用主模型产出质量提案，约需 1–3 分钟；每周 timer 自动执行</n-text>
              </n-space>
              <n-space>
                <n-tag size="small">待裁决 {{ pendingCount }}</n-tag>
                <n-tag size="small" type="success">已采纳 {{ adoptedCount }}</n-tag>
                <n-tag size="small" type="default">已忽略 {{ dismissedCount }}</n-tag>
              </n-space>
            </n-space>
          </n-card>

          <n-empty v-if="!proposals.length"
            description="还没有质量提案——点上方「立即深度审查」生成，或等每周 timer 自动执行" />

          <n-card v-for="p in proposals" :key="p.file" size="small" :title="p.file">
            <n-space vertical size="small">
              <n-card v-for="f in p.findings" :key="f.index" size="small"
                :bordered="true" :class="{ 'finding-done': findingAction(p, f) }">
                <n-space vertical size="small">
                  <n-space justify="space-between" align="center">
                    <n-space align="center">
                      <n-tag size="tiny" :type="f.severity === 'high' ? 'error' : f.severity === 'medium' ? 'warning' : 'default'">
                        {{ f.severity }}
                      </n-tag>
                      <n-tag size="tiny">{{ f.type }}</n-tag>
                      <n-text v-if="findingAction(p, f)" depth="3">
                        （{{ findingAction(p, f) === 'adopted' ? '已采纳' : '已忽略' }}）
                      </n-text>
                    </n-space>
                    <n-space v-if="!findingAction(p, f)">
                      <n-button size="tiny" type="success" @click="judge(p, f, 'adopted')">采纳</n-button>
                      <n-button size="tiny" @click="judge(p, f, 'dismissed')">忽略</n-button>
                    </n-space>
                    <n-space v-else-if="findingAction(p, f) === 'adopted'">
                      <n-button size="tiny" @click="copyExecInstruction(p, f)">复制执行指令</n-button>
                    </n-space>
                  </n-space>
                  <n-text depth="2">{{ f.reason }}</n-text>
                  <n-text v-if="f.paths.length" depth="3" style="font-size: 12px">
                    涉及：{{ f.paths.join('、') }}
                  </n-text>
                  <n-text v-if="f.proposal" depth="3" style="font-size: 12px">建议：{{ f.proposal }}</n-text>
                </n-space>
              </n-card>
              <n-text v-if="!p.findings.length" depth="3">本次提案无发现条目。</n-text>
              <n-collapse>
                <n-collapse-item title="提案原文" name="raw">
                  <div class="markdown-body" v-html="renderMarkdown(p.content)" />
                </n-collapse-item>
              </n-collapse>
            </n-space>
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, h } from 'vue'
import {
  NSpace, NCard, NButton, NList, NListItem, NText, NTag, NTabs, NTabPane,
  NStatistic, NEmpty, NCollapse, NCollapseItem, useMessage,
} from 'naive-ui'
import { marked } from 'marked'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const activeTab = ref('audit')
const auditing = ref(false)
const curatorRunning = ref(false)
const auditData = ref(null)
const lastAuditTs = ref(0)

// ---- 审计历史 / 快照 ----
const runs = ref([])
const currentRun = ref('')
const snapshotMarkdown = ref('')
const snapshotTitle = ref('')
const actions = ref([])               // audit_actions 表全量（处置 + 提案裁决）

// ---- 提案 ----
const proposals = ref([])             // [{file, path, content, findings}]
const disposed = ref(new Set())       // 本会话内已处置的审计问题 id（即时反馈）

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
const d5Parts = c => {
  const rest = c.startsWith('D5:') ? c.slice(3) : c
  const i = rest.indexOf('|')
  return i === -1 ? { title: rest, card: '' } : { title: rest.slice(0, i), card: rest.slice(i + 1) }
}

const openD1 = computed(() => (auditData.value?.title_duplicates || []).filter(c => !disposed.value.has(d1Id(c))))
const openD2 = computed(() => (auditData.value?.collisions || []).filter(c => !disposed.value.has(`D2:${c.id}`)))
const openD3 = computed(() => (auditData.value?.dangling_links || []).filter(c => !disposed.value.has(d3Id(c))))
const openStray = computed(() => (auditData.value?.stray || []).filter(p => !disposed.value.has(d4Id(p))))
const openD5 = computed(() => (auditData.value?.dangling_cards || []).filter(c => !disposed.value.has(c)))
const hasOpenIssues = computed(() =>
  openD1.value.length || openD2.value.length || openD3.value.length ||
  openD5.value.length || openStray.value.length)

const lastAuditTime = computed(() => lastAuditTs.value
  ? new Date(lastAuditTs.value * 1000).toLocaleString('zh-CN', { hour12: false }) : '')

// ---- 提案裁决状态（audit_actions 中 kind=P 的行） ----
const pActions = computed(() => {
  const m = new Map()
  for (const a of actions.value) {
    if (a.kind !== 'P') continue
    m.set(a.id, a)
  }
  return m
})
function findingAction(p, f) {
  const a = pActions.value.get(`P:${p.path}:${f.index}`)
  return a?.action || ''
}
const adoptedCount = computed(() =>
  [...pActions.value.values()].filter(a => a.action === 'adopted').length)
const dismissedCount = computed(() =>
  [...pActions.value.values()].filter(a => a.action === 'dismissed').length)
const pendingCount = computed(() => {
  const total = proposals.value.reduce((n, p) => n + p.findings.length, 0)
  return total - adoptedCount.value - dismissedCount.value
})

function parseFindings(markdown) {
  const lines = (markdown || '').split('\n')
  const start = lines.findIndex(l => l.startsWith('## 提案'))
  if (start === -1) return []
  const findings = []
  let cur = null
  for (const line of lines.slice(start + 1)) {
    if (line.startsWith('## ') || line.startsWith('> ')) break
    const m = line.match(/^(\d+)\.\s+\*\*\[(\w+)\]\s+(\w+)\*\*\s+—\s+(.*)$/)
    if (m) {
      cur = { index: Number(m[1]), severity: m[2], type: m[3], reason: m[4], paths: [], proposal: '' }
      findings.push(cur)
      continue
    }
    if (!cur) continue
    const p = line.match(/^\s+-\s+涉及:\s+(.*)$/)
    if (p) cur.paths.push(p[1])
    const s = line.match(/^\s+-\s+建议:\s+(.*)$/)
    if (s) cur.proposal = s[1]
  }
  return findings
}

function fmtSize(n) { return n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B` }
function fmtRunName(file) {
  const m = file.match(/^(\d{4})(\d{2})(\d{2})(?:-(\d{2})(\d{2})(\d{2}))?\.md$/)
  if (!m) return file
  const base = `${m[2]}-${m[3]}`
  return m[4] ? `${base} ${m[4]}:${m[5]}` : base
}
function renderMarkdown(text) { return marked.parse(text || '') }
function actionVerb(a) {
  if (a.kind === 'P') return a.action === 'adopted' ? '提案已采纳' : '提案已忽略'
  return a.action === 'resolved' ? '已处理' : '已忽略'
}

// ---- 数据加载 ----
async function loadAuditState() {
  try {
    const data = await api(`/api/${props.user}/audit/last`)
    if (data.audit) {
      auditData.value = data.audit
      lastAuditTs.value = data.ts || 0
      if (data.audit.audit_file) await viewRun({ path: data.audit.audit_file, file: data.audit.audit_file.split('/').pop() })
    }
  } catch (e) { /* 服务未重启过即无缓存，静默 */ }
}
async function loadActions() {
  try {
    const data = await api(`/api/${props.user}/audit/actions`)
    actions.value = data.actions || []
  } catch (e) { /* */ }
}
async function loadRuns() {
  try {
    const data = await api(`/api/${props.user}/audit/runs`)
    runs.value = data.runs || []
  } catch (e) { /* */ }
}
async function viewRun(r) {
  currentRun.value = r.path
  snapshotTitle.value = fmtRunName(r.file) + ' 快照'
  try {
    const note = await api(`/api/${props.user}/note${params({ path: r.path })}`)
    snapshotMarkdown.value = note.content
  } catch (e) {
    message.error('加载快照失败: ' + e.message)
  }
}
async function loadProposals() {
  try {
    const data = await api(`/api/${props.user}/proposals`)
    const loaded = []
    for (const p of data.proposals) {
      const note = await api(`/api/${props.user}/note${params({ path: p.path })}`)
      loaded.push({ ...p, content: note.content, findings: parseFindings(note.content) })
    }
    proposals.value = loaded
  } catch (e) { /* */ }
}
async function reloadAll() {
  if (!props.user) return
  await Promise.all([loadAuditState(), loadActions(), loadRuns(), loadProposals()])
}

// ---- 动作 ----
async function runAudit() {
  auditing.value = true
  try {
    const data = await api(`/api/${props.user}/audit`, { method: 'POST' })
    auditData.value = data.audit
    lastAuditTs.value = Math.floor(Date.now() / 1000)
    message.success('审计完成')
    await Promise.all([loadRuns(), loadActions()])
    if (auditData.value.audit_file) {
      await viewRun({ path: auditData.value.audit_file, file: auditData.value.audit_file.split('/').pop() })
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
    await loadActions()
    message.success(action === 'resolved' ? '已标记处理' : '已忽略')
  } catch (e) {
    message.error(e.message)
  }
}

async function runCurator() {
  curatorRunning.value = true
  message.info('深度审查进行中，约需 1–3 分钟…')
  try {
    await api(`/api/${props.user}/curator`, { method: 'POST' })
    message.success('深度审查完成')
    await loadProposals()
  } catch (e) {
    message.error('深度审查失败: ' + e.message)
  } finally {
    curatorRunning.value = false
  }
}

async function judge(p, f, action) {
  try {
    const data = await api(`/api/${props.user}/proposal/action`, {
      method: 'POST',
      body: JSON.stringify({
        file: p.path, index: f.index, action,
        type: f.type, reason: f.reason,
      }),
    })
    if (data.content) p.content = data.content
    await loadActions()
    message.success(action === 'adopted'
      ? '已采纳——点「复制执行指令」交给 agent 落实'
      : '已忽略')
  } catch (e) {
    message.error(e.message)
  }
}

async function copyExecInstruction(p, f) {
  const text = (`请执行记忆质量提案 ${p.file} 第${f.index}条（[${f.severity}] ${f.type}）：`
    + `${f.reason} 涉及：${f.paths.join('、') || '—'} 建议：${f.proposal || '—'}。`
    + `执行完成后在提案文件的「裁决记录」下留痕。`)
  try {
    await navigator.clipboard.writeText(text)
    message.success('执行指令已复制，粘贴给任意 agent 即可')
  } catch (e) {
    message.error('复制失败：' + e.message)
  }
}

watch(() => props.user, reloadAll)
onMounted(reloadAll)
</script>

<style scoped>
.audit-page { max-width: 900px; margin: 0 auto; }
.markdown-body { line-height: 1.7; }
.run-active { background: rgba(51, 153, 255, 0.08); }
.finding-done { opacity: 0.55; }
</style>
