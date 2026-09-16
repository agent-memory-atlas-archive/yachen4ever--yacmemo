<template>
  <div class="profile-page">
    <n-split direction="horizontal" :default-size="0.3" :min="0.15" :max="0.6">
      <template #1>
        <div class="profile-sections">
          <n-spin v-if="loading" size="small" />
          <template v-else>
            <n-list hover-clickable>
              <n-list-item
                v-for="section in sections"
                :key="section.name"
                :class="{ active: selectedSection === section.name }"
                @click="selectSection(section)"
                style="cursor: pointer"
              >
                <n-thing :title="section.name" :description="section.preview" />
              </n-list-item>
            </n-list>
            <n-button size="small" dashed block style="margin-top: 8px" @click="showAdd = true">
              + 新增小节
            </n-button>
          </template>
        </div>
      </template>
      <template #2>
        <div class="profile-editor">
          <n-empty v-if="!selectedSection" description="选择左侧小节查看或编辑" />
          <template v-else>
            <n-space justify="space-between" align="center" style="margin-bottom: 12px">
              <n-text strong style="font-size: 15px">{{ selectedSection }}</n-text>
              <n-button size="small" type="primary" @click="save" :loading="saving">保存</n-button>
            </n-space>
            <n-input
              v-model:value="editContent"
              type="textarea"
              :rows="20"
              style="font-family: monospace"
              placeholder="事实行用 - [类别] 内容 语法"
            />
            <n-divider />
            <n-text depth="3" style="font-size: 12px">预览：</n-text>
            <div class="markdown-body" v-html="renderedPreview" />
          </template>
        </div>
      </template>
    </n-split>

    <n-modal v-model:show="showAdd" preset="dialog" title="新增画像小节">
          <n-input v-model:value="newSectionName" placeholder="小节名（如沟通风格）" />
      <template #action>
        <n-space>
          <n-button @click="showAdd = false">取消</n-button>
          <n-button type="primary" @click="addSection" :disabled="!newSectionName.trim()">创建</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { NSplit, NSpin, NList, NListItem, NThing, NButton, NInput, NSpace, NText, NDivider, NEmpty, NModal, useMessage } from 'naive-ui'
import { marked } from 'marked'
import { api } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()

const loading = ref(true)
const saving = ref(false)
const sections = ref([])
const selectedSection = ref('')
const editContent = ref('')
const showAdd = ref(false)
const newSectionName = ref('')

const renderedPreview = computed(() => marked.parse(editContent.value || ''))

function parseSections(text) {
  if (!text || text.startsWith('（尚无')) return []
  const result = []
  const lines = text.split('\n')
  let current = null
  let content = []
  for (const line of lines) {
    const m = line.match(/^##\s+(.+?)\s*$/)
    if (m) {
      if (current) result.push({ name: current, preview: content[0]?.slice(0, 50) || '', content: content.join('\n') })
      current = m[1].trim()
      content = []
    } else if (current) {
      content.push(line)
    }
  }
  if (current) result.push({ name: current, preview: content[0]?.slice(0, 50) || '', content: content.join('\n') })
  return result
}

async function loadProfile() {
  loading.value = true
  try {
    const data = await api(`/api/${props.user}/profile`)
    sections.value = parseSections(data.content)
  } catch (e) {
    message.error('加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

function selectSection(section) {
  selectedSection.value = section.name
  editContent.value = section.content?.trim() || ''
}

async function save() {
  saving.value = true
  try {
    await api(`/api/${props.user}/profile`, {
      method: 'PUT',
      body: JSON.stringify({ section: selectedSection.value, content: editContent.value }),
    })
    message.success('已保存')
    await loadProfile()
  } catch (e) {
    message.error('保存失败: ' + e.message)
  } finally {
    saving.value = false
  }
}

async function addSection() {
  const name = newSectionName.value.trim()
  if (!name) return
  try {
    await api(`/api/${props.user}/profile`, {
      method: 'PUT',
      body: JSON.stringify({ section: name, content: '- [类别] 待补充' }),
    })
    message.success('已创建')
    showAdd.value = false
    newSectionName.value = ''
    await loadProfile()
    selectedSection.value = name
  } catch (e) {
    message.error(e.message)
  }
}

watch(() => props.user, () => { if (props.user) loadProfile() })
onMounted(() => { if (props.user) loadProfile() })
</script>

<style scoped>
.profile-page { height: 100%; }
.profile-sections { padding: 8px; height: 100%; overflow-y: auto; }
.profile-editor { padding: 0 16px; height: 100%; overflow-y: auto; }
.markdown-body { line-height: 1.7; margin-top: 8px; }
</style>
