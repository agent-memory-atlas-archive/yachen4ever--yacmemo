const BASE = ''

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
