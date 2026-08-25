<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AppWindow, BookOpen, Braces, Code2, FolderUp, Globe2, Network, Plus, Search, SquarePen, Trash2, X } from 'lucide-vue-next'
import { api } from '../services/api'
import { usePlatformStore } from '../stores/platform'
import EmptyState from '../components/EmptyState.vue'
import { confirmDialog } from '../services/dialog'

const store = usePlatformStore()
const route = useRoute()
const router = useRouter()
const tab = ref('skills')
const search = ref('')
const modal = ref(false)
const importModal = ref(false)
const importFiles = ref([])
const importing = ref(false)
const importProgress = ref(0)
const folderInput = ref(null)
const requestText = ref('{}')
const actionType = ref('http')
const mcpTransport = ref('mcp_http')
const pythonMode = ref('source')
const pythonTemplate = `def run(query: str = "", **kwargs):
    """Return the tool result. Code runs inside the isolated executor."""
    return f"Processed: {query}"
`
const newPlugin = () => ({ name: '', description: '', kind: 'http', category: 'Tool', version: '0.1.0', module: '', class_name: 'CustomTool', package: '', source_code: pythonTemplate, source_path: '', input_schema: {}, env_vars: [], permissions: [], endpoint: '', method: 'POST', request_template: {}, response_path: '', server_url: '', command: '', args: [], headers: {}, auth_header: 'Authorization', auth_token: '', app_slug: '', cache_tools_list: true, enabled: true })
const plugin = ref(newPlugin())
const headersText = ref('{}')
const schemaText = ref('{}')
const argsText = ref('')
const envText = ref('')
const permissionText = ref('')
const items = computed(() => (tab.value === 'skills' ? store.skills : store.plugins).filter(item => `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(search.value.toLowerCase())))
const kindLabels = { python: 'Python tool', http: 'HTTP API tool', mcp_http: 'MCP tool', mcp_sse: 'MCP tool', app: 'Connected app tool' }
const transportLabels = { mcp_http: 'Streamable HTTP', mcp_sse: 'SSE' }
const actionChoices = [
  { id: 'http', label: 'HTTP API', detail: 'Call an API endpoint', icon: Globe2 },
  { id: 'python', label: 'Python tool', detail: 'Load an installed Python class', icon: Code2 },
  { id: 'mcp', label: 'MCP server', detail: 'Connect to one MCP server', icon: Network },
  { id: 'app', label: 'Connected app', detail: 'Authorized by CrewAI platform token', icon: AppWindow, disabled: computed(() => !store.runtime?.connected_apps?.configured) },
]
function iconFor(kind) { return kind === 'python' ? Code2 : kind === 'app' ? AppWindow : kind.startsWith('mcp_') ? Network : Globe2 }
function choiceDisabled(choice) { return Boolean(choice.disabled?.value) }
function chooseAction(type) {
  const choice = actionChoices.find(item => item.id === type)
  if (choiceDisabled(choice)) { store.error = '服务端尚未配置 CREWAI_PLATFORM_INTEGRATION_TOKEN'; return }
  actionType.value = type; plugin.value.kind = type === 'mcp' ? mcpTransport.value : type
}
function openPlugin() { plugin.value = newPlugin(); actionType.value = 'http'; pythonMode.value = 'source'; mcpTransport.value = 'mcp_http'; headersText.value = '{}'; schemaText.value = '{}'; requestText.value = '{}'; argsText.value = ''; envText.value = ''; permissionText.value = ''; modal.value = true }
function editPlugin(item) {
  modal.value = true
  const editable = JSON.parse(JSON.stringify(item || {}))
  plugin.value = { ...newPlugin(), ...editable }
  actionType.value = item.kind?.startsWith('mcp_') ? 'mcp' : item.kind || 'http'
  mcpTransport.value = item.kind?.startsWith('mcp_') ? item.kind : 'mcp_http'
  headersText.value = JSON.stringify(item.headers || {}, null, 2); schemaText.value = JSON.stringify(item.input_schema || {}, null, 2); requestText.value = JSON.stringify(item.request_template || {}, null, 2)
  argsText.value = (item.args || []).join('\n'); envText.value = (item.env_vars || []).join('\n'); permissionText.value = (item.permissions || []).join('\n')
}
function changeTransport(value) { mcpTransport.value = value; plugin.value.kind = value }
async function savePlugin() {
  try {
    plugin.value.headers = JSON.parse(headersText.value || '{}'); plugin.value.input_schema = JSON.parse(schemaText.value || '{}'); plugin.value.request_template = JSON.parse(requestText.value || '{}')
    plugin.value.args = argsText.value.split('\n').map(item => item.trim()).filter(Boolean); plugin.value.env_vars = envText.value.split(/[\n,]/).map(item => item.trim()).filter(Boolean); plugin.value.permissions = permissionText.value.split(/[\n,]/).map(item => item.trim()).filter(Boolean)
    if (actionType.value === 'python') {
      if (pythonMode.value === 'source') plugin.value.module = ''
      else plugin.value.source_code = ''
    }
    if (actionType.value === 'app' && !plugin.value.env_vars.includes('CREWAI_PLATFORM_INTEGRATION_TOKEN')) plugin.value.env_vars.push('CREWAI_PLATFORM_INTEGRATION_TOKEN')
    await api.savePlugin(plugin.value); modal.value = false; await store.load(); store.notify('工具已保存')
    if (route.query.returnTo) await router.push(String(route.query.returnTo))
  } catch (error) { store.error = error.message }
}
async function remove(item) {
  const resourceLabel = tab.value === 'skills' ? 'Skill' : '工具'
  if (!(await confirmDialog({
    title: `删除${resourceLabel}`,
    message: `确认删除“${item.name}”？已引用它的智能体可能需要重新配置。`,
    confirmLabel: `删除${resourceLabel}`,
    danger: true,
  }))) return
  tab.value === 'skills' ? await api.deleteSkill(item.id) : await api.deletePlugin(item.id)
  await store.load()
}
function stageFolder(event) { importFiles.value = [...(event.target.files || [])]; event.target.value = '' }
async function filesFromEntry(entry, prefix = '') {
  if (entry.isFile) return [await new Promise((resolve, reject) => entry.file(file => { Object.defineProperty(file, 'webkitRelativePath', { value: `${prefix}${file.name}`, configurable: true }); resolve(file) }, reject))]
  if (!entry.isDirectory) return []
  const reader = entry.createReader(); const children = []
  while (true) { const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject)); if (!batch.length) break; children.push(...batch) }
  return (await Promise.all(children.map(child => filesFromEntry(child, `${prefix}${entry.name}/`)))).flat()
}
async function dropFolder(event) {
  try {
    const entries = [...(event.dataTransfer?.items || [])].map(item => item.webkitGetAsEntry?.()).filter(Boolean)
    importFiles.value = entries.length ? (await Promise.all(entries.map(entry => filesFromEntry(entry)))).flat() : [...(event.dataTransfer?.files || [])]
    if (!importFiles.value.length) throw new Error('未读取到文件，请点击选择文件夹重试')
  } catch (error) { importFiles.value = []; store.error = error.message }
}
async function importFolder() { if (!importFiles.value.length || importing.value) return; importing.value = true; importProgress.value=0; try { await api.importSkills(importFiles.value,value=>{importProgress.value=value}); await store.load(); importModal.value = false; importFiles.value = []; store.notify('Skill package 已检查并导入') } catch (error) { importFiles.value = []; importProgress.value = 0; store.error = `${error.message}，请重新选择文件夹` } finally { importing.value = false } }
function formatSize(value) { return value < 1024 * 1024 ? `${Math.ceil(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB` }
onMounted(async () => {
  if (!store.skills.length && !store.plugins.length) await store.load()
  if (route.query.tab === 'actions') tab.value = 'actions'
  if (route.query.create === '1' && tab.value === 'actions') openPlugin()
})
</script>

<template>
  <div class="page-heading"><div><h2>Agent capabilities</h2><p>Skills 提供按需加载的知识与流程；Tools 让 Agent 调用代码、API、MCP 或外部应用。</p></div><div class="heading-actions"><template v-if="tab==='skills'"><button class="button" @click="importModal=true"><FolderUp :size="14" />导入 Skill 文件夹</button><button class="button primary" @click="router.push('/skill-dev')"><Code2 :size="14" />创建 Skill</button></template><button v-else class="button primary" @click="openPlugin"><Plus :size="15" />添加工具</button><input ref="folderInput" type="file" hidden webkitdirectory directory multiple @change="stageFolder" /></div></div>
  <div class="toolbar"><div class="toolbar-left"><div class="segmented"><button :class="{active:tab==='skills'}" @click="tab='skills'">Skills</button><button :class="{active:tab==='actions'}" @click="tab='actions'">Tools</button></div><label class="search-input"><Search :size="14" /><input v-model="search" placeholder="搜索 Skill 或工具" /></label></div><span class="resource-count">{{ items.length }} items</span></div>

  <section v-if="items.length" class="resource-grid"><article v-for="item in items" :key="item.id" class="resource-card is-editable" @click="tab==='skills'?router.push(`/skill-dev/${item.id}`):editPlugin(item)"><div class="resource-card-top"><span class="resource-icon"><BookOpen v-if="tab==='skills'" :size="17" /><component v-else :is="iconFor(item.kind)" :size="17" /></span><div class="card-actions"><button class="icon-button" title="编辑" @click.stop="tab==='skills'?router.push(`/skill-dev/${item.id}`):editPlugin(item)"><SquarePen :size="14" /></button><button class="icon-button" title="删除" @click.stop="remove(item)"><Trash2 :size="14" /></button></div></div><h3>{{ item.name }}</h3><p>{{ item.description }}</p><div class="resource-meta"><span class="tag">{{ tab==='skills'?(item.source==='registry'?item.registry_ref:item.slug):kindLabels[item.kind] }}</span><span v-if="tab==='actions'&&item.kind.startsWith('mcp_')" class="tag">{{ transportLabels[item.kind] }}</span><span class="tag">{{ item.enabled?'Enabled':'Disabled' }}</span></div></article></section>
  <EmptyState v-else :title="tab==='skills'?'还没有 Skill':'还没有工具'" :detail="tab==='skills'?'导入标准 Skill 文件夹，或在在线编辑器中创建 package。':'添加 HTTP API、Python Tool、MCP Tool 或已授权的应用工具。'"><div v-if="tab==='skills'" class="empty-actions"><button class="button" @click="importModal=true"><FolderUp :size="14" />导入文件夹</button><button class="button accent" @click="router.push('/skill-dev')"><Code2 :size="14" />创建 Skill</button></div><button v-else class="button accent" @click="openPlugin">添加工具</button></EmptyState>

  <div v-if="importModal" class="modal-backdrop" @click.self="!importing&&(importModal=false)"><section class="modal"><header class="modal-header"><div><span class="eyebrow">IMPORT SKILL</span><h2>导入 Skill 文件夹</h2></div><button class="icon-button" :disabled="importing" @click="importModal=false"><X :size="16"/></button></header><div class="modal-body skill-import-body"><button class="skill-dropzone" :disabled="importing" @click="folderInput?.click()" @dragover.prevent @drop.prevent="dropFolder"><FolderUp :size="24"/><strong>选择或拖入 Skill 文件夹</strong><small>必须包含一个带 YAML front matter 的 SKILL.md；所有路径和文件大小会在服务端再次检查。</small></button><div v-if="importFiles.length" class="import-summary"><strong>{{ importFiles.length }} 个文件 · {{ formatSize(importFiles.reduce((sum,file)=>sum+file.size,0)) }}</strong><div><span v-for="file in importFiles.slice(0,6)" :key="file.webkitRelativePath||file.name">{{ file.webkitRelativePath||file.name }}</span><small v-if="importFiles.length>6">还有 {{ importFiles.length-6 }} 个文件</small></div></div><div v-if="importing" class="upload-status"><div class="upload-status-head"><span>正在上传并校验 Skill package</span><b>{{ importProgress }}%</b></div><div class="upload-progress-track"><i :style="{width:`${importProgress}%`}"></i></div></div></div><footer class="modal-footer"><button class="button" :disabled="importing" @click="importModal=false">取消</button><button class="button primary" :disabled="!importFiles.length||importing" @click="importFolder">{{ importing?(importProgress<100?'上传中...':'校验中...'):'检查并导入' }}</button></footer></section></div>

  <div v-if="modal" class="modal-backdrop" @click.self="modal=false"><section class="modal large"><header class="modal-header"><div><span class="eyebrow">AGENT TOOL</span><h2>{{ plugin.id ? '编辑' : '添加' }} Agent 可调用的工具</h2></div><button class="icon-button" @click="modal=false"><X :size="16" /></button></header><div class="modal-body">
    <div class="action-type-grid"><button v-for="choice in actionChoices" :key="choice.id" :class="{active:actionType===choice.id}" :disabled="choiceDisabled(choice)" @click="chooseAction(choice.id)"><component :is="choice.icon" :size="17" /><span><strong>{{ choice.label }}</strong><small>{{ choiceDisabled(choice)?'需先配置平台集成令牌':choice.detail }}</small></span></button></div>
    <div class="form-grid action-form"><div class="field"><label>Name</label><input v-model="plugin.name" /></div><div class="field"><label>Category</label><input v-model="plugin.category" /></div><div class="field full"><label>Description</label><textarea v-model="plugin.description" placeholder="说明 Agent 何时调用、能完成什么以及限制。"></textarea></div>
      <template v-if="actionType==='python'"><div class="field full"><label>执行方式</label><div class="segmented transport-switch"><button class="active">隔离源码</button></div></div><div class="field full"><label>Python source</label><textarea v-model="plugin.source_code" class="python-source"></textarea><small>必须定义 run(**kwargs) 或 main(**kwargs)。源码只在统一 executor 容器及当前应用工作目录中执行，不会加载进 Worker。</small></div><div class="field full"><label>Input schema (JSON Schema)</label><textarea v-model="schemaText"></textarea></div></template>
      <template v-if="actionType==='http'"><div class="field full"><label>Endpoint</label><input v-model="plugin.endpoint" placeholder="https://api.example.com/search/{query}" /></div><div class="field"><label>Method</label><select v-model="plugin.method"><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select></div><div class="field"><label>Response path</label><input v-model="plugin.response_path" placeholder="data.items" /></div><div class="field full"><label>Request template (JSON)</label><textarea v-model="requestText"></textarea></div><div class="field full"><label>Input schema (JSON Schema)</label><textarea v-model="schemaText"></textarea></div></template>
      <template v-if="actionType==='mcp'"><div class="field full"><label>Connection</label><div class="segmented transport-switch"><button v-for="(label,key) in transportLabels" :key="key" :class="{active:mcpTransport===key}" @click="changeTransport(key)">{{ label }}</button></div></div><div class="field full"><label>Server URL</label><input v-model="plugin.server_url" placeholder="https://mcp.example.com/mcp" /></div><div class="field"><label>Auth header</label><input v-model="plugin.auth_header" /></div><div class="field"><label>Token</label><input v-model="plugin.auth_token" type="password" /></div><div class="field full"><label>Additional headers (JSON)</label><textarea v-model="headersText"></textarea></div><div class="field full"><small>为保持统一沙箱边界，仅支持远程 Streamable HTTP / SSE；本地 stdio 命令不会在 Worker 中执行。</small></div></template>
      <template v-if="actionType==='app'"><div class="field full"><label>App name</label><input v-model="plugin.app_slug" placeholder="gmail" /><small>填写 CrewAI 支持且已授权的应用名，例如 gmail、slack、github。服务环境必须配置 CREWAI_PLATFORM_INTEGRATION_TOKEN。</small></div></template>
      <div v-if="actionType==='python'" class="field full"><label>Required environment variables</label><textarea v-model="envText" placeholder="API_KEY, DATABASE_URL"></textarea><small>只允许将这里声明且服务端已配置的变量传给该工具的隔离进程；普通代码执行无法读取。</small></div><div class="field full"><label>Permissions / scopes</label><textarea v-model="permissionText"></textarea></div><label v-if="actionType==='mcp'" class="toggle-row"><span>Cache server tool list</span><input v-model="plugin.cache_tools_list" class="toggle" type="checkbox" /></label><label class="toggle-row"><span>Enabled</span><input v-model="plugin.enabled" class="toggle" type="checkbox" /></label>
    </div></div><footer class="modal-footer"><button class="button" @click="modal=false">取消</button><button class="button primary" :disabled="!plugin.name||!plugin.description" @click="savePlugin">保存工具</button></footer></section></div>
</template>
