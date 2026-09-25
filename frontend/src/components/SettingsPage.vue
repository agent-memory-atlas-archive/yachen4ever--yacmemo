<template>
  <div class="settings-page">
    <n-tabs type="line" animated>
      <!-- 用户管理 -->
      <n-tab-pane name="users" tab="用户">
        <n-space vertical size="large">
          <n-card size="small" title="用户列表">
            <template #header-extra>
              <n-button size="small" type="primary" @click="openAdd">新增用户</n-button>
            </template>
            <n-list>
              <n-list-item v-for="u in users" :key="u.id">
                <n-space justify="space-between" align="center" :wrap="false">
                  <n-space vertical size="small">
                    <n-space align="center" :size="8">
                      <n-text strong>{{ u.id }}</n-text>
                      <n-tag v-if="u.mounted" size="tiny" type="success">已挂载</n-tag>
                      <n-tag v-else size="tiny" type="warning">未挂载（需重启）</n-tag>
                      <n-tag v-if="!u.root_exists" size="tiny" type="error">目录缺失</n-tag>
                    </n-space>
                    <n-text depth="3" style="font-size: 12px">{{ u.root }}</n-text>
                    <n-text v-if="u.git_user_name" depth="3" style="font-size: 12px">
                      git: {{ u.git_user_name }} &lt;{{ u.git_user_email }}&gt;
                    </n-text>
                  </n-space>
                  <n-space :size="4">
                    <n-button size="tiny" @click="openEdit(u)">编辑</n-button>
                    <n-button size="tiny" type="error" ghost @click="openDelete(u)">删除</n-button>
                  </n-space>
                </n-space>
              </n-list-item>
            </n-list>
            <n-text depth="3" style="font-size: 12px; display: block; margin-top: 8px">
              新增/编辑/删除都会先校验再写回 config.toml（自动备份）。新用户与
              root 变更需要重启服务才会挂载 MCP 端点。
            </n-text>
          </n-card>
        </n-space>
      </n-tab-pane>

      <!-- 服务配置 -->
      <n-tab-pane name="service" tab="服务配置">
        <n-space vertical size="large">
          <n-card size="small" title="Embedding（语义检索与撞车检测）">
            <n-space vertical size="small">
              <n-grid :cols="2" :x-gap="12" responsive="screen">
                <n-gi><n-input v-model:value="embForm.base_url" placeholder="base_url（OpenAI 兼容，如 http://host:11235/v1）" /></n-gi>
                <n-gi><n-input v-model:value="embForm.model" placeholder="模型名" /></n-gi>
                <n-gi><n-input v-model:value="embForm.api_key" type="password" show-password-on="click" placeholder="api_key（可空）" /></n-gi>
                <n-gi><n-space :size="8"><n-input-number v-model:value="embForm.dimensions" placeholder="维度" style="width: 130px" /><n-input-number v-model:value="embForm.timeout" placeholder="超时秒" style="width: 130px" /></n-space></n-gi>
              </n-grid>
              <n-space align="center">
                <n-button size="small" @click="testEmbedding" :loading="testingEmb">测试连接</n-button>
                <n-text v-if="embTest" :type="embTest.ok ? 'success' : 'error'" style="font-size: 12px">
                  {{ embTest.ok ? `正常 · ${embTest.latency_ms}ms · ${embTest.dims} 维` : `失败：${embTest.error}` }}
                </n-text>
              </n-space>
            </n-space>
          </n-card>

          <n-card size="small" title="Curator（每周质量审查）">
            <n-space vertical size="small">
              <n-space align="center">
                <n-switch v-model:value="curForm.enabled" size="small" />
                <n-text>{{ curForm.enabled ? '启用' : '停用' }}</n-text>
                <n-text depth="3" style="font-size: 12px">模型调用只提案、绝不执行</n-text>
              </n-space>
              <n-grid :cols="2" :x-gap="12" responsive="screen">
                <n-gi><n-input v-model:value="curForm.base_url" placeholder="base_url（OpenAI 兼容 chat 端点）" /></n-gi>
                <n-gi><n-input v-model:value="curForm.model" placeholder="模型名" /></n-gi>
                <n-gi><n-input v-model:value="curForm.api_key" type="password" show-password-on="click" placeholder="api_key（可空）" /></n-gi>
                <n-gi><n-space :size="8">
                  <n-input-number v-model:value="curForm.max_tokens" placeholder="max_tokens" style="width: 130px" />
                  <n-input-number v-model:value="curForm.timeout" placeholder="超时秒" style="width: 130px" />
                  <n-input-number v-model:value="curForm.audit_retention_days" placeholder="快照保留天数" style="width: 150px" />
                </n-space></n-gi>
              </n-grid>
              <n-space align="center">
                <n-button size="small" @click="testCurator" :loading="testingCur">测试连接</n-button>
                <n-text v-if="curTest" :type="curTest.ok ? 'success' : 'error'" style="font-size: 12px">
                  {{ curTest.ok ? `正常 · ${curTest.latency_ms}ms · 回复：${curTest.reply}` : `失败：${curTest.error}` }}
                </n-text>
              </n-space>
            </n-space>
          </n-card>

          <n-card size="small" title="保存">
            <n-space align="center">
              <n-button type="primary" @click="saveStructured" :loading="savingStructured">保存配置</n-button>
              <n-checkbox v-model:checked="structuredRestart">保存并重启服务</n-checkbox>
              <n-text depth="3" style="font-size: 12px">
                保存前自动校验并备份原 config.toml；只改动上述字段，注释与顺序保留。
              </n-text>
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
        </n-space>
      </n-tab-pane>

      <!-- config.toml 原文（高级） -->
      <n-tab-pane name="config" tab="config.toml（高级）">
        <n-space vertical>
          <n-space>
            <n-button type="primary" @click="saveConfig" :loading="configSaving">保存</n-button>
            <n-checkbox v-model:checked="configRestart">保存并重启服务</n-checkbox>
            <n-button size="small" @click="loadConfig">重新加载</n-button>
          </n-space>
          <n-input v-model:value="configText" type="textarea" :rows="24"
            style="font-family: monospace; font-size: 13px" />
          <n-text depth="3" style="font-size: 12px">
            高级模式：直接编辑 TOML 原文。保存前自动校验 TOML 语法与配置结构，并备份原文件。
            常规改动建议用「服务配置」表单。
          </n-text>
        </n-space>
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
    </n-tabs>

    <!-- 新增/编辑用户 -->
    <n-modal v-model:show="userModal" preset="card" :title="editingId ? `编辑用户 ${editingId}` : '新增用户'"
      style="width: 520px">
      <n-space vertical size="small">
        <n-input v-if="!editingId" v-model:value="userForm.id" placeholder="用户 id（字母/数字/下划线/连字符）" />
        <n-input v-model:value="userForm.root" placeholder="记忆根目录（绝对路径，自动创建）" />
        <n-input v-model:value="userForm.git_user_name" placeholder="git 提交名（可空）" />
        <n-input v-model:value="userForm.git_user_email" placeholder="git 提交邮箱（可空）" />
        <n-checkbox v-model:checked="userForm.restart">保存并重启服务（新用户/root 变更必须重启才会生效）</n-checkbox>
      </n-space>
      <template #action>
        <n-button @click="userModal = false">取消</n-button>
        <n-button type="primary" :loading="userBusy" @click="submitUser">
          {{ editingId ? '保存' : '创建' }}
        </n-button>
      </template>
    </n-modal>

    <!-- 删除用户 -->
    <n-modal v-model:show="delModal" preset="card" :title="`删除用户 ${deletingId}`" style="width: 520px">
      <n-space vertical size="small">
        <n-text type="warning">
          将把「{{ deletingId }}」从 config.toml 移除并重启服务。默认保留其记忆目录与 git 历史：
          <n-text code>{{ deletingRoot }}</n-text>
        </n-text>
        <n-checkbox v-model:checked="delPurge">
          同时删除记忆目录（不可恢复！git 历史一并消失）
        </n-checkbox>
        <n-input v-model:value="delConfirm" placeholder="输入用户 id 以确认删除" />
        <n-text depth="3" style="font-size: 12px">
          服务重启后该用户的 MCP 端点与 WebUI 页面才会完全消失。
        </n-text>
      </n-space>
      <template #action>
        <n-button @click="delModal = false">取消</n-button>
        <n-button type="error" :disabled="delConfirm !== deletingId" :loading="userBusy"
          @click="submitDelete">确认删除</n-button>
      </template>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import {
  NTabs, NTabPane, NCard, NSpace, NButton, NList, NListItem, NText, NTag,
  NInput, NCheckbox, NInputNumber, NGrid, NGi, NSwitch, NModal, NEmpty,
  NPopconfirm, NSelect, NDataTable, useMessage,
} from 'naive-ui'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

