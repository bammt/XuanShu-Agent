<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Bot, Check, ChevronDown, Download, FileText, Image, LoaderCircle, Paperclip, Send, Trash2, X } from 'lucide-vue-next'
import { stripLocalArtifactReferences } from '../services/messageFormatting'
import { confirmDialog } from '../services/dialog'
import RichMessage from '../components/RichMessage.vue'
import { formatBeijingDateTime } from '../services/dateFormatting'

const token = useRoute().params.token
const app = ref(null), loading = ref(true), error = ref(''), message = ref(''), busy = ref(false), uploading = ref(false), uploadProgress = ref(0), thread = ref(null)
const values = reactive({}), files = reactive({})
const messages = ref([]), approval = ref(null), approvalText = ref('')
const conversations = ref([]), conversationId = ref('')
const textInput = computed(() => app.value?.inputs.find(item => ['text', 'long_text'].includes(item.input_type)) || null)
const extraInputs = computed(() => app.value?.inputs.filter(item => item.name !== textInput.value?.name && !['file', 'image'].includes(item.input_type)) || [])
const fileInputs = computed(() => app.value?.inputs.filter(item => ['file', 'image'].includes(item.input_type)) || [])
const selectedFiles = computed(() => fileInputs.value.flatMap(input => (files[input.name] || []).map(file => ({ input, file }))))
const scroll = () => nextTick(() => { if (thread.value) thread.value.scrollTop = thread.value.scrollHeight })

