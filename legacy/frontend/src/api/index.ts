/** API client for yacmemo backend.
 *
 * All endpoints are relative to /admin/api (the FastAPI sub-app mount path).
 */

const BASE = '/admin/api'

async function getJSON(path: string): Promise<any> {
  const resp = await fetch(`${BASE}${path}`)
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(body.detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

async function postForm(path: string, data: Record<string, string>): Promise<any> {
  const form = new FormData()
  for (const [k, v] of Object.entries(data)) form.append(k, v)
  const resp = await fetch(`${BASE}${path}`, { method: 'POST', body: form })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(body.detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

async function postJSON(path: string, data: any): Promise<any> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(body.detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

async function deleteReq(path: string): Promise<any> {
  const resp = await fetch(`${BASE}${path}`, { method: 'DELETE' })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(body.detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

// ---- Types ----

export interface UserStats {
  id: string
  display_name: string
  md_files?: number
  valid_nodes?: number
  total_nodes?: number
  events?: number
  processed?: number
  failed?: number
  pending_consistency?: number
  has_custom_key?: boolean
  error?: string
}

export interface EntityNode {
  name: string
  type: string
  summary: string | null
  source_path: string
  valid: number
  created_at: string
}

export interface EntityEvent {
  date: string | null
  type: string | null
  summary: string
  source_path: string
}

export interface FileTreeItem {
  name: string
  path: string
  type: 'dir' | 'file'
  children?: FileTreeItem[]
}

export interface ConsistencyPending {
  id: string
  old_source_path: string
  new_source_path: string
  reason: string
  confidence: number
  checked_at: string
}

export interface ConsistencyAutoLog {
  old_source_path: string
  new_source_path: string
  reason: string
  confidence: number
  checked_at: string
}

export interface InvalidatedNode {
  name: string
  type: string
  summary: string | null
  invalid_at: string | null
  invalid_reason: string | null
  source_path: string
}

export interface SearchResult {
  id: string
  text: string
  source_path: string
  _distance: number
  kind: string
}

export interface UserIndex {
  user_id: string
  sqlite_size?: string
  nodes?: number
  events?: number
  processed?: number
  failed?: number
  recent?: { path: string; status: string; processed_at: string; split_file_count: number }[]
  error?: string
}

export interface SystemStatus {
  llm_ok: boolean
  llm_detail: string
  llm_base_url: string
  llm_model: string
  emb_ok: boolean
  emb_detail: string
  emb_base_url: string
  emb_model: string
  user_indexes: UserIndex[]
  extract_cron: string
  consistency_cron: string
}

// ---- API functions ----

export const api = {
  // Users
  listUsers: (): Promise<{ users: UserStats[] }> => getJSON('/users'),
  createUser: (data: { id: string; display_name?: string; memory_root: string; llm_api_key?: string; embedding_api_key?: string }) =>
    postJSON('/users', data),
  deleteUser: (userId: string) => deleteReq(`/users/${userId}`),
  triggerScan: (userId: string) => postJSON(`/users/${userId}/trigger-scan`, {}),

  // Memory
  getMemory: (userId: string): Promise<{ file_tree: FileTreeItem[]; nodes: EntityNode[]; events: EntityEvent[] }> =>
    getJSON(`/memory/${userId}`),
  readFile: (userId: string, path: string): Promise<{ content: string; path: string }> =>
    getJSON(`/memory/${userId}/file?path=${encodeURIComponent(path)}`),
  getHistory: (userId: string, entityName: string): Promise<{ history: any[] }> =>
    getJSON(`/memory/${userId}/history/${encodeURIComponent(entityName)}`),

  // Search
  search: (userId: string, query: string, limit: number = 10): Promise<{ results: SearchResult[]; query: string; error?: string }> =>
    postForm(`/search/${userId}`, { query, limit: String(limit) }),
  grep: (userId: string, pattern: string): Promise<{ matches?: string; error?: string }> =>
    postForm(`/grep/${userId}`, { pattern }),

  // Consistency
  getConsistency: (userId: string): Promise<{ pending: ConsistencyPending[]; auto_logs: ConsistencyAutoLog[]; invalidated: InvalidatedNode[] }> =>
    getJSON(`/consistency/${userId}`),
  resolveConsistency: (userId: string, logId: string, action: string) =>
    postForm(`/consistency/${userId}/resolve/${logId}`, { action }),

  // Status
  getStatus: (): Promise<SystemStatus> => getJSON('/status'),

  // Health
  getHealth: (): Promise<{ status: string; users: string[] }> => getJSON('/status/health'),
}