// ---- 用户管理 ----
const users = ref([])
const userModal = ref(false)
const editingId = ref('')
const userForm = ref({ id: '', root: '', git_user_name: '', git_user_email: '', restart: true })
const userBusy = ref(false)
const delModal = ref(false)
const deletingId = ref('')
const deletingRoot = ref('')
const delPurge = ref(false)
const delConfirm = ref('')

async function loadUsers() {
  try {
    const data = await api('/api/users')
    users.value = data.users || []
  } catch (e) { message.error(e.message) }
}

function openAdd() {
  editingId.value = ''
  userForm.value = { id: '', root: '', git_user_name: '', git_user_email: '', restart: true }
  userModal.value = true
}

function openEdit(u) {
  editingId.value = u.id
  userForm.value = { id: u.id, root: u.root, git_user_name: u.git_user_name,
                     git_user_email: u.git_user_email, restart: false }
  userModal.value = true
}

async function submitUser() {
  userBusy.value = true
  try {
    const path = editingId.value ? '/api/users/update' : '/api/users/add'
    const data = await api(path, {
      method: 'POST',
      body: JSON.stringify({ ...userForm.value, id: editingId.value || userForm.value.id }),
    })
    userModal.value = false
    message.success(data.restarting ? '已保存，服务重启中…重启完成后自动刷新' : '已保存')
    if (data.restarting) setTimeout(() => window.location.reload(), 2500)
    else await loadUsers()
  } catch (e) {
    message.error(e.message)
  } finally {
    userBusy.value = false
  }
}

