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
                <n-tag size="small" :type="openCount ? 'warning' : 'default'">待处理 {{ openCount }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="inProgressCount ? 'info' : 'default'">执行中 {{ inProgressCount }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="recheckCount ? 'warning' : 'default'">已执行待复审 {{ recheckCount }}</n-tag>
                <n-text depth="3">→</n-text>
                <n-tag size="small" :type="verifiedIds.size ? 'success' : 'default'">复审通过 {{ verifiedIds.size }}</n-tag>
                <n-text depth="3">·</n-text>
                <n-tag v-if="blockedCount" size="small" type="error">受阻 {{ blockedCount }}</n-tag>
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

            <!-- 问题列表：与提案页同一套两级语言——
                 行 = 状态 + 类型 + 一句话描述（行内按钮），展开看详情与时间线 -->
            <n-collapse v-if="visibleIssues.length"
              :expanded-names="expandedIssues"
              @update:expanded-names="names => expandedIssues = names">
              <n-collapse-item v-for="o in visibleIssues" :key="o.id" :name="o.id">
                <template #header>
                  <n-space align="center" :size="8" :wrap-item="false">
                    <n-tag size="small" :type="o.st.type">{{ o.st.label }}</n-tag>
                    <n-tag size="small" :bordered="false">{{ kindLabel(o.kind) }}</n-tag>
                    <n-text style="font-size: 13px">{{ o.desc }}</n-text>
                  </n-space>
                </template>
                <template #header-extra>
                  <n-space v-if="issueDispatchable(o)" :size="4" @click.stop>
                    <n-button size="tiny" type="primary" secondary @click="copyIssueInstruction(o)">复制执行指令</n-button>
                    <n-button size="tiny" @click="dispose(o.id, 'dismissed', o.dismissLabel)">忽略</n-button>
                  </n-space>
                </template>
                <n-space vertical size="small">
                  <n-text v-for="(d, di) in o.detail" :key="di" depth="2" style="font-size: 12px">{{ d }}</n-text>
                  <n-text depth="3" style="font-size: 12px">问题 id：{{ o.id }}</n-text>
                  <exec-timeline :issue-id="o.id" />
                </n-space>
              </n-collapse-item>
            </n-collapse>
            <n-card v-else-if="hasOpenIssues" size="small">
              <n-text depth="3">当前筛选下没有问题。</n-text>
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
                  <n-text depth="3" style="font-size: 12px">{{ fmtTs(e.ts) }}{{ e.note ? ` · ${e.note}` : '' }}</n-text>
                </n-space>
              </n-list-item>
            </n-list>
          </n-card>

          <!-- 历史快照：左列清单 + 右列内容，不做上下堆叠 -->
          <n-card size="small" title="历史审计快照">
            <n-empty v-if="!runs.length" description="还没有审计记录" />
            <div v-else class="snapshot-layout">
              <div class="snapshot-list">
                <div v-for="r in runs" :key="r.file"
                  class="snapshot-item" :class="{ 'run-active': r.path === currentRun }"
                  @click="viewRun(r)">
                  <n-text :depth="r.path === currentRun ? 1 : 2" style="font-size: 13px">{{ fmtRunName(r.file) }}</n-text>
                  <n-text depth="3" style="font-size: 11px">{{ fmtSize(r.size) }}</n-text>
                </div>
              </div>
              <div class="snapshot-content">
                <n-text depth="3" style="font-size: 12px; display: block; margin-bottom: 6px">{{ snapshotTitle }}</n-text>
                <div v-if="snapshotMarkdown" class="markdown-body" v-html="renderMarkdown(snapshotMarkdown)" />
                <n-text v-else depth="3">左侧选择一份快照查看内容。</n-text>
              </div>
            </div>
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

          <!-- 两级结构：第一层提案（提案级状态汇总），点开是条目（条目级状态） -->
          <n-collapse :expanded-names="expandedProposals"
            @update:expanded-names="names => expandedProposals = names">
            <n-collapse-item v-for="p in sortedProposals" :key="p.file" :name="p.path">
              <template #header>
                <n-space align="center" :size="10" wrap>
                  <n-tag size="small" :type="proposalStatus(p).type">{{ proposalStatus(p).label }}</n-tag>
                  <n-text strong>{{ p.file }}</n-text>
                  <n-text depth="3" style="font-size: 12px">{{ proposalCountsLine(p) }}</n-text>
                </n-space>
              </template>
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
                      <n-tag v-if="f.review" size="tiny" type="info" :title="f.review">复审</n-tag>
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
            </n-collapse-item>
          </n-collapse>
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
import { parseFindings } from '../composables/proposal-parse.js'

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
          `${fmtTs(e.ts)}${e.identity ? ' · ' + e.identity : ''}`),
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
// 已忽略（audit_actions 有行）直接剔除；行内状态标签说清"这事要你干什么"
const kindNames = { D1: '标题重复', D2: '语义撞车', D3: '悬空链接', D4: '游离文件', D5: '悬空主题卡' }
const kindLabel = k => kindNames[k] || k
const openItems = computed(() => {
  const a = auditData.value
  if (!a) return []
  const disposedIds = disposed.value
  const items = []
  const push = (id, kind, desc, detail, dismissLabel) => {
    if (disposedIds.has(id)) return
    items.push({ id, kind, desc, detail, dismissLabel, state: issueState(id) })
  }
  for (const c of a.title_duplicates || [])
    push(d1Id(c), 'D1', `${c.a_path} ↔ ${c.b_path}`,
      [`相似度 ${c.score}`, '两篇标题近似——确认是否同一主题的两份拷贝'], '标题重复误报')
  for (const c of a.collisions || [])
    push(`D2:${c.id}`, 'D2', `${c.a_path} ↔ ${c.b_path}`,
      [`相似度 ${c.score}`, `A: ${c.a_text || ''}`, `B: ${c.b_text || ''}`,
       '两篇疑似在说同一件事——合并后派发，或判为误报'], '撞车误报')
  for (const c of a.dangling_links || [])
    push(d3Id(c), 'D3', `${c.path}: [[${c.link}]]`,
      ['链接目标按标题与路径都解析不到——补目标笔记或删掉链接'], '非笔记引用')
  for (const p of a.stray || [])
    push(d4Id(p), 'D4', p, ['散文件不属于任何注册主题——归位到主题目录，或删除'], '无需归位')
  for (const c of a.dangling_cards || []) {
    const t = d5Parts(c)
    push(c, 'D5', `${t.title}：卡路径不存在（${t.card}）`,
      ['注册表指向的 abstract 不存在——修正注册表路径或重建卡'], '暂不处理')
  }
  return items
})

const hasOpenIssues = computed(() => openItems.value.length > 0)

// ---- 问题级状态标签（与提案页同一套话术：说清"这事要你干什么"）----
function issueStatus(state) {
  switch (state.key) {
    case 'blocked': return { key: 'blocked', label: '受阻·需要你介入', type: 'error', order: 0 }
    case 'open': return { key: 'open', label: '待处理·等你决定', type: 'warning', order: 1 }
    case 'regressed': return { key: 'regressed', label: '复发·重新处理', type: 'warning', order: 1 }
    case 'executing': return { key: 'executing', label: '执行中·agent 正在做', type: 'info', order: 2 }
    case 'executed': return { key: 'executed', label: '已执行·待复审', type: 'default', order: 3 }
    default: return { key: 'open', label: '待处理·等你决定', type: 'warning', order: 1 }
  }
}

// ---- 状态筛选 ----
const auditFilter = ref('all')
const issueRows = computed(() =>
  openItems.value
    .map(o => ({ ...o, st: issueStatus(o.state) }))
    .sort((a, b) => a.st.order - b.st.order
      || a.kind.localeCompare(b.kind) || a.desc.localeCompare(b.desc)))
const visibleIssues = computed(() => {
  const f = auditFilter.value
  return f === 'all' || f === 'verified'
    ? issueRows.value
    : issueRows.value.filter(o => o.st.key === f)
})
const openCount = computed(() =>
  issueRows.value.filter(o => ['open', 'regressed'].includes(o.st.key)).length)
const inProgressCount = computed(() =>
  issueRows.value.filter(o => o.st.key === 'executing').length)
const recheckCount = computed(() =>
  issueRows.value.filter(o => o.st.key === 'executed').length)
const blockedCount = computed(() =>
  issueRows.value.filter(o => o.st.key === 'blocked').length)
function issueDispatchable(o) {
  return ['open', 'regressed', 'blocked'].includes(o.st.key)
}
const auditFilterOptions = computed(() => {
  const meta = { open: '待处理', executing: '执行中', executed: '已执行待复审',
                 blocked: '受阻', regressed: '复发' }
  const opts = [{ label: '全部状态', value: 'all' }]
  for (const [key, label] of Object.entries(meta)) {
    const n = issueRows.value.filter(o => o.st.key === key).length
    if (n) opts.push({ label: `${label} (${n})`, value: key })
  }
  if (verifiedIds.value.size)
    opts.push({ label: `复审通过 (${verifiedIds.value.size})`, value: 'verified' })
  return opts
})
const showVerified = computed(() => ['all', 'verified'].includes(auditFilter.value))

// 展开管理：默认展开需要你关注的（待处理/受阻/复发），执行中的折叠；
// 按状态筛选时全部展开
const expandedIssues = ref([])
function autoExpandIssues() {
  expandedIssues.value = auditFilter.value === 'all'
    ? issueRows.value.filter(o => o.st.order <= 1).map(o => o.id)
    : visibleIssues.value.map(o => o.id)
}
watch(auditFilter, autoExpandIssues)

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

// ---- 提案级状态汇总（给"什么都不懂的用户"一眼看懂）----
// 受阻 > 待处理（有等你决定的条目）> 执行中 > 已结案（全部收口）
function proposalStatus(p) {
  const keys = (p.findings || []).map(f => findingState(p, f).key)
  if (!keys.length) return { key: 'empty', label: '无条目', type: 'default' }
  if (keys.includes('blocked')) return { key: 'blocked', label: '受阻·需要你介入', type: 'error' }
  if (keys.includes('pending') || keys.includes('adopted'))
    return { key: 'attention', label: '待处理·等你决定', type: 'warning' }
  if (keys.includes('executing')) return { key: 'executing', label: '执行中·agent 正在做', type: 'info' }
  return { key: 'settled', label: '已结案·全部完成', type: 'success' }
}
function proposalCountsLine(p) {
  const c = {}
  for (const f of p.findings || []) {
    const k = findingState(p, f).key
    c[k] = (c[k] || 0) + 1
  }
  const order = [['pending', '待处理'], ['executing', '执行中'], ['blocked', '受阻'],
                 ['executed', '已执行'], ['adopted', '已采纳·未执行'], ['dismissed', '已忽略']]
  const parts = order.filter(([k]) => c[k]).map(([k, label]) => `${label} ${c[k]}`)
  return parts.length ? parts.join(' · ') : '无条目'
}
const statusOrder = { blocked: 0, attention: 1, executing: 2, settled: 3, empty: 4 }
const sortedProposals = computed(() =>
  [...proposals.value].sort((a, b) => {
    const d = statusOrder[proposalStatus(a).key] - statusOrder[proposalStatus(b).key]
    return d !== 0 ? d : (a.file < b.file ? 1 : -1)
  }))

// 展开管理：默认展开"需要你关注"的提案，已结案折叠；按状态筛选时全展开
// （watch 放在 proposalFilter 定义之后——见下方筛选块）
const expandedProposals = ref([])
function autoExpandProposals() {
  const visible = sortedProposals.value.filter(p => filteredFindings(p).length)
  expandedProposals.value = proposalFilter.value === 'all'
    ? visible.filter(p => proposalStatus(p).key !== 'settled').map(p => p.path)
    : visible.map(p => p.path)
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
watch(proposalFilter, autoExpandProposals)

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

function fmtSize(n) { return n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B` }
function fmtRunName(file) {
  const m = file.match(/^(\d{4})(\d{2})(\d{2})(?:-(\d{2})(\d{2})(\d{2}))?\.md$/)
  if (!m) return file
  const base = `${m[2]}-${m[3]}`
  return m[4] ? `${base} ${m[4]}:${m[5]}` : base
}
function renderMarkdown(text) { return marked.parse(text || '') }
// 事件时间统一转本地时区显示（后端存 UTC ISO，新手看 04:02 会懵）
function fmtTs(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return isNaN(d)
    ? String(ts).replace('T', ' ').slice(0, 16)
    : d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// ---- 数据加载 ----
async function loadAuditState() {
  try {
    const data = await api(`/api/${props.user}/audit/last`)
    if (data.audit) {
      auditData.value = data.audit
      lastAuditTs.value = data.ts || 0
      autoExpandIssues()
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
    autoExpandProposals()
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
    autoExpandIssues()
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
  const text = (`请执行记忆质量提案 ${p.file} 第${f.index}条（全局序号${f.review ? `，出自${f.review}` : ''}；`
    + `[${f.severity}] ${f.type}）：${f.reason} 涉及：${f.paths.join('、') || '—'} 建议：${f.proposal || '—'}。\n`
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
.audit-page { max-width: 1080px; margin: 0 auto; }
.markdown-body { line-height: 1.7; }
.markdown-body :deep(table) { border-collapse: collapse; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid rgba(0,0,0,0.15); padding: 4px 8px; }
.run-active { background: rgba(51, 153, 255, 0.12); }
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
.snapshot-layout { display: flex; gap: 14px; align-items: flex-start; }
.snapshot-list { width: 210px; flex-shrink: 0; display: flex; flex-direction: column; gap: 2px; }
.snapshot-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 8px; border-radius: 4px; cursor: pointer;
}
.snapshot-item:hover { background: rgba(51, 153, 255, 0.08); }
.snapshot-content {
  flex: 1; min-width: 0; overflow-x: auto;
  border-left: 1px solid rgba(0, 0, 0, 0.08); padding-left: 14px;
}
@media (max-width: 800px) {
  .snapshot-layout { flex-direction: column; }
  .snapshot-list { width: 100%; flex-direction: row; flex-wrap: wrap; }
  .snapshot-content { border-left: none; padding-left: 0; width: 100%; }
}
</style>
