// 轻量 i18n：中文原文即 key，英文走映射字典，缺译自动回退中文。
// locale 持久化到 localStorage；t() 读取响应式 locale，切换即全局重渲染。
import { ref } from 'vue'
import enMap from './i18n-en.js'

const LOCALE_KEY = 'yacmemo-locale'

export const locale = ref(localStorage.getItem(LOCALE_KEY) || 'zh')

export function setLocale(l) {
  locale.value = l
  localStorage.setItem(LOCALE_KEY, l)
}

const en = enMap

export function registerEn(map) {
  Object.assign(en, map)
}

export function t(key, params) {
  let s = locale.value === 'en' ? (en[key] || key) : key
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replaceAll(`{${k}}`, String(v))
    }
  }
  return s
}
