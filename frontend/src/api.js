const BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')
const TOKEN_KEY = 'jobspy_api_token'

let onUnauthorized = null

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* private mode */
  }
}

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

function authHeaders(extra = {}) {
  const token = getToken()
  return token ? { ...extra, 'X-API-Token': token } : extra
}

async function readError(res) {
  let detail = `${res.status} ${res.statusText}`
  try {
    const data = await res.json()
    if (data && data.detail != null) {
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    }
  } catch {
    // non-JSON body — keep the status line
  }
  const err = new Error(detail)
  err.status = res.status
  return err
}

async function getJSON(path) {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() })
  if (res.status === 401) onUnauthorized?.()
  if (!res.ok) throw await readError(res)
  return res.json()
}

async function sendJSON(path, method, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body ?? {}),
  })
  if (res.status === 401) onUnauthorized?.()
  if (!res.ok) throw await readError(res)
  if (res.status === 204) return null
  return res.json()
}

async function postJSON(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body ?? {}),
  })
  if (res.status === 401) onUnauthorized?.()
  if (!res.ok) throw await readError(res)
  return res.json()
}

export const getHealth = () => getJSON('/health')
export const getStats = () => getJSON('/stats')
export const getJobs = ({ status, order = 'score', limit = 200, offset = 0, autoRejected } = {}) => {
  const q = new URLSearchParams()
  if (status) q.set('status', status)
  if (autoRejected !== undefined && autoRejected !== null) q.set('auto_rejected', String(autoRejected))
  q.set('order', order)
  q.set('limit', String(limit))
  if (offset) q.set('offset', String(offset))
  return getJSON(`/jobs?${q.toString()}`)
}
export const getJob = (key) => getJSON(`/jobs/${encodeURIComponent(key)}`)
export const setJobStatus = (key, status) =>
  postJSON(`/jobs/${encodeURIComponent(key)}/status`, { new_status: status })
export const getApplications = () => getJSON('/applications')
export const createApplication = (body) => postJSON('/applications', body)
export const updateApplication = (id, patch) =>
  sendJSON(`/applications/${encodeURIComponent(id)}`, 'PATCH', patch)
export const deleteApplication = (id) =>
  sendJSON(`/applications/${encodeURIComponent(id)}`, 'DELETE')
export const importApplications = (applications) =>
  postJSON('/applications/import', { applications })
export const getRuns = (limit = 50) => getJSON(`/runs?limit=${limit}`)
export const startRun = (forceRescore = false) =>
  postJSON(`/run?force_rescore=${forceRescore}`)
export const getRunLatest = () => getJSON('/run/latest')
export const getScheduler = () => getJSON('/scheduler')
export const saveSchedulerConfig = (config) => postJSON('/scheduler/config', config)
export const toggleScheduler = (enabled) => postJSON(`/scheduler/toggle?enabled=${enabled}`)
export const getSettings = () => getJSON('/settings')
export const saveSettings = (settings) => postJSON('/settings', settings)
export const testNotify = () => postJSON('/notify/test')
export const purgeOlderThan = (olderThanDays) =>
  postJSON('/maintenance/purge', { older_than_days: olderThanDays })
export const getResumeInfo = () => getJSON('/resume')
export const uploadResume = (file) => {
  const form = new FormData()
  form.append('resume', file, file.name)
  return fetch(`${BASE}/resume`, { method: 'POST', headers: authHeaders(), body: form }).then(async (res) => {
    if (res.status === 401) onUnauthorized?.()
    if (!res.ok) throw await readError(res)
    return res.json()
  })
}
export const exportUrl = () => {
  const token = getToken()
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${BASE}/export.xlsx${query}`
}
