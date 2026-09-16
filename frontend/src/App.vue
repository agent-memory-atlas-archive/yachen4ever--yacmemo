<template>
  <n-config-provider :theme="theme" :locale="zhCN" :date-locale="dateZhCN">
    <n-loading-bar-provider>
      <n-message-provider>
        <n-dialog-provider>
          <n-notification-provider>
            <div class="app-layout">
              <n-layout has-sider>
                <n-layout-sider
                  bordered
                  collapse-mode="width"
                  :collapsed-width="64"
                  :width="220"
                  show-trigger
                >
                  <div class="sidebar-logo">
                    <span v-if="!collapsed">yacmemo</span>
                    <span v-else>ym</span>
                  </div>
                  <n-menu
                    v-model:value="activePage"
                    :collapsed="collapsed"
                    :collapsed-width="64"
                    :collapsed-icon-size="22"
                    :options="menuOptions"
                    @update:value="onMenuSelect"
                  />
                </n-layout-sider>
                <n-layout>
                  <n-layout-header bordered class="app-header">
                    <n-space align="center" justify="space-between">
                      <n-text strong>{{ pageTitle }}</n-text>
                      <n-space align="center">
                        <n-tag v-if="currentUser" size="small" type="info" round>
                          {{ currentUser }}
                        </n-tag>
                        <n-select
                          v-if="users.length > 1"
                          v-model:value="currentUser"
                          :options="userOptions"
                          size="small"
                          style="width: 120px"
                          @update:value="onUserChange"
                        />
                      </n-space>
                    </n-space>
                  </n-layout-header>
                  <n-layout-content class="app-content">
                    <TopicsPage v-if="activePage === 'topics'" :user="currentUser" />
                    <SearchPage v-else-if="activePage === 'search'" :user="currentUser" />
                    <AuditPage v-else-if="activePage === 'audit'" :user="currentUser" />
                    <ProfilePage v-else-if="activePage === 'profile'" :user="currentUser" />
                    <SettingsPage v-else-if="activePage === 'settings'" :user="currentUser" />
                  </n-layout-content>
                </n-layout>
              </n-layout>
            </div>
          </n-notification-provider>
        </n-dialog-provider>
      </n-message-provider>
    </n-loading-bar-provider>
  </n-config-provider>
</template>

<script setup>
import { ref, computed, onMounted, h } from 'vue'
import {
  NConfigProvider, NLayout, NLayoutSider, NLayoutHeader, NLayoutContent,
  NMenu, NSpace, NText, NTag, NSelect, NLoadingBarProvider,
  NMessageProvider, NDialogProvider, NNotificationProvider,
  darkTheme, zhCN, dateZhCN,
  NIcon,
} from 'naive-ui'

import TopicsPage from './components/TopicsPage.vue'
import SearchPage from './components/SearchPage.vue'
import AuditPage from './components/AuditPage.vue'
import ProfilePage from './components/ProfilePage.vue'
import SettingsPage from './components/SettingsPage.vue'
import { api } from './composables/api.js'

const activePage = ref('topics')
const collapsed = ref(false)
const currentUser = ref('')
const users = ref([])

const menuOptions = [
  { label: '主题', key: 'topics' },
  { label: '搜索', key: 'search' },
  { label: '审计', key: 'audit' },
  { label: '画像', key: 'profile' },
  { label: '设置', key: 'settings' },
]

const pageTitle = computed(() => {
  const m = { topics: '主题浏览', search: '搜索', audit: '审计', profile: '画像与偏好', settings: '设置' }
  return m[activePage.value] || ''
})

const userOptions = computed(() => users.value.map(u => ({ label: u, value: u })))

function onMenuSelect(key) {
  activePage.value = key
}

function onUserChange(val) {
  currentUser.value = val
}

onMounted(async () => {
  try {
    const data = await api('/api/overview')
    users.value = data.users.map(u => u.id)
    if (users.value.length > 0) currentUser.value = users.value[0]
  } catch (e) {
    console.error('Failed to load overview:', e)
  }
})
</script>

<style>
body { margin: 0; }
.app-layout { height: 100vh; }
.sidebar-logo {
  height: 48px; display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700; border-bottom: 1px solid var(--n-border-color);
}
.app-header {
  height: 48px; padding: 0 20px; display: flex; align-items: center;
}
.app-content { padding: 16px; height: calc(100vh - 48px); overflow-y: auto; }
</style>