function openDelete(u) {
  deletingId.value = u.id
  deletingRoot.value = u.root
  delPurge.value = false
  delConfirm.value = ''
  delModal.value = true
}

async function submitDelete() {
  userBusy.value = true
  try {
    const data = await api('/api/users/delete', {
      method: 'POST',
      body: JSON.stringify({ id: deletingId.value, confirm_id: delConfirm.value,
                             purge: delPurge.value, restart: true }),
    })
    delModal.value = false
    message.success(data.purged ? '用户与记忆目录已删除，服务重启中…' : '用户已移出配置，服务重启中…')
    setTimeout(() => window.location.reload(), 2500)
  } catch (e) {
    message.error(e.message)
  } finally {
    userBusy.value = false
  }
}

// ---- 服务配置（结构化） ----
const embForm = ref({ base_url: '', api_key: '', model: '', dimensions: 1024, timeout: 30 })
const curForm = ref({ enabled: false, base_url: '', api_key: '', model: '', max_tokens: 4096,
                      timeout: 180, audit_retention_days: 7 })
const embTest = ref(null)
const curTest = ref(null)
const testingEmb = ref(false)
const testingCur = ref(false)
const savingStructured = ref(false)
const structuredRestart = ref(false)

async function loadStructured() {
  try {
    const data = await api('/api/config/structured')
    embForm.value = { ...embForm.value, ...data.embedding }
    curForm.value = { ...curForm.value, ...data.curator }
  } catch (e) { /* 无配置文件时保持默认 */ }
}

async function testEmbedding() {
  testingEmb.value = true
  embTest.value = null
  try {
    embTest.value = await api('/api/config/test-embedding', {
      method: 'POST', body: JSON.stringify(embForm.value),
    })
  } catch (e) { embTest.value = { ok: false, error: e.message } }
  testingEmb.value = false
}

async function testCurator() {
  testingCur.value = true
  curTest.value = null
  try {
    curTest.value = await api('/api/config/test-curator', {
      method: 'POST', body: JSON.stringify(curForm.value),
    })
  } catch (e) { curTest.value = { ok: false, error: e.message } }
  testingCur.value = false
}

async function saveStructured() {
  savingStructured.value = true
  try {
    const data = await api('/api/config/structured', {
      method: 'POST',
      body: JSON.stringify({ embedding: embForm.value, curator: curForm.value,
                             restart: structuredRestart.value }),
    })
    message.success(data.restarting ? '已保存，服务重启中…重启完成后自动刷新' : `已保存（备份: ${data.backup}）`)
    if (data.restarting) setTimeout(() => window.location.reload(), 2500)
  } catch (e) {
    message.error('保存失败: ' + e.message)
  } finally {
    savingStructured.value = false
  }
}

// ---- config.toml 原文（高级模式） ----
const configText = ref('')
const configSaving = ref(false)
const configRestart = ref(false)

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

// ---- 维护 ----
async function runReindex() {
  try {
    await api(`/api/${props.user}/reindex`, { method: 'POST' })
    message.success('索引已全量重建')
  } catch (e) {
    message.error('重建失败: ' + e.message)
  }
}

// ---- 使用记录 ----
const usageRows = ref([])
const usageFilter = ref(null)
const toolOptions = [
  'memory_search', 'memory_read', 'memory_write', 'memory_edit',
  'memory_edit_section', 'memory_move', 'memory_delete', 'memory_audit',
  'memory_audit_update', 'memory_list', 'memory_context', 'topic_list',
  'topic_register', 'topic_unregister', 'archive_topic',
  'get_user_preference', 'update_user_preference', 'integration_check',
  'webui:profile_save', 'webui:note_save', 'webui:note_create',
].map(t => ({ label: t, value: t }))

const usageColumns = [
  { title: '时间', key: 'ts', width: 150, render: (r) => r.ts?.slice(5, 19) || '' },
  { title: '用户', key: 'user', width: 70 },
  { title: '工具', key: 'tool', width: 160 },
  { title: '摘要', key: 'summary', ellipsis: { tooltip: true } },
  { title: '耗时', key: 'ms', width: 60, render: (r) => r.ok ? `${r.ms}ms` : '—' },
  { title: '状态', key: 'ok', width: 50,
    render: (r) => h(NTag, { size: 'tiny', type: r.ok ? 'success' : 'error' }, () => r.ok ? '✓' : '✗') },
]

async function loadUsage() {
  try {
    const data = await api(`/api/usage${params({ limit: 100, tool: usageFilter.value })}`)
    usageRows.value = data.rows
  } catch (e) { message.error(e.message) }
}

onMounted(() => {
  loadUsers()
  loadStructured()
  loadConfig()
  loadUsage()
})
</script>

<style scoped>
.settings-page { max-width: 1080px; margin: 0 auto; }
</style>