onMounted(async () => {
  try {
    const response = await fetch(`/api/public/${token}`), data = await response.json()
    if (!response.ok) throw new Error(data.detail || '应用不存在')
    app.value = data
    for (const input of data.inputs || []) { values[input.name] = input.input_type === 'boolean' ? false : ''; files[input.name] = [] }
    await loadConversation()
  } catch (cause) { error.value = cause.message }
  finally { loading.value = false }
})
async function loadConversation() {
  let rows = []
  try {
    const response = await fetch(`/api/public/${token}/conversations`)
    if (response.ok) rows = await response.json()
  } catch (_) { /* first visit has no anonymous cookie yet */ }
  conversations.value = rows
  if (!rows.length) {
    conversationId.value = ''
    messages.value = [{ role: 'assistant', text: app.value.welcome }]
    return
  }
  const requested = String(new URLSearchParams(window.location.search).get('conversation') || '')
  const selected = rows.find(item => item.id === requested) || rows[0]
  await openConversation(selected.id, false)
}
async function refreshConversations() {
  const response = await fetch(`/api/public/${token}/conversations`)
  if (response.ok) conversations.value = await response.json()
}
async function openConversation(id, updateUrl = true) {
  if (!id || busy.value) return
  const response = await fetch(`/api/public/${token}/conversations/${id}`)
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '无法打开对话')
  conversationId.value = data.id
  approval.value = null
  messages.value = [{ role: 'assistant', text: app.value.welcome }]
  for (const item of data.runs || []) {
    if (item.user_message) messages.value.push({ role: 'user', text: item.user_message, attachments: Object.values(item.attachments || {}).flat() })
    if (item.status === 'completed') messages.value.push({ role: 'assistant', text: stripLocalArtifactReferences(item.output), files: item.files || [], done: true })
    else if (item.status === 'failed') messages.value.push({ role: 'assistant', text: stripLocalArtifactReferences(item.output), error: item.error || item.output, done: true })
    else if (item.status === 'waiting_for_feedback') {
      const answer = reactive({ role: 'assistant', text: '流程已暂停，等待你的审核。', done: true })
      messages.value.push(answer)
      approval.value = { runId: item.id, ...(item.pending_feedback || {}), eventCursor: item.events?.length || 0 }
    } else if (item.status === 'waiting_input') {
      messages.value.push({ role: 'assistant', text: stripLocalArtifactReferences(item.output) || item.waiting_input?.question || '请补充必要信息。', done: true })
    }
  }
  if (updateUrl) window.history.replaceState({}, '', `${window.location.pathname}?conversation=${encodeURIComponent(id)}`)
  scroll()
}
async function newConversation() {
  if (busy.value) return
  try {
    const response = await fetch(`/api/public/${token}/conversations`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '无法创建对话')
    await refreshConversations(); await openConversation(data.id)
  } catch (cause) { error.value = cause.message }
}
async function deleteConversation(item) {
  if (busy.value) return
  const confirmed = await confirmDialog({
    title: '删除对话',
    message: `确认删除对话“${item.title || '新对话'}”？此操作会删除该会话的历史运行。`,
    confirmLabel: '删除', danger: true,
  })
  if (!confirmed) return
  try {
    const response = await fetch(`/api/public/${token}/conversations/${item.id}`, { method: 'DELETE' })
    if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '删除失败') }
    await refreshConversations()
    if (item.id === conversationId.value) {
      if (!conversations.value.length) return newConversation()
      await openConversation(conversations.value[0].id)
    }
  } catch (cause) { error.value = cause.message }
}
function uploadPublicFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData(); form.append('file', file, file.name)
    const xhr = new XMLHttpRequest(); xhr.open('POST', `/api/public/${token}/files`); xhr.responseType = 'json'; xhr.withCredentials = true
    xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress?.(Math.round(event.loaded / event.total * 100)) }
    xhr.onload = () => xhr.status < 300 ? resolve(xhr.response) : reject(new Error(xhr.response?.detail || '文件上传失败'))
    xhr.onerror = () => reject(new Error('网络连接中断，文件上传失败')); xhr.send(form)
  })
}
async function chooseFiles(input, event) {
  const selected = Array.from(event.target.files || []); event.target.value = ''
  if (!selected.length || uploading.value) return
  uploading.value = true; uploadProgress.value = 0; error.value = ''
  try {
    const uploaded = []
    for (let index = 0; index < selected.length; index += 1) {
      const item = await uploadPublicFile(selected[index], value => {
        uploadProgress.value = Math.round(((index + value / 100) / selected.length) * 100)
      })
      uploaded.push(item)
    }
    files[input.name] = input.multiple ? [...files[input.name], ...uploaded] : uploaded.slice(0, 1)
  } catch (cause) { error.value = cause.message }
  finally { uploading.value = false; uploadProgress.value = 0 }
}
function removeFile(inputName, index) { files[inputName].splice(index, 1) }
function parsedInputs() {
  const result = {}
  for (const input of app.value.inputs || []) {
    if (['file', 'image'].includes(input.input_type)) continue
    const raw = input.name === textInput.value?.name ? message.value.trim() : values[input.name]
    if (raw === '' || raw === null || raw === undefined) continue
    if (input.input_type === 'number') result[input.name] = Number(raw)
    else if (input.input_type === 'json') {
      try { result[input.name] = JSON.parse(raw) } catch (_) { throw new Error(`${input.label} 不是有效的 JSON`) }
    } else result[input.name] = raw
  }
  if (app.value.interaction_mode !== 'multi_turn') {
    const missing = (app.value.inputs || []).filter(input => input.required).filter(input => ['file', 'image'].includes(input.input_type) ? !files[input.name]?.length : result[input.name] === undefined)
    if (missing.length) throw new Error(`请先填写：${missing.map(item => item.label).join('、')}`)
  }
  return result
}
function eventData(block) { return block.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n') }
function mergeNodeResult(answer, frame) {
  const id = frame.node_id || `${frame.node_name || 'node'}:${frame.agent_role || 'agent'}`
  const existing = answer.steps.find(item => item.id === id)
  const result = { id, name: frame.node_name || '执行节点', role: frame.agent_role || 'Agent', output: stripLocalArtifactReferences(frame.output), open: existing?.open || false }
  if (existing) Object.assign(existing, result)
  else answer.steps.push(result)
}
function outcomeLabel(outcome) {
  return ({ approved: '通过并继续', revise: '要求修改', rejected: '拒绝', needs_revision: '要求修改' })[outcome] || outcome
}
async function streamRun(runId, answer) {
  const response = await fetch(`/api/public/${token}/runs/${runId}/events?after_event=${answer.eventCursor || 0}`)
  if (!response.ok) throw new Error((await response.json()).detail || '无法读取执行状态')
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = ''
  while (true) {
    const { done, value } = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || ''
    for (const block of blocks) {
      const raw = eventData(block); if (!raw) continue
      const frame = JSON.parse(raw)
      if (Number.isFinite(frame.event_cursor)) answer.eventCursor = Math.max(answer.eventCursor || 0, frame.event_cursor)
      if (frame.type === 'node.completed') mergeNodeResult(answer, frame)
      if (frame.type === 'approval.required') { approval.value = { runId, message: frame.message || '请审核当前结果', output: frame.output || '', outcomes: frame.outcomes || ['approved','revise'], defaultOutcome: frame.default_outcome || '', eventCursor: answer.eventCursor || 0 }; answer.text = '流程已暂停，等待你的审核。' }
      if (frame.type === 'run.completed') { answer.text = stripLocalArtifactReferences(frame.output) || '执行完成。'; answer.done = true }
      if (frame.type === 'run.waiting_input') {
        answer.text = stripLocalArtifactReferences(frame.question || frame.output) || '请补充必要信息。'
        answer.waitingInput = frame.waiting_input || { question: answer.text }
        answer.done = true
      }
      if (frame.type === 'run.failed') { answer.error = frame.error || '执行失败'; answer.done = true }
      scroll()
    }
    if (done) break
  }
  if (answer.done) answer.files = await fetch(`/api/public/${token}/runs/${runId}/files`).then(result => result.json()).catch(() => [])
}
async function send() {
  if (busy.value || uploading.value) return
  let inputs
  try { inputs = parsedInputs() } catch (cause) { error.value = cause.message; return }
  const text = message.value.trim() || (selectedFiles.value.length ? '请处理上传的文件。' : '运行应用。')
  const submittedMessage = message.value.trim()
  const answer = reactive({ role: 'assistant', text: '', steps: [], files: [], done: false, error: '' })
  messages.value.push({ role: 'user', text, attachments: selectedFiles.value.map(item => item.file.name) }, answer)
  busy.value = true; error.value = ''; scroll()
  try {
    const form = new FormData(); form.append('message', submittedMessage); form.append('inputs_json', JSON.stringify(inputs))
    form.append('conversation_id', conversationId.value)
    for (const item of selectedFiles.value) { form.append('upload_ids', item.file.id); form.append('upload_variables', item.input.name) }
    const xhr = new XMLHttpRequest()
    const result = await new Promise((resolve, reject) => {
      xhr.open('POST', `/api/public/${token}/run`); xhr.responseType = 'json'
      xhr.upload.onprogress = event => { if (event.lengthComputable) uploadProgress.value = Math.round(event.loaded / event.total * 100) }
      xhr.onload = () => xhr.status < 300 ? resolve(xhr.response) : reject(new Error(xhr.response?.detail || '运行提交失败'))
      xhr.onerror = () => reject(new Error('网络连接中断，请重试')); xhr.send(form)
    })
    message.value = ''; Object.keys(files).forEach(key => { files[key] = [] })
    if (result.status === 'conversation_created') {
      answer.text = result.output || '已新建对话。'; answer.done = true
    } else {
      if (result.conversation_id) conversationId.value = result.conversation_id
      await streamRun(result.id, answer)
    }
    await refreshConversations()
  } catch (cause) { answer.error = cause.message; answer.done = true }
  finally { busy.value = false; uploadProgress.value = 0; scroll() }
}
async function approve(outcome) {
  if (!approval.value) return
  busy.value = true
  try {
    const response = await fetch(`/api/public/${token}/runs/${approval.value.runId}/approval`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ outcome, feedback: approvalText.value }) })
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '提交失败')
    const answer = reactive({ role: 'assistant', text: '', steps: [], files: [], done: false, error: '', eventCursor: approval.value.eventCursor || 0 }); messages.value.push(answer)
    const id = approval.value.runId; approval.value = null; approvalText.value = ''; await streamRun(id, answer)
  } catch (cause) { error.value = cause.message } finally { busy.value = false }
}
</script>

