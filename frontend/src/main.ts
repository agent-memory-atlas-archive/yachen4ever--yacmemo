import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import naive from 'naive-ui'
import App from './App.vue'
import './style.css'

const routes = [
  { path: '/', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
  { path: '/users', name: 'users', component: () => import('./views/Users.vue') },
  { path: '/status', name: 'status', component: () => import('./views/Status.vue') },
  { path: '/memory/:userId', name: 'memory', component: () => import('./views/Memory.vue'), props: true },
  { path: '/search/:userId', name: 'search', component: () => import('./views/Search.vue'), props: true },
  { path: '/consistency/:userId', name: 'consistency', component: () => import('./views/Consistency.vue'), props: true },
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
]

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes,
})

const app = createApp(App)
app.use(router)
app.use(naive)
app.mount('#app')
