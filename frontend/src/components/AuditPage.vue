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

          <!-- 工作流条：人判断 → agent 执行 → 审计验证 -->
          <n-card size="small">
            <n-space align="center" justify="space-between">
              <n-space align="center" :size="6">
                <n-tag size="small" :type="attentionItems.length ? 'warning' : 'default'">待处理 {{ attentionItems.length }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="inProgressItems.length ? 'info' : 'default'">执行中 {{ inProgressItems.length }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="recheckItems.length ? 'warning' : 'default'">已执行待复审 {{ recheckItems.length }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="verifiedIds.size ? 'success' : 'default'">复审通过 {{ verifiedIds.size }}</n-tag>
                <n-text depth="3">·</n-text>
                <n-tag size="small" type="default">已处置 {{ humanActions.length }}</n-tag>
              </n-space>
              <n-select v-model:value="auditFilter" size="small"
                :options="auditFilterOptions" style="width: 170px" />
            </n-space>
            <n-text depth="3" style="font-size: 12px; display: block; margin-top: 6px">
              人只做判断（误报忽略 / 派发给 agent）；agent 执行修复并经 memory_audit_update
              汇报过程；复审由审计自动确认——已执行且下轮不再报告即为通过。
            </n-text>
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

            <!-- 进行中：agent 正在执行的问题（观测板） -->
            <n-card v-if="showInProgress && inProgressItems.length" size="small" title="进行中">
              <n-list>
                <n-list-item v-for="o in inProgressItems" :key="o.id">
                  <n-space vertical size="small">
                    <n-space justify="space-between" align="center">
                      <n-text>{{ o.desc }}</n-text>
                      <n-tag size="tiny" type="info">{{ eventLabel(lastEvent(o.id)?.event) }}</n-tag>
                    </n-space>
                    <n-text depth="3" style="font-size: 12px" v-if="lastEvent(o.id)?.note">
                      {{ lastEvent(o.id)?.identity || 'agent' }}：{{ lastEvent(o.id)?.note }}
                    </n-text>
                    <exec-timeline :issue-id="o.id" />
                  </n-space>
                </n-list-item>
              </n-list>
            </n-card>

            <!-- 待处理（含复审未过/受阻/复发），按检查类型分组 -->
            <n-card v-for="g in shownAttentionGroups" :key="g.kind" size="small" :title="g.title">
              <n-list>
                <n-list-item v-for="o in g.items" :key="o.id">
                  <n-space vertical size="small">
                    <n-space justify="space-between" align="center">
                      <n-text>{{ o.desc }}</n-text>
                      <n-space :size="4">
                        <n-tag v-if="o.state.label !== '待处理'" size="tiny" :type="o.state.type">{{ o.state.label }}</n-tag>
                        <n-button size="tiny" type="primary" secondary @click="copyIssueInstruction(o)">复制执行指令</n-button>
                        <n-button size="tiny" @click="dispose(o.id, 'dismissed', o.dismissLabel)">忽略</n-button>
                      </n-space>
                    </n-space>
                    <exec-timeline :issue-id="o.id" />
                  </n-space>
                </n-list-item>
              </n-list>
            </n-card>

            <!-- 复审通过：agent 已执行、本轮审计确认消除 -->
            <n-card v-if="showVerified && verifiedIds.size" size="small" title="复审通过（本轮审计确认消除）">
              <n-text depth="2" style="font-size: 13px">
                <n-tag v-for="id in [...verifiedIds]" :key="id" size="tiny" type="success" style="margin: 2px">{{ id }}</n-tag>
              </n-text>
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

          <!-- 判断与执行记录：人的处置（audit_actions）+ agent 的执行汇报 -->
          <n-card size="small" title="判断与执行记录">
            <n-empty v-if="!combinedLog.length" description="暂无记录" />
            <n-list v-else>
              <n-list-item v-for="(e, i) in combinedLog.slice(0, 50)" :key="i">
                <n-space justify="space-between" align="center">
                  <n-text :depth="2" style="font-size: 13px">
                    <n-tag size="tiny" :type="e.tagType">{{ e.verb }}</n-tag>
                    {{ e.actor }} · {{ e.id }}
                  </n-text>
                  <n-text depth="3" style="font-size: 12px">{{ e.ts }}{{ e.note ? ` · ${e.note}` : '' }}</n-text>
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
              <n-select v-model:value="proposalFilter" size="small"
                :options="proposalFilterOptions" style="width: 170px" />
            </n-space>
            <n-space align="center" :size="6" style="margin-top: 8px">
              <n-tag size="small" :type="pCount('pending') ? 'warning' : 'default'">待处理 {{ pCount('pending') }}</n-tag>
              <n-text depth="3">→</n-text>
              <n-tag size="small" :type="pCount('executing') ? 'info' : 'default'">执行中 {{ pCount('executing') }}</n-tag>
              <n-text depth="3">→</n-text>
              <n-tag size="small" :type="pCount('executed') ? 'success' : 'default'">已执行 {{ pCount('executed') }}</n-tag>
              <n-text depth="3">·</n-text>
              <n-tag v-if="pCount('blocked')" size="small" type="error">受阻 {{ pCount('blocked') }}</n-tag>
              <n-tag v-if="pCount('adopted')" size="small" type="warning">已采纳·未执行 {{ pCount('adopted') }}</n-tag>
              <n-tag size="small" type="default">已忽略 {{ pCount('dismissed') }}</n-tag>
            </n-space>
            <n-text depth="3" style="font-size: 12px; display: block; margin-top: 6px">
              提案是派给 agent 的工作项：派发即采纳（复制执行指令），误报/不做点忽略；
              agent 执行进度经 memory_audit_update 汇报。全部条目执行/忽略后提案自动结案，
              不再出现在 agent 的检索结果里。
            </n-text>
          </n-card>

          <n-empty v-if="!proposals.length"
            description="还没有质量提案——点上方「立即深度审查」生成，或等每周 timer 自动执行" />
          <n-card v-else-if="!visibleProposalCount" size="small">
            <n-text depth="3">当前筛选下没有提案条目。</n-text>
          </n-card>

          <n-card v-for="p in proposals" :key="p.file" size="small"
            :title="p.file + (proposalSettled(p) ? '　（已结案）' : '')">
            <n-space vertical size="small">
              <n-card v-for="f in filteredFindings(p)" :key="f.index" size="small"
                :bordered="true" :class="{ 'finding-done': findingSettled(p, f) }">
                <n-space vertical size="small">
                  <n-space justify="space-between" align="center">
                    <n-space align="center">
                      <n-tag size="tiny" :type="f.severity === 'high' ? 'error' : f.severity === 'medium' ? 'warning' : 'default'">
                        {{ f.severity }}
                      </n-tag>
                      <n-tag size="tiny">{{ f.type }}</n-tag>
                      <n-tag size="tiny" :type="findingState(p, f).type">{{ findingState(p, f).label }}</n-tag>
                    </n-space>
                    <n-space v-if="findingDispatchable(p, f)">
                      <n-button size="tiny" type="primary" secondary @click="copyExecInstruction(p, f)">复制执行指令</n-button>
                      <n-button size="tiny" @click="judge(p, f, 'dismissed')">忽略</n-button>
                    </n-space>
                  </n-space>
                  <n-text depth="2">{{ f.reason }}</n-text>
                  <n-text v-if="f.paths.length" depth="3" style="font-size: 12px">
                    涉及：{{ f.paths.join('、') }}
                  </n-text>
                  <n-text v-if="f.proposal" depth="3" style="font-size: 12px">建议：{{ f.proposal }}</n-text>
                  <exec-timeline :issue-id="`P:${p.path}:${f.index}`" />
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
import { ref, computed, watch, onMounted, defineComponent, h } from 'vue'
import {
  NSpace, NCard, NButton, NList, NListItem, NText, NTag, NTabs, NTabPane,
  NStatistic, NEmpty, NCollapse, NCollapseItem, NSelect, useMessage,
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
const actions = ref([])               // audit_actions 全量（人的判断）
const execEvents = ref([])            // audit_exec_events 全量（agent 的执行汇报，新在前）

// ---- 提案 ----
const proposals = ref([])             // [{file, path, content, findings}]
const disposed = ref(new Set())       // 已处置（忽略）过的审计问题 id——loadActions 填充

const AuditSection = {
  props: ['title', 'items'],
  setup(p) {
    return () => p.items?.length
      ? h(NCard, { size: 'small', title: `${p.title}（${p.items.length}）` }, () =>
          h(NList, () => p.items.map((item, i) => h(NListItem, { key: i }, () => item))))
      : null
  },
}

// 执行时间线（agent 汇报逐条展示，新在前）
const ExecTimeline = defineComponent({
  name: 'ExecTimeline',
  props: { issueId: String },
  setup(p) {
    return () => {
      const events = (execEvents.value || []).filter(e => e.issue_id === p.issueId)
      if (!events.length) return null
      return h('div', { class: 'exec-timeline' }, events.map(e => h('div', { class: 'exec-line', key: e.seq }, [
        h('span', { class: 'exec-ev' }, eventLabel(e.event)),
        h('span', { class: 'exec-meta' },
          `${e.ts.replace('T', ' ').slice(0, 16)}${e.identity ? ' · ' + e.identity : ''}`),
        e.note ? h('span', { class: 'exec-note' }, e.note) : null,
      ])))
    }
  },
})

const d1Id = c => 'D1:' + [c.a_title, c.b_title].sort().join('|')
const d3Id = c => `D3:${c.path}|${c.link}`
const d4Id = p => `D4:${p}`
const d5Parts = c => {
  const rest = c.startsWith('D5:') ? c.slice(3) : c
  const i = rest.indexOf('|')
  return i === -1 ? { title: rest, card: '' } : { title: rest.slice(0, i), card: rest.slice(i + 1) }
}

// ---- 扁平化当前报告的全部待关注问题（执行状态机在这里落位）----
// 已忽略（audit_actions 有行）直接剔除；执行中(executing/progress)进「进行中」，
// 其余（无汇报/已执行复审未过/受阻/复发）留在分组卡片里
const openItems = computed(() => {
  const a = auditData.value
  if (!a) return []
  const disposedIds = disposed.value
  const items = []
  const push = (id, kind, desc, dismissLabel) => {
    if (disposedIds.has(id)) return
    items.push({ id, kind, desc, dismissLabel, state: issueState(id) })
  }
  for (const c of a.title_duplicates || [])
    push(d1Id(c), 'D1', `${c.a_path} ↔ ${c.b_path} (score ${c.score})`, '标题重复误报')
  for (const c of a.collisions || [])
    push(`D2:${c.id}`, 'D2', `${c.a_path} ↔ ${c.b_path} (score ${c.score}) — A: ${c.a_text?.slice(0, 50)} / B: ${c.b_text?.slice(0, 50)}`, '撞车误报')
  for (const c of a.dangling_links || [])
    push(d3Id(c), 'D3', `${c.path}: [[${c.link}]]`, '非笔记引用')
  for (const p of a.stray || [])
    push(d4Id(p), 'D4', p, '无需归位')
  for (const c of a.dangling_cards || []) {
    const t = d5Parts(c)
    push(c, 'D5', `${t.title}：卡路径不存在（${t.card}）`, '暂不处理')
  }
  return items
})

const inProgressItems = computed(() =>
  openItems.value.filter(o => o.state.key === 'executing'))
const recheckItems = computed(() =>
  openItems.value.filter(o => o.state.key === 'executed'))
const attentionItems = computed(() =>
  openItems.value.filter(o => o.state.key !== 'executing'))
const hasOpenIssues = computed(() => openItems.value.length > 0)

// ---- 审计 tab 状态筛选（按状态机 key 精确匹配，动态出项）----
const auditFilter = ref('all')
const auditFilterOptions = computed(() => {
  const meta = { open: '待处理', executing: '执行中', executed: '已执行待复审',
                 blocked: '受阻', regressed: '复发' }
  const opts = [{ label: '全部状态', value: 'all' }]
  for (const [key, label] of Object.entries(meta)) {
    const n = openItems.value.filter(o => o.state.key === key).length
    if (n) opts.push({ label: `${label} (${n})`, value: key })
  }
  if (verifiedIds.value.size)
    opts.push({ label: `复审通过 (${verifiedIds.value.size})`, value: 'verified' })
  return opts
})
const shownAttentionGroups = computed(() => {
  const f = auditFilter.value
  const items = f === 'all' ? attentionItems.value
    : attentionItems.value.filter(o => o.state.key === f)
  const gmeta = {
    D1: '标题重复（D1）', D2: '语义撞车（D2）', D3: '悬空链接（D3）',
    D4: '游离文件（D4）', D5: '悬空主题卡（D5）',
  }
  return Object.entries(gmeta).map(([kind, title]) => ({
    kind, title, items: items.filter(o => o.kind === kind),
  })).filter(g => g.items.length)
})
const showInProgress = computed(() => ['all', 'executing'].includes(auditFilter.value))
const showVerified = computed(() => ['all', 'verified'].includes(auditFilter.value))

// ---- 执行状态机（读取端派生，不落库）----
// 状态直接从事件表（/audit/exec）派生——agent 一汇报，页面刷新即生效，
// 不用等下次审计；audit/last 里的 exec_status 只是服务端给 MCP 输出的镜像
const execLastMap = computed(() => {
  const last = {}
  for (const e of execEvents.value || [])
    if (!(e.issue_id in last)) last[e.issue_id] = e
  return last
})
const verifiedIds = computed(() => {
  const last = new Map()
  for (const e of execEvents.value || [])
    if (!last.has(e.issue_id)) last.set(e.issue_id, e.event)
  return new Set([...last.entries()].filter(([, ev]) => ev === 'verified').map(([id]) => id))
})

function issueState(id) {
  const st = execLastMap.value[id]
  if (!st) return { key: 'open', label: '待处理', type: 'default' }
  if (st.event === 'executing' || st.event === 'progress')
    return { key: 'executing', label: '执行中', type: 'info' }
  if (st.event === 'executed')
    return { key: 'executed', label: '已执行·复审未过', type: 'warning' }
  if (st.event === 'blocked')
    return { key: 'blocked', label: '受阻', type: 'error' }
  if (st.event === 'verified')
    return { key: 'regressed', label: '复发', type: 'warning' }
  return { key: 'open', label: '待处理', type: 'default' }
}
function lastEvent(id) {
  return (execEvents.value || []).find(e => e.issue_id === id) || null
}
function eventLabel(ev) {
  return { executing: '开始执行', progress: '过程汇报', executed: '执行完成',
           blocked: '受阻', verified: '复审通过' }[ev] || ev
}

// ---- 人的判断（audit_actions）：D 类处置 + P 类提案忽略/旧采纳 ----
const humanActions = computed(() => actions.value.filter(a => a.kind !== 'P'))
const pActions = computed(() => {
  const m = new Map()
  for (const a of actions.value) {
    if (a.kind !== 'P') continue
    m.set(a.id, a)
  }
  return m
})

// ---- 提案条目状态机（执行事件权威，人判断收口）----
// pending 待处理 / executing 执行中 / executed 已执行 / blocked 受阻 /
// adopted 已采纳·未执行（旧口径派发意图，需执行或忽略收口）/ dismissed 已忽略
function findingState(p, f) {
  const st = execLastMap.value[`P:${p.path}:${f.index}`]
  if (st) {
    if (st.event === 'executed' || st.event === 'verified')
      return { key: 'executed', label: '已执行', type: 'success' }
    if (st.event === 'executing' || st.event === 'progress')
      return { key: 'executing', label: '执行中', type: 'info' }
    if (st.event === 'blocked')
      return { key: 'blocked', label: '受阻', type: 'error' }
  }
  const a = pActions.value.get(`P:${p.path}:${f.index}`)
  if (a?.action === 'dismissed') return { key: 'dismissed', label: '已忽略', type: 'default' }
  if (a?.action === 'adopted') return { key: 'adopted', label: '已采纳·未执行', type: 'warning' }
  return { key: 'pending', label: '待处理', type: 'default' }
}
function findingDispatchable(p, f) {
  return ['pending', 'adopted', 'blocked'].includes(findingState(p, f).key)
}
function findingSettled(p, f) {
  return ['executed', 'dismissed'].includes(findingState(p, f).key)
}
function proposalSettled(p) {
  return p.findings.length > 0 && p.findings.every(f => findingSettled(p, f))
}

// ---- 提案统计与筛选 ----
const proposalFilter = ref('all')
const pCount = computed(() => (key) => {
  let n = 0
  for (const p of proposals.value)
    for (const f of p.findings || [])
      if (findingState(p, f).key === key) n += 1
  return n
})
const proposalFilterOptions = computed(() => {
  const opts = [{ label: '全部状态', value: 'all' }]
  const meta = {
    pending: '待处理', executing: '执行中', executed: '已执行',
    blocked: '受阻', adopted: '已采纳·未执行', dismissed: '已忽略',
  }
  for (const [key, label] of Object.entries(meta))
    if (pCount.value(key)) opts.push({ label: `${label} (${pCount.value(key)})`, value: key })
  return opts
})
function filteredFindings(p) {
  if (proposalFilter.value === 'all') return p.findings || []
  return (p.findings || []).filter(f => findingState(p, f).key === proposalFilter.value)
}
const visibleProposalCount = computed(() =>
  proposals.value.reduce((n, p) => n + filteredFindings(p).length, 0))

// ---- 判断与执行记录：人的处置 + agent 汇报合并按时间倒序 ----
const combinedLog = computed(() => {
  const rows = []
  for (const a of actions.value) {
    rows.push({
      ts: a.acted_at, id: a.id, note: a.note,
      verb: a.kind === 'P'
        ? (a.action === 'adopted' ? '提案已采纳' : '提案已忽略')
        : (a.action === 'resolved' ? '人·已处理' : '人·忽略'),
      tagType: a.action === 'resolved' || a.action === 'adopted' ? 'success' : 'default',
      actor: '人',
    })
  }
  for (const e of execEvents.value || []) {
    rows.push({
      ts: e.ts, id: e.issue_id, note: e.note,
      verb: `agent·${eventLabel(e.event)}`,
      tagType: e.event === 'executed' || e.event === 'verified' ? 'success'
        : e.event === 'blocked' ? 'error' : 'info',
      actor: e.identity || 'agent',
    })
  }
  rows.sort((x, y) => (x.ts < y.ts ? 1 : -1))
  return rows
})

const lastAuditTime = computed(() => lastAuditTs.value
  ? new Date(lastAuditTs.value * 1000).toLocaleString('zh-CN', { hour12: false }) : '')

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
    disposed.value = new Set(actions.value.map(a => a.id))
  } catch (e) { /* */ }
}
async function loadExecEvents() {
  try {
    const data = await api(`/api/${props.user}/audit/exec`)
    execEvents.value = data.events || []
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
    if (String(e.message).includes('未找到笔记')) {
      // 快照可能已被手动删除或 curator 过期清理：降级展示，不弹错误
      // （审计结果本身仍有效，处置历史以 audit_actions 表为权威）
      snapshotMarkdown.value = ''
      message.info('快照文件不存在（可能已被删除或过期清理）')
    } else {
      message.error('加载快照失败: ' + e.message)
    }
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
  await Promise.all([loadAuditState(), loadActions(), loadExecEvents(), loadRuns(), loadProposals()])
}

// ---- 动作 ----
async function runAudit() {
  auditing.value = true
  try {
    const data = await api(`/api/${props.user}/audit`, { method: 'POST' })
    auditData.value = data.audit
    lastAuditTs.value = Math.floor(Date.now() / 1000)
    message.success('审计完成')
    await Promise.all([loadRuns(), loadActions(), loadExecEvents()])
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
    message.success('已忽略——该问题不再重放')
  } catch (e) {
    message.error(e.message)
  }
}

// ---- 执行指令：复制给任意 agent，内嵌 memory_audit_update 汇报约定 ----
async function copyText(text) {
  // navigator.clipboard 仅在 secure context（HTTPS/localhost）可用——
  // LAN 上纯 IP 的 HTTP 访问是 insecure context，必须走 execCommand 兜底
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.focus()
  ta.select()
  try {
    if (!document.execCommand('copy')) throw new Error('浏览器拒绝了复制')
  } finally {
    document.body.removeChild(ta)
  }
}

const REPORT_PROTOCOL = (id) => (
  `执行要求：开始时调 memory_audit_update(issue_id="${id}", event="executing")；` +
  `关键动作用 event="progress" 汇报；完成后 event="executed" 附改动摘要；` +
  `受阻需要人工时 event="blocked" 说明卡点。` +
  `若属 yacmemo 系统缺陷而非记忆内容问题，直接向用户说明，勿强行修改记忆内容。` +
  `完成后重跑 memory_audit，该问题不再被报告即为复审通过。`)

function copyIssueInstruction(o) {
  const text = `请处理记忆审计问题 ${o.id}（${o.kind}）：${o.desc}。\n${REPORT_PROTOCOL(o.id)}`
  copyText(text).then(
    () => message.success('执行指令已复制，粘贴给任意 agent 即可'),
    e => message.error('复制失败：' + e.message))
}

async function runCurator() {
  curatorRunning.value = true
  message.info('深度审查进行中，约需 1–3 分钟…')
  try {
    await api(`/api/${props.user}/curator`, { method: 'POST' })
    message.success('深度审查完成')
    await loadProposals()
  } catch (e) {
    // 服务端消息已带「深度审查失败」前缀，避免重复
    message.error(e.message)
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
    message.success('已忽略——该条目不再出现；若提案全部条目收口将自动结案')
  } catch (e) {
    message.error(e.message)
  }
}

async function copyExecInstruction(p, f) {
  const id = `P:${p.path}:${f.index}`
  const text = (`请执行记忆质量提案 ${p.file} 第${f.index}条（[${f.severity}] ${f.type}）：`
    + `${f.reason} 涉及：${f.paths.join('、') || '—'} 建议：${f.proposal || '—'}。\n`
    + REPORT_PROTOCOL(id))
  try {
    await copyText(text)
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
.exec-timeline {
  border-left: 2px solid rgba(51, 153, 255, 0.35);
  padding-left: 10px;
  margin: 2px 0 2px 4px;
  font-size: 12px;
  line-height: 1.7;
}
.exec-line { color: rgba(0, 0, 0, 0.65); }
.exec-ev { font-weight: 600; margin-right: 6px; }
.exec-meta { color: rgba(0, 0, 0, 0.45); margin-right: 6px; }
.exec-note { color: rgba(0, 0, 0, 0.75); }
</style>
