// 服务端在 base_path 模式下向 index.html 注入 window.__BASE_PATH__（如 "/yacmemo"）；
// 未注入（默认无前缀）时为空串，行为不变。导出供组件内跳转等非 fetch 场景复用——
// 组件里不许再硬编码 /ui 这类绝对路径（编译进 chunk，服务端改写够不到）。
export const BASE = (typeof window !== 'undefined' && window.__BASE_PATH__) || ''

export async function api(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const data = await res.json()
  if (!data.ok && data.error) throw new Error(data.error)
  return data
}

export function params(obj) {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(obj)) if (v != null && v !== '') s.set(k, v)
  return '?' + s.toString()
}
