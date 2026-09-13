<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NLayout, NLayoutSider, NLayoutContent, NMenu, NSelect, NSpace,
  NMessageProvider, NDialogProvider, darkTheme,
} from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { h } from 'vue'
import { RouterLink } from 'vue-router'
import { api, type UserStats } from './api'

const route = useRoute()
const router = useRouter()
const users = ref<UserStats[]>([])
const collapsed = ref(false)

const currentUserId = computed(() => {
  const uid = route.params.userId as string | undefined
  if (uid) return uid
  return users.value.length > 0 ? users.value[0].id : ''
})

const menuOptions = computed<MenuOption[]>(() => {
  const items: MenuOption[] = [
    {
      label: () => h(RouterLink, { to: '/' }, { default: () => '仪表盘' }),
      key: 'dashboard',
    },
    {
      label: () => h(RouterLink, { to: '/users' }, { default: () => '用户管理' }),
      key: 'users',
    },
    {
      label: () => h(RouterLink, { to: '/status' }, { default: () => '系统状态' }),
      key: 'status',
    },
  ]
  if (users.value.length > 0) {
    items.push({ type: 'divider', key: 'd1' })
    items.push({
      label: '用户视图',
      key: 'user-section',
      type: 'group',
      children: [
        {
          label: () => h(RouterLink, { to: `/memory/${currentUserId.value}` }, { default: () => '记忆浏览' }),
          key: 'memory',
        },
        {
          label: () => h(RouterLink, { to: `/search/${currentUserId.value}` }, { default: () => '搜索' }),
          key: 'search',
        },
        {
          label: () => h(RouterLink, { to: `/consistency/${currentUserId.value}` }, { default: () => '一致性校验' }),
          key: 'consistency',
        },
      ],
    })
  }
  return items
})

const activeKey = computed(() => {
  const name = route.name as string
  if (['dashboard', 'users', 'status'].includes(name)) return name
  return name || 'dashboard'
})

const userOptions = computed(() =>
  users.value.map(u => ({ label: u.display_name || u.id, value: u.id }))
)

function onUserSwitch(val: string) {
  const name = route.name as string
  if (['memory', 'search', 'consistency'].includes(name)) {
    router.push({ name, params: { userId: val } })
  } else {
    router.push({ name: 'memory', params: { userId: val } })
  }
}

async function loadUsers() {
  try {
    const resp = await api.listUsers()
    users.value = resp.users
  } catch {
    // ignore on first load
  }
}

watch(() => route.params.userId, () => {
  if (!users.value.length) loadUsers()
})

onMounted(loadUsers)
</script>

<template>
  <n-config-provider :theme="darkTheme">
    <n-message-provider>
      <n-dialog-provider>
        <n-layout has-sider style="height: 100vh">
          <n-layout-sider
            bordered
            collapse-mode="width"
            :collapsed-width="64"
            :width="220"
            :collapsed="collapsed"
            show-trigger
            @collapse="collapsed = true"
            @expand="collapsed = false"
          >
            <div style="padding: 16px 20px; font-size: 18px; font-weight: 700; color: #6c8eff">
              yacmemo
            </div>
            <n-menu
              :options="menuOptions"
              :value="activeKey"
            />
            <div v-if="userOptions.length > 0" style="padding: 0 20px; margin-top: 12px;">
              <n-select
                :options="userOptions"
                :value="currentUserId"
                @update:value="onUserSwitch"
                size="small"
              />
            </div>
          </n-layout-sider>
          <n-layout-content style="padding: 24px;">
            <router-view />
          </n-layout-content>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
