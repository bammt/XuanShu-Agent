const BASE = import.meta.env.VITE_API_BASE || ''

let authRedirecting = false

export function handleAuthFailure() {
  if (typeof window === 'undefined') return
  localStorage.removeItem('xuanshu_token')
  localStorage.removeItem('xuanshu_workspace')
  localStorage.removeItem('xuanshu_user')
  if (!authRedirecting && window.location.pathname !== '/login') {
    authRedirecting = true
    window.location.replace('/login?expired=1')
  }
}

function authExpiredError() {
  return new Error('登录已失效，请重新登录')
}

function authHeaders() {
  const token = localStorage.getItem('xuanshu_token')
  const workspace = localStorage.getItem('xuanshu_workspace')
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(workspace ? { 'X-Workspace-Id': workspace } : {}),
  }
}

export async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(options.headers || {}) },
    ...options,
  })
  if (response.status === 401) {
    handleAuthFailure()
    throw authExpiredError()
  }
  if (response.status === 204) return null
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => `${item.loc?.slice(1).join('.') || '字段'}：${item.msg}`).join('；')
      : payload.detail
    throw new Error(detail || payload.message || `HTTP ${response.status}`)
  }
  return payload
}

async function downloadAuthenticatedFile(file) {
  const response = await fetch(`${BASE}${file.url}`, { headers: authHeaders() })
  if (response.status === 401) {
    handleAuthFailure()
    throw authExpiredError()
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.detail || `文件下载失败（HTTP ${response.status}）`)
  }
  const blob = await response.blob()
  const objectUrl = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = file.name.split('/').pop() || 'download'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(objectUrl)
}

export async function authenticatedObjectUrl(file) {
  const response = await fetch(`${BASE}${file.url}`, { headers: authHeaders() })
  if (response.status === 401) {
    handleAuthFailure()
    throw authExpiredError()
  }
  if (!response.ok) throw new Error(`文件预览失败（HTTP ${response.status}）`)
  return window.URL.createObjectURL(await response.blob())
}

