<template>
  <div class="topics-page">
    <n-split direction="horizontal" :max="0.8" :min="0.15" :default-size="0.25">
      <template #1>
        <div class="topic-tree">
          <n-spin v-if="loading" size="small" />
          <n-tree
            v-else
            :data="treeData"
            :selectable="true"
            @update:selected-keys="onSelect"
            block-line
            expand-on-click
            :default-expanded-keys="expandedKeys"
          />
        </div>
      </template>
      <template #2>
        <div class="topic-detail">
          <n-empty v-if="!selectedNote" description="选择左侧主题或笔记查看内容" />
          <template v-else>
            <n-space justify="space-between" align="center" style="margin-bottom: 12px">
              <n-text strong style="font-size: 15px">{{ selectedNoteTitle }}</n-text>
              <n-space>
                <n-button size="small" @click="toggleEdit">
                  {{ editing ? '预览' : '编辑' }}
                </n-button>
                <n-button size="small" type="error" ghost @click="handleDelete"
                  v-if="!isAbstract">删除</n-button>
              </n-space>
            </n-space>
            <n-input
              v-if="editing"
              v-model:value="editContent"
              type="textarea"
              :rows="20"
              style="font-family: monospace"
            />
            <div v-else class="markdown-body" v-html="renderedContent" />
          </template>
        </div>
      </template>
    </n-split>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { NSplit, NSpin, NTree, NEmpty, NText, NSpace, NButton, NInput, useMessage, useDialog } from 'naive-ui'
import { marked } from 'marked'
import { api, params } from '../composables/api.js'

const props = defineProps({ user: String })
const message = useMessage()
const dialog = useDialog()

const loading = ref(true)
const topics = ref([])
const notes = ref([])
const selectedNote = ref(null)
const selectedNoteTitle = ref('')
const editing = ref(false)
const editContent = ref('')
const isAbstract = ref(false)

const expandedKeys = ref([])

const treeData = computed(() => {
  const result = []
  // Active topics
  const activeChildren = []
  for (const t of topics.value.filter(t => !t.archived)) {
    const topicNotes = notes.value.filter(n =>
      n.path.startsWith(`topics/${t.title}/`) && n.path !== t.card)
    activeChildren.push({
      key: `topic:${t.title}`,
      label: t.title,
      children: [
        { key: `note:${t.card}`, label: 'abstract', isLeaf: true },
        ...topicNotes.map(n => ({
          key: `note:${n.path}`,
          label: n.title || n.path.split('/').pop().replace('.md', ''),
          isLeaf: true,
        })),
      ],
    })
  }
  result.push({ key: 'active', label: `活跃主题 (${activeChildren.length})`, children: activeChildren })
  // Archived topics
  const archived = topics.value.filter(t => t.archived)
  if (archived.length) {
    result.push({
      key: 'archived',
      label: `已归档 (${archived.length})`,
      children: archived.map(t => ({
        key: `topic:${t.title}`,
        label: t.title,
        children: [{ key: `note:${t.card}`, label: 'abstract', isLeaf: true }],
      })),
    })
  }
  // Free zones
  result.push({ key: 'free', label: '免注册区', children: [
    { key: 'zone:journal', label: 'journal', children: notes.value
      .filter(n => n.path.startsWith('journal/'))
      .map(n => ({ key: `note:${n.path}`, label: n.title || n.path.split('/').pop().replace('.md',''), isLeaf: true })) },
    { key: 'zone:curator', label: 'curator', children: notes.value
      .filter(n => n.path.startsWith('curator/'))
      .map(n => ({ key: `note:${n.path}`, label: n.title || n.path.split('/').pop().replace('.md',''), isLeaf: true })) },
  ]})
  return result
})

const renderedContent = computed(() => {
  if (!selectedNote.value) return ''
  return marked.parse(selectedNote.value.content || '')
})

async function loadData() {
  if (!props.user) return
  loading.value = true
  try {
    const [topicData, noteData] = await Promise.all([
      api(`/api/${props.user}/topics`),
      api(`/api/${props.user}/notes${params({ sort: 'name' })}`),
    ])
    topics.value = [...topicData.active, ...topicData.archived.map(a => ({ ...a, archived: true }))]
    notes.value = noteData.notes
  } catch (e) {
    message.error('加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function onSelect(keys) {
  const key = keys[0]
  if (!key || !key.startsWith('note:')) return
  const path = key.slice(5)
  try {
    const data = await api(`/api/${props.user}/note${params({ path })}`)
    selectedNote.value = data
    selectedNoteTitle.value = data.title || path.split('/').pop().replace('.md', '')
    isAbstract.value = path.endsWith('abstract.md')
    editing.value = false
    editContent.value = data.content
  } catch (e) {
    message.error('读取失败: ' + e.message)
  }
}

function toggleEdit() {
  if (editing.value && selectedNote.value) {
    // Save
    saveNote()
  } else {
    editing.value = true
  }
}

async function saveNote() {
  try {
    await api(`/api/${props.user}/note`, {
      method: 'PUT',
      body: JSON.stringify({ path: selectedNote.value.path, content: editContent.value }),
    })
    selectedNote.value.content = editContent.value
    editing.value = false
    message.success('已保存')
  } catch (e) {
    message.error('保存失败: ' + e.message)
  }
}

function handleDelete() {
  dialog.warning({
    title: '删除笔记',
    content: `确认删除 ${selectedNote.value.path}？git 历史可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api(`/api/${props.user}/note${params({ path: selectedNote.value.path })}`, { method: 'DELETE' })
        message.success('已删除')
        selectedNote.value = null
        await loadData()
      } catch (e) {
        message.error('删除失败: ' + e.message)
      }
    },
  })
}

watch(() => props.user, () => { if (props.user) loadData() })
onMounted(() => { if (props.user) loadData() })
</script>

<style scoped>
.topics-page { height: 100%; }
.topic-tree { padding: 8px; height: 100%; overflow-y: auto; }
.topic-detail { padding: 0 16px; height: 100%; overflow-y: auto; }
.markdown-body { line-height: 1.7; }
.markdown-body :deep(h1) { font-size: 1.5em; margin: 0.5em 0; }
.markdown-body :deep(h2) { font-size: 1.2em; margin: 0.5em 0; }
.markdown-body :deep(code) { background: rgba(255,255,255,0.1); padding: 2px 6px; border-radius: 4px; }
.markdown-body :deep(pre) { background: rgba(0,0,0,0.2); padding: 12px; border-radius: 8px; overflow-x: auto; }
</style>