<template>
  <div v-if="loading" class="run-app-loading"><LoaderCircle class="spin" :size="22" />正在打开应用...</div>
  <div v-else-if="error&&!app" class="run-app-loading">{{ error }}</div>
  <div v-else class="chat-app-page public-chat-page has-history">
    <header class="chat-app-header"><span class="chat-app-icon"><Bot :size="18"/></span><div class="chat-app-title"><strong>{{ app.name }}</strong><small>{{ app.kind.toUpperCase() }} · 玄枢公开应用</small></div></header>
    <main class="chat-app-main">
      <aside class="chat-history">
        <header><strong>历史对话</strong><button class="icon-button" title="新建对话" aria-label="新建对话" @click="newConversation"><Send :size="13" /></button></header>
        <div v-if="conversations.length" class="chat-history-list">
          <article v-for="item in conversations" :key="item.id" class="chat-history-item" :class="{ active: item.id === conversationId }" @click="openConversation(item.id)">
            <div><strong>{{ item.title || '新对话' }}</strong><small>{{ formatBeijingDateTime(item.updated_at) }}</small></div>
            <button class="chat-history-delete" title="删除对话" aria-label="删除对话" @click.stop="deleteConversation(item)"><Trash2 :size="13" /></button>
          </article>
        </div>
        <p v-else class="chat-history-empty">暂无历史对话</p>
      </aside>
      <section class="chat-surface"><div ref="thread" class="chat-thread"><div class="chat-thread-inner">
      <article v-for="(item,index) in messages" :key="index" class="chat-message" :class="item.role"><span v-if="item.role==='assistant'" class="chat-avatar"><Bot :size="16"/></span><div class="chat-message-body"><RichMessage v-if="item.text" class="message-content" :text="item.text" :files="item.files || []" :authenticated="false"/><div v-if="item.attachments?.length" class="message-files"><span v-for="name in item.attachments" :key="name"><FileText :size="13"/>{{ name }}</span></div>
        <div v-if="item.steps?.length" class="execution-progress"><div class="execution-progress-head">执行结果 <span>{{ item.steps.length }} 个节点</span></div><button v-for="step in item.steps" :key="step.id" class="execution-step" @click="step.open=!step.open"><span><Check :size="13"/>{{ step.role }} · {{ step.name }}</span><ChevronDown :size="13"/><pre v-if="step.open">{{ step.output }}</pre></button></div>
        <div v-if="item.error" class="form-error">{{ item.error }}</div><div v-if="item.files?.length" class="delivery-files"><a v-for="file in item.files" :key="file.name" :href="file.url"><Download :size="14"/>{{ file.name }}</a></div><span v-if="!item.done&&!item.text&&item.role==='assistant'" class="typing-dots">•••</span></div></article>
      <section v-if="approval" class="feedback-gate"><h3>需要人工审核</h3><p>{{ approval.message }}</p><pre>{{ approval.output }}</pre><textarea v-model="approvalText" placeholder="填写审核意见（可选）"></textarea><div><button v-for="outcome in approval.outcomes" :key="outcome" class="button" :class="{primary:outcome==='approved'}" @click="approve(outcome)">{{ outcomeLabel(outcome) }}</button></div></section>
    </div></div><footer class="chat-composer public-composer">
      <div v-if="extraInputs.length" class="public-run-fields"><label v-for="input in extraInputs" :key="input.name"><span>{{ input.label }}<em v-if="input.required">必填</em></span><textarea v-if="input.input_type==='long_text'||input.input_type==='json'" v-model="values[input.name]" :placeholder="input.description"></textarea><span v-else-if="input.input_type==='boolean'" class="chat-variable-switch"><input v-model="values[input.name]" type="checkbox"/>启用</span><input v-else v-model="values[input.name]" :type="input.input_type==='number'?'number':'text'" :placeholder="input.description"/></label></div>
      <div v-if="fileInputs.length" class="public-file-inputs"><div v-for="input in fileInputs" :key="input.name"><label class="public-file-button"><Image v-if="input.input_type==='image'" :size="14"/><Paperclip v-else :size="14"/>{{ input.label }}<em v-if="input.required">必填</em><input type="file" hidden :multiple="input.multiple" :accept="input.input_type==='image'?'image/*':'*/*'" @change="chooseFiles(input,$event)"/></label><span v-for="(file,index) in files[input.name]" :key="`${file.name}-${index}`"><FileText :size="12"/>{{ file.name }}<button @click="removeFile(input.name,index)"><X :size="11"/></button></span></div></div>
      <div v-if="uploadProgress" class="upload-progress"><span :style="{width:`${uploadProgress}%`}"></span></div><textarea v-model="message" :disabled="busy || uploading" :placeholder="textInput?.label||'输入消息'" @keydown.enter.exact.prevent="send"></textarea><div v-if="error" class="public-composer-error">{{ error }}</div><div class="composer-toolbar"><span></span><button class="send-button" :disabled="busy || uploading" @click="send"><LoaderCircle v-if="busy || uploading" class="spin" :size="16"/><Send v-else :size="16"/></button></div></footer></section></main>
  </div>
</template>