export const api = {
  workspaces: () => request('/api/workspaces'),
  createWorkspace: (name) => request('/api/workspaces', { method: 'POST', body: JSON.stringify({ name }) }),
  deleteWorkspace: (id) => request(`/api/workspaces/${id}`, { method: 'DELETE' }),
  workspaceMembers: (id) => request(`/api/workspaces/${id}/members`),
  workspaceInviteCandidates: (id) => request(`/api/workspaces/${id}/invite-candidates`),
  inviteMember: (id, username, can_edit) => request(`/api/workspaces/${id}/members`, { method: 'POST', body: JSON.stringify({ username, can_edit }) }),
  setMemberPermission: (id, userId, can_edit) => request(`/api/workspaces/${id}/members/${userId}`, { method: 'PUT', body: JSON.stringify({ can_edit }) }),
  invitations: () => request('/api/invitations'),
  decideInvitation: (id, decision) => request(`/api/invitations/${id}/${decision}`, { method: 'POST' }),
  users: () => request('/api/admin/users'),
  createUser: (username, password) => request('/api/admin/users', { method: 'POST', body: JSON.stringify({ username, password }) }),
  resetUserPassword: (id, password) => request(`/api/admin/users/${id}/reset-password`, { method: 'POST', body: JSON.stringify({ password }) }),
  deleteUser: (id) => request(`/api/admin/users/${id}`, { method: 'DELETE' }),
  overview: () => request(`/api/overview?workspace_id=${localStorage.getItem('xuanshu_workspace') || ''}`),
  studioChat: (body) => request('/api/studio/chat', { method: 'POST', body: JSON.stringify(body) }),
  studioJob: (id) => request(`/api/studio/jobs/${id}`),
  studioSessions: () => request('/api/studio/sessions'),
  createStudioSession: (kind = 'crew') => request('/api/studio/sessions', { method: 'POST', body: JSON.stringify({ kind }) }),
  studioSession: (id) => request(`/api/studio/sessions/${id}`),
  updateStudioSession: (id, body) => request(`/api/studio/sessions/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteStudioSession: (id) => request(`/api/studio/sessions/${id}`, { method: 'DELETE' }),
  studioEvents: async (id, onEvent) => {
    const response = await fetch(`${BASE}/api/studio/jobs/${id}/events`, { headers: authHeaders() })
    if (response.status === 401) {
      handleAuthFailure()
      throw authExpiredError()
    }
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() || ''
      for (const frame of frames) {
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
        if (data) onEvent(JSON.parse(data))
      }
      if (done) break
    }
  },
  uploadStudioAttachments: (files, onProgress) => new Promise((resolve, reject) => {
    const form = new FormData()
    files.forEach(file => form.append('files', file, file.name))
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}/api/studio/attachments`)
    Object.entries(authHeaders()).forEach(([name, value]) => xhr.setRequestHeader(name, value))
    xhr.responseType = 'json'
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onerror = () => reject(new Error('文件上传失败，请检查网络后重试'))
    xhr.onload = () => {
      const payload = xhr.response || {}
      if (xhr.status === 401) {
        handleAuthFailure()
        reject(authExpiredError())
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100)
        resolve(payload)
        return
      }
      reject(new Error(payload.detail || payload.message || `HTTP ${xhr.status}`))
    }
    xhr.send(form)
  }),
  deleteStudioAttachment: (id) => request(`/api/studio/attachments/${id}`, { method: 'DELETE' }),
  knowledge: () => request('/api/knowledge'),
  knowledgeItem: (id) => request(`/api/knowledge/${id}`),
  saveKnowledge: (body) => request('/api/knowledge', { method: 'POST', body: JSON.stringify(body) }),
  deleteKnowledge: (id) => request(`/api/knowledge/${id}`, { method: 'DELETE' }),
  deleteKnowledgeFile: (id, fileId) => request(`/api/knowledge/${id}/files/${fileId}`, { method: 'DELETE' }),
  knowledgeFileChunks: (id, fileId) => request(`/api/knowledge/${id}/files/${fileId}/chunks`),
  uploadKnowledgeFiles: (id, files, onProgress) => new Promise((resolve, reject) => {
    const form = new FormData(); files.forEach(file => form.append('files', file, file.name))
    const xhr = new XMLHttpRequest(); xhr.open('POST', `${BASE}/api/knowledge/${id}/files`)
    Object.entries(authHeaders()).forEach(([name, value]) => xhr.setRequestHeader(name, value))
    xhr.responseType = 'json'
    xhr.upload.onprogress = event => event.lengthComputable && onProgress?.(Math.round(event.loaded / event.total * 100))
    xhr.onerror = () => reject(new Error('知识文件上传失败，请检查网络后重试'))
    xhr.onload = () => {
      if (xhr.status === 401) {
        handleAuthFailure()
        reject(authExpiredError())
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response || [])
      else reject(new Error(xhr.response?.detail || `HTTP ${xhr.status}`))
    }
    xhr.send(form)
  }),
  saveWorkflow: (body, manualChanges = []) => request('/api/workflows', {
    method: 'POST',
    body: JSON.stringify({ ...body, _manual_changes: manualChanges }),
  }),
  workflow: (id) => request(`/api/workflows/${id}`),
  publishWorkflow: (id) => request(`/api/workflows/${id}/publish`, { method: 'POST' }),
  runtimeWorkflow: (id) => request(`/api/workflows/${id}/runtime`),
  deleteWorkflow: (id) => request(`/api/workflows/${id}`, { method: 'DELETE' }),
  applicationApiKeys: (id) => request(`/api/apps/${id}/api-keys`),
  createApplicationApiKey: (id, name) => request(`/api/apps/${id}/api-keys`, {
    method: 'POST', body: JSON.stringify({ name }),
  }),
  deleteApplicationApiKey: (id, keyId) => request(`/api/apps/${id}/api-keys/${keyId}`, { method: 'DELETE' }),
  runWorkflow: (id, inputs, attachments = {}, conversation = {}) => request(`/api/workflows/${id}/run`, {
    method: 'POST', body: JSON.stringify({ inputs, attachments, ...conversation }),
  }),
  conversations: (id) => request(`/api/workflows/${id}/conversations`),
  createConversation: (id, options = {}) => request(`/api/workflows/${id}/conversations`, { method: 'POST', body: JSON.stringify(options) }),
  conversation: (id, conversationId) => request(`/api/workflows/${id}/conversations/${conversationId}`),
  deleteConversation: (id, conversationId) => request(`/api/workflows/${id}/conversations/${conversationId}`, { method: 'DELETE' }),
  runs: () => request('/api/runs'),
  run: (id) => request(`/api/runs/${id}`),
  deleteRun: (id) => request(`/api/runs/${id}`, { method: 'DELETE' }),
  traces: () => request('/api/traces'),
  trace: (conversationId) => request(`/api/traces/${conversationId}`),
  deleteTrace: (conversationId) => request(`/api/traces/${conversationId}`, { method: 'DELETE' }),
  downloadRunFile: (file) => downloadAuthenticatedFile(file),
  runEvents: async (id, onEvent, signal, afterEvent = 0) => {
    const response = await fetch(`${BASE}/api/runs/${id}/events?after_event=${afterEvent}`, { signal, headers: authHeaders() })
    if (response.status === 401) {
      handleAuthFailure()
      throw authExpiredError()
    }
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() || ''
      for (const frame of frames) {
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
        if (data) onEvent(JSON.parse(data))
      }
      if (done) break
    }
  },
  submitRunFeedback: (id, outcome, feedback) => request(`/api/runs/${id}/feedback`, {
    method: 'POST', body: JSON.stringify({ outcome, feedback }),
  }),
  saveSkill: (body) => request('/api/skills', { method: 'POST', body: JSON.stringify(body) }),
  importSkills: (files, onProgress) => new Promise((resolve, reject) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file, file.webkitRelativePath || file.name))
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}/api/skills/import`)
    Object.entries(authHeaders()).forEach(([name, value]) => xhr.setRequestHeader(name, value))
    xhr.responseType = 'json'
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onerror = () => reject(new Error('Skill 上传失败，请检查网络后重试'))
    xhr.onload = () => {
      const payload = xhr.response || {}
      if (xhr.status === 401) {
        handleAuthFailure()
        reject(authExpiredError())
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) { onProgress?.(100); resolve(payload); return }
      reject(new Error(payload.detail || payload.message || `HTTP ${xhr.status}`))
    }
    xhr.send(form)
  }),
  deleteSkill: (id) => request(`/api/skills/${id}`, { method: 'DELETE' }),
  savePlugin: (body) => request('/api/plugins', { method: 'POST', body: JSON.stringify(body) }),
  deletePlugin: (id) => request(`/api/plugins/${id}`, { method: 'DELETE' }),
  models: () => request('/api/models'),
  saveModel: (body) => request('/api/models', { method: 'POST', body: JSON.stringify(body) }),
  setDefaultModel: (model_id, model_type = 'chat') => request('/api/models/default', { method: 'PUT', body: JSON.stringify({ model_id: String(model_id), model_type }) }),
  deleteModel: (id) => request(`/api/models/${id}`, { method: 'DELETE' }),
  testModel: (id) => request(`/api/models/${id}/test`, { method: 'POST' }),
}
