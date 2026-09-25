<template>
  <n-config-provider :theme="theme" :locale="naiveLocale" :date-locale="naiveDateLocale">
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
                  <div class="sidebar-flex">
                    <div>
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
                    </div>
                    <div v-if="!collapsed" class="sidebar-version">
                      v{{ build.version }} · {{ build.commit }}
                    </div>
                  </div>
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
                        <n-select
                          :value="locale"
                          :options="localeOptions"
                          size="small"
                          style="width: 96px"
                          @update:value="setLocale"
                        />
                      </n-space>
                    </n-space>
                  </n-layout-header>
                  <n-layout-content class="app-content">
                    <DashboardPage v-if="activePage === 'dashboard'" :build="build"
                      @navigate="onDashboardNavigate" />
                    <TopicsPage v-else-if="activePage === 'topics'" :user="currentUser" />
                    <SearchPage v-else-if="activePage === 'search'" :user="currentUser" />
                    <AuditPage v-else-if="activePage === 'audit'" :user="currentUser" />
                    <ProfilePage v-else-if="activePage === 'profile'" :user="currentUser" />
                    <IdentitiesPage v-else-if="activePage === 'identities'" :user="currentUser" />
                    <SettingsPage v-else-if="activePage === 'settings'" :user="currentUser" />
                  </n-layout-content>
                </n-layout>
              </n-layout>
            </div>
          </n-notification-provider>
        </n-dialog-provider>
      </n-message-provider>

      <n-modal :show="needLogin" preset="dialog" :title="t('yacmemo 登录')" :show-icon="false"
               :mask-closable="false" :closable="false" style="width: 320px">
        <n-input
          v-model:value="loginPassword"
          type="password" show-password-on="click"
          :placeholder="t('访问密码')" @keyup.enter="doLogin"
        />
        <n-text v-if="loginError" type="error" style="font-size: 12px">{{ loginError }}</n-text>
        <template #action>
          <n-button type="primary" block :loading="loginBusy" @click="doLogin">{{ t('登录') }}</n-button>
        </template>
      </n-modal>
    </n-loading-bar-provider>
  </n-config-provider>
</template>

<script setup>
import { ref, computed, onMounted, h } from 'vue'
import {
  NConfigProvider, NLayout, NLayoutSider, NLayoutHeader, NLayoutContent,
  NMenu, NSpace, NText, NTag, NSelect, NLoadingBarProvider,
  NMessageProvider, NDialogProvider, NNotificationProvider,
  NModal, NInput, NButton,
  darkTheme, zhCN, dateZhCN, enUS, dateEnUS,
  NIcon,
} from 'naive-ui'

import TopicsPage from './components/TopicsPage.vue'
import SearchPage from './components/SearchPage.vue'
import AuditPage from './components/AuditPage.vue'
import ProfilePage from './components/ProfilePage.vue'
import IdentitiesPage from './components/IdentitiesPage.vue'
import SettingsPage from './components/SettingsPage.vue'
import DashboardPage from './components/DashboardPage.vue'
import { api } from './composables/api.js'
import { t, locale, setLocale } from './composables/i18n.js'

const build = __BUILD__

const activePage = ref('dashboard')
const collapsed = ref(false)
const currentUser = ref('')
const users = ref([])
const needLogin = ref(false)
const loginPassword = ref('')
const loginBusy = ref(false)
const loginError = ref('')

const menuOptions = computed(() => [
  { label: t('仪表盘'), key: 'dashboard' },
  { label: t('主题'), key: 'topics' },
  { label: t('搜索'), key: 'search' },
  { label: t('审计'), key: 'audit' },
  { label: t('画像'), key: 'profile' },
  { label: t('身份'), key: 'identities' },
  { label: t('设置'), key: 'settings' },
])

const pageTitle = computed(() => {
  const m = { dashboard: t('仪表盘'), topics: t('主题浏览'), search: t('搜索'),
              audit: t('审计'), profile: t('画像与偏好'), identities: t('身份管理'),
              settings: t('设置') }
  return m[activePage.value] || ''
})

const naiveLocale = computed(() => (locale.value === 'en' ? enUS : zhCN))
const naiveDateLocale = computed(() => (locale.value === 'en' ? dateEnUS : dateZhCN))
const localeOptions = [
  { label: '中文', value: 'zh' },
  { label: 'English', value: 'en' },
]

function onDashboardNavigate({ user, page }) {
  if (user) currentUser.value = user
  activePage.value = page
}

async function doLogin() {
  loginBusy.value = true
  loginError.value = ''
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: loginPassword.value }) })
    needLogin.value = false
    loginPassword.value = ''
    window.location.reload()
  } catch (e) {
    loginError.value = e.message || '登录失败'
  } finally {
    loginBusy.value = false
  }
}

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
    if (String(e.message).includes('未登录')) needLogin.value = true
    else console.error('Failed to load overview:', e)
  }
})
</script>

<style>
body { margin: 0; }
.app-layout { height: 100vh; }
.sidebar-flex { height: 100%; display: flex; flex-direction: column; }
.sidebar-flex > div:first-child { flex: 1; }
.sidebar-logo {
  height: 48px; display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700; border-bottom: 1px solid var(--n-border-color);
}
.sidebar-version {
  padding: 10px 0; text-align: center; font-size: 11px; opacity: 0.6;
  border-top: 1px solid var(--n-border-color);
}
.app-header {
  height: 48px; padding: 0 20px; display: flex; align-items: center;
}
.app-content { padding: 16px; height: calc(100vh - 48px); overflow-y: auto; }
</style>
