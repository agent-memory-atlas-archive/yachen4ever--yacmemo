<template>
  <div class="identities-page">
    <n-alert type="info" :show-icon="false" style="margin-bottom: 16px">
      <n-text strong>专属记忆层级模型：</n-text>
      user 层（topics/、journal/、PROFILE.md）所有身份共享；
      <n-text code>agents/&lt;agent&gt;/</n-text> 下平铺文件为 agent 层（同 agent 跨设备共享）；
      <n-text code>agents/&lt;agent&gt;/&lt;device&gt;/</n-text> 为 identity 层（本机专属）。
      不同 identity 互相不可见：memory_search 只返回 user 层 + 自己的专属区，
      memory_context 自动注入自己的两份必读。
    </n-alert>

    <n-space justify="space-between" align="center" style="margin-bottom: 12px">
      <n-text strong style="font-size: 15px">已有 identity（扫描 agents/ 目录）</n-text>
      <n-space>
        <n-button size="small" @click="load" :loading="loading">刷新</n-button>
        <n-button size="small" type="primary" @click="showCreate = true">新建 identity</n-button>
      </n-space>
    </n-space>

    <n-spin v-if="loading" size="small" />
    <n-empty v-else-if="!rows.length" description="尚无 identity——agents/ 目录为空。点「新建 identity」生成第一个 token" />
    <n-collapse v-else>
      <n-collapse-item v-for="row in rows" :key="row.agent" :name="row.agent">
        <template #header>
          <n-space align="center">
            <n-text strong>{{ row.agent }}</n-text>
            <n-tag size="small" :type="row.devices.length ? 'info' : 'default'">
              {{ row.devices.length }} 设备
            </n-tag>
            <n-tag size="small">agent 层 {{ row.shared_files }} 文件</n-tag>
          </n-space>
        </template>
        <n-text depth="3" style="font-size: 12px">
          agent 层共享目录：agents/{{ row.agent }}/（每个设备一份必读：<n-code :code="`${row.agent}_${row.device}`" language="text" />）
        </n-text>
        <n-list v-if="row.devices.length" style="margin-top: 8px">
          <n-list-item v-for="d in row.devices" :key="d.device">
            <n-thing :title="d.device"
              :description="`agents/${row.agent}/${d.device}/ · ${d.notes} 个笔记`" />
          </n-list-item>
        </n-list>
        <n-text v-else depth="3" style="font-size: 12px">（尚无设备子树）</n-text>
      </n-collapse-item>
    </n-collapse>

    <n-modal v-model:show="showCreate" preset="dialog" title="新建 identity" :show-icon="false">
      <n-space vertical>
        <n-input v-model:value="newAgent" placeholder="agent 名（如 teleagent）" />
        <n-input v-model:value="newDevice" placeholder="设备名（如 r9000x）" />
        <n-text depth="3" style="font-size: 12px">
          名称限小写字母/数字/短横线（不含下划线）。token = 设备名_agent名，确定性拼接、可随时重建。
        </n-text>
        <template v-if="created">
          <n-alert type="success" :show-icon="false">
            token：<n-text code>{{ created.token }}</n-text>
          </n-alert>
          <n-input :value="claudeSnippet" type="textarea" :rows="2" readonly />
          <n-input :value="jsonSnippet" type="textarea" :rows="5" readonly />
        </template>
      </n-space>
      <template #action>
        <n-space>
          <n-button @click="closeCreate">关闭</n-button>
          <n-button type="primary" :loading="creating"
                    :disabled="!newAgent.trim() || !newDevice.trim()"
                    @click="create">
            {{ created ? '重新生成' : '生成 token' }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import {
  NAlert, NButton, NCollapse, NCollapseItem, NCode, NEmpty, NInput,
  NList, NListItem, NModal, NSpace, NSpin, NTag, NText, NThing, useMessage,
} from 'naive-ui'
import { api } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const loading = ref(false)
const rows = ref([])
const showCreate = ref(false)
const creating = ref(false)
const newAgent = ref('')
const newDevice = ref('')
const created = ref(null)

const claudeSnippet = computed(() => created.value
  ? `claude mcp add --transport http yacmemo http://<服务器>:9721/${props.user}/mcp --header "Authorization: Bearer ${created.value.token}"`
  : '')
const jsonSnippet = computed(() => created.value
  ? JSON.stringify({
      mcpServers: {
        yacmemo: {
          url: `http://<服务器>:9721/${props.user}/mcp`,
          headers: { Authorization: `Bearer ${created.value.token}` },
        },
      },
    }, null, 2)
  : '')

async function load() {
  if (!props.user) return
  loading.value = true
  try {
    const data = await api(`/api/${props.user}/identities`)
    rows.value = data.identities
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function create() {
  creating.value = true
  try {
    const data = await api(`/api/${props.user}/identities`, {
      method: 'POST',
      body: JSON.stringify({ agent: newAgent.value, device: newDevice.value }),
    })
    created.value = data
    message.success(`已生成 token: ${data.token}`)
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    creating.value = false
  }
}

function closeCreate() {
  showCreate.value = false
  created.value = null
  newAgent.value = ''
  newDevice.value = ''
}

watch(() => props.user, load, { immediate: true })
</script>

<style>
.identities-page { max-width: 860px; }
</style>
