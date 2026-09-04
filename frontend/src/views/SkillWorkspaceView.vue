<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft, Check, ChevronDown, ChevronRight, ChevronsDownUp, FileCode2,
  FilePlus2, Folder, FolderOpen, FolderPlus, PanelRightClose, PanelRightOpen,
  Pencil, Save, Trash2, X,
} from 'lucide-vue-next'
import { api } from '../services/api'
import { confirmDialog } from '../services/dialog'
import { timestampValue } from '../services/dateFormatting'
import { usePlatformStore } from '../stores/platform'

const route = useRoute()
const router = useRouter()
const store = usePlatformStore()
const DEFAULT_FOLDERS = ['references', 'scripts', 'assets']
const LOCAL_DRAFT_LIMIT = 1_500_000

const freshSkill = () => ({
  id: null, revision: null, updated_at: '', name: 'New skill', slug: '',
  description: 'Describe when this skill applies.',
  instructions: '## Workflow\n\nWrite the domain procedure here.',
  version: '1.0.0', author: 'local', source: 'local',
  registry_ref: '', files: [], directories: [], enabled: true, status: 'draft',
})
const clone = value => JSON.parse(JSON.stringify(value))
const fileKey = path => `file:${path}`
const baseName = path => String(path || '').split('/').filter(Boolean).at(-1) || ''
const parentPath = path => String(path || '').split('/').filter(Boolean).slice(0, -1).join('/')
const kindForPath = path => path === 'scripts' || path.startsWith('scripts/') ? 'script' : path === 'assets' || path.startsWith('assets/') ? 'asset' : 'reference'

const skill = ref(freshSkill())
const selectedKey = ref(fileKey('SKILL.md'))
const selectedFolder = ref('')
const openKeys = ref([fileKey('SKILL.md')])
const expandedFolders = ref(new Set(DEFAULT_FOLDERS))
const modifiedKeys = ref(new Set())
const creating = ref(null)
const createInput = ref(null)
const editor = ref(null)
const metadataOpen = ref(true)
const cursor = ref({ line: 1, column: 1 })
const saved = ref('Ready')
let hydrating = true
let dirty = false
let editVersion = 0
let draftTimer = null
let localTimer = null
let savePromise = null
let loadVersion = 0
let promotedRouteId = ''

function yamlScalar(value) { return JSON.stringify(String(value ?? '')) }
function skillDocument() {
  return `---\nname: ${skill.value.slug || 'new-skill'}\ndescription: ${yamlScalar(skill.value.description)}\nmetadata:\n  author: ${yamlScalar(skill.value.author || 'local')}\n  version: ${yamlScalar(skill.value.version || '1.0.0')}\n---\n\n${skill.value.instructions || ''}`
}
const files = computed(() => [
  { key: fileKey('SKILL.md'), path: 'SKILL.md', kind: 'skill', content: skillDocument(), encoding: 'utf8' },
  ...(skill.value.files || []).map(file => ({ ...file, key: fileKey(file.path) })),
])
const activeFile = computed(() => files.value.find(file => file.key === selectedKey.value) || files.value[0])
const activeIsBinary = computed(() => activeFile.value?.encoding === 'base64')
const activeLanguage = computed(() => {
  const extension = activeFile.value?.path?.split('.').at(-1)?.toLowerCase()
  return ({ py: 'Python', md: 'Markdown', json: 'JSON', yaml: 'YAML', yml: 'YAML', js: 'JavaScript', ts: 'TypeScript', css: 'CSS', html: 'HTML', csv: 'CSV' })[extension] || 'Plain Text'
})

const directoryPaths = computed(() => {
  const result = new Set(DEFAULT_FOLDERS)
  const addWithParents = path => {
    const parts = String(path || '').split('/').filter(Boolean)
    parts.forEach((_, index) => result.add(parts.slice(0, index + 1).join('/')))
  }
  ;(skill.value.directories || []).forEach(addWithParents)
  ;(skill.value.files || []).forEach(file => addWithParents(parentPath(file.path)))
  return result
})

const treeEntries = computed(() => {
  const result = [{ key: fileKey('SKILL.md'), type: 'file', path: 'SKILL.md', name: 'SKILL.md', depth: 0 }]
  const children = new Map()
  const add = (parent, entry) => children.set(parent, [...(children.get(parent) || []), entry])
  ;[...directoryPaths.value].forEach(path => add(parentPath(path), { key: `folder:${path}`, type: 'folder', path, name: baseName(path) }))
  ;(skill.value.files || []).forEach(file => add(parentPath(file.path), { key: fileKey(file.path), type: 'file', path: file.path, name: baseName(file.path) }))
  const visit = (parent, depth) => {
    const items = (children.get(parent) || []).sort((a, b) => a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'folder' ? -1 : 1)
    items.forEach(item => {
      result.push({ ...item, depth })
      if (item.type === 'folder' && expandedFolders.value.has(item.path)) visit(item.path, depth + 1)
    })
  }
  visit('', 0)
  return result
})
const selectedFolderIsDefault = computed(() => DEFAULT_FOLDERS.includes(selectedFolder.value))

function normalizePath(value, segmentOnly = false) {
  const raw = String(value || '').trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')
  if (!raw || /[<>:"|?*\u0000-\u001f]/.test(raw)) return ''
  const parts = raw.split('/')
  if (parts.some(part => !part || part === '.' || part === '..') || (segmentOnly && parts.length !== 1)) return ''
  return parts.join('/')
}
function normalizedSkill(value) {
  const result = { ...freshSkill(), ...clone(value || {}) }
  delete result.category
  result.files = (result.files || []).map(file => {
    const path = String(file.path || '').replace(/\\/g, '/').replace(/^\/+/, '')
    return { ...file, path, kind: kindForPath(path), content: typeof file.content === 'string' ? file.content : '', encoding: file.encoding === 'base64' ? 'base64' : 'utf8' }
  })
  result.directories = [...new Set(result.directories || [])]
  return result
}

function draftKey(id = route.params.id || skill.value.id || 'new') { return `xuanshu-skill-draft:${id}` }
function readLocalDraft(item) {
  const key = draftKey()
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    if (raw.length > LOCAL_DRAFT_LIMIT) { localStorage.removeItem(key); return null }
    const local = JSON.parse(raw)
    return local?.data && (!item || timestampValue(local.saved_at) > timestampValue(item.updated_at)) ? local.data : null
  } catch { localStorage.removeItem(key); return null }
}
function approximateSize() {
  return (skill.value.instructions?.length || 0) + (skill.value.files || []).reduce((sum, file) => sum + (file.content?.length || 0), 0)
}
function persistLocalDraft() {
  if (!dirty) return
  const key = draftKey()
  if (approximateSize() > LOCAL_DRAFT_LIMIT) { localStorage.removeItem(key); return }
  try { localStorage.setItem(key, JSON.stringify({ saved_at: new Date().toISOString(), data: skill.value })) }
  catch { localStorage.removeItem(key) }
}
function scheduleLocalDraft() {
  if (localTimer) window.clearTimeout(localTimer)
  localTimer = window.setTimeout(persistLocalDraft, 350)
}
function scheduleDraft() {
  if (draftTimer) window.clearTimeout(draftTimer)
  draftTimer = window.setTimeout(() => persistSkill('draft'), 900)
}

async function load() {
  if (route.name !== 'skill-dev') return
  const token = ++loadVersion
  hydrating = true
  const routeId = String(route.params.id || '')
  const item = routeId ? store.skills.find(entry => String(entry.id) === routeId) : null
  const local = readLocalDraft(item)
  if (token !== loadVersion) return
  skill.value = normalizedSkill(local || item || freshSkill())
  selectedKey.value = fileKey('SKILL.md'); selectedFolder.value = ''
  openKeys.value = [fileKey('SKILL.md')]; expandedFolders.value = new Set(DEFAULT_FOLDERS)
  modifiedKeys.value = new Set(); dirty = Boolean(local); saved.value = local ? 'Unsaved changes' : 'Ready'
  await nextTick(); hydrating = false
}
watch(skill, () => {
  if (hydrating) return
  dirty = true; editVersion += 1; saved.value = 'Unsaved changes'
  scheduleLocalDraft(); scheduleDraft()
}, { deep: true })
watch(() => route.params.id, (id) => {
  if (promotedRouteId && String(id || '') === promotedRouteId) {
    promotedRouteId = ''
    return
  }
  load()
})
onMounted(async () => {
  if (!store.skills.length) await store.load()
  await load()
  window.addEventListener('keydown', globalKeydown)
})
onBeforeUnmount(() => {
  if (draftTimer) window.clearTimeout(draftTimer)
  if (localTimer) window.clearTimeout(localTimer)
  persistLocalDraft()
  window.removeEventListener('keydown', globalKeydown)
})

function markModified(key = selectedKey.value) { modifiedKeys.value = new Set([...modifiedKeys.value, key]) }
function openFile(key) {
  if (!files.value.some(file => file.key === key)) return
  selectedKey.value = key
  if (!openKeys.value.includes(key)) openKeys.value = [...openKeys.value, key]
  if (key !== fileKey('SKILL.md')) selectedFolder.value = parentPath(key.slice(5))
  nextTick(updateCursor)
}
function closeFile(key) {
  const index = openKeys.value.indexOf(key)
  openKeys.value = openKeys.value.filter(item => item !== key)
  if (!openKeys.value.length) openKeys.value = [fileKey('SKILL.md')]
  if (selectedKey.value === key) selectedKey.value = openKeys.value[Math.max(0, index - 1)] || openKeys.value[0]
}
function toggleFolder(path) {
  selectedFolder.value = path
  const expanded = new Set(expandedFolders.value)
  expanded.has(path) ? expanded.delete(path) : expanded.add(path)
  expandedFolders.value = expanded
}
function pathExists(path) { return path === 'SKILL.md' || directoryPaths.value.has(path) || (skill.value.files || []).some(file => file.path === path) }

function startCreate(type) {
  const parent = selectedFolder.value
  if (parent) expandedFolders.value = new Set([...expandedFolders.value, parent])
  const extension = parent === 'scripts' || parent.startsWith('scripts/') ? 'py' : parent === 'assets' || parent.startsWith('assets/') ? 'txt' : 'md'
  creating.value = { type, parent, name: type === 'file' ? `untitled.${extension}` : 'new-folder' }
  nextTick(() => { createInput.value?.focus(); createInput.value?.select() })
}
function startRenameFolder() {
  if (!selectedFolder.value || selectedFolderIsDefault.value) return
  creating.value = { type: 'rename-folder', parent: parentPath(selectedFolder.value), path: selectedFolder.value, name: baseName(selectedFolder.value) }
  nextTick(() => { createInput.value?.focus(); createInput.value?.select() })
}
function finishCreate() {
  const state = creating.value
  if (!state) return
  const name = normalizePath(state.name, true)
  if (!name) { store.error = '名称不能为空，且不能包含路径分隔符或特殊字符'; return }
  const path = [state.parent, name].filter(Boolean).join('/')
  if (state.type === 'rename-folder') {
    if (path !== state.path && pathExists(path)) { store.error = `路径 ${path} 已存在`; return }
    renameFolderPath(state.path, path)
  } else if (pathExists(path)) { store.error = `路径 ${path} 已存在`; return }
  else if (state.type === 'folder') {
    skill.value.directories = [...new Set([...(skill.value.directories || []), path])]
    selectedFolder.value = path; expandedFolders.value = new Set([...expandedFolders.value, path])
  } else {
    skill.value.files.push({ path, kind: kindForPath(path), content: '', encoding: 'utf8', executable: kindForPath(path) === 'script' })
    markModified(fileKey(path)); openFile(fileKey(path))
  }
  creating.value = null
}
function renameFolderPath(previous, next) {
  const remap = path => path === previous ? next : path.startsWith(`${previous}/`) ? `${next}${path.slice(previous.length)}` : path
  skill.value.directories = [...new Set((skill.value.directories || []).map(remap))]
  const keyMap = new Map()
  skill.value.files.forEach(file => {
    const oldKey = fileKey(file.path); file.path = remap(file.path); file.kind = kindForPath(file.path); keyMap.set(oldKey, fileKey(file.path))
  })
  openKeys.value = openKeys.value.map(key => keyMap.get(key) || key)
  selectedKey.value = keyMap.get(selectedKey.value) || selectedKey.value
  modifiedKeys.value = new Set([...modifiedKeys.value].map(key => keyMap.get(key) || key))
  expandedFolders.value = new Set([...expandedFolders.value].map(remap)); expandedFolders.value.add(next)
  selectedFolder.value = next
}

function parseScalar(raw) {
  const value = String(raw || '').trim()
  if (value.startsWith('"')) { try { return JSON.parse(value) } catch { return value.replace(/^"|"$/g, '') } }
  return value.replace(/^['"]|['"]$/g, '')
}
function updateActive(value) {
  if (activeFile.value.kind === 'skill') {
    const match = value.match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/)
    if (!match) skill.value.instructions = value
    else {
      const read = key => { const found = match[1].match(new RegExp(`^${key}:\\s*(.*)$`, 'mi')); return found ? parseScalar(found[1]) : '' }
      skill.value.instructions = match[2].replace(/^\n/, '')
      skill.value.slug = read('name') || skill.value.slug; skill.value.description = read('description') || skill.value.description
      const author = match[1].match(/^\s+author:\s*(.*)$/mi); const version = match[1].match(/^\s+version:\s*(.*)$/mi)
      if (author) skill.value.author = parseScalar(author[1]); if (version) skill.value.version = parseScalar(version[1])
    }
  } else {
    const file = skill.value.files.find(item => fileKey(item.path) === selectedKey.value)
    if (file && file.encoding !== 'base64') file.content = value
  }
  markModified(); nextTick(updateCursor)
}
function renameActive(event) {
  const file = skill.value.files.find(item => fileKey(item.path) === selectedKey.value)
  if (!file) return
  const next = normalizePath(event.target.value)
  if (!next || (next !== file.path && pathExists(next))) {
    store.error = !next ? '请输入有效的相对文件路径' : `路径 ${next} 已存在`; event.target.value = file.path; return
  }
  const oldKey = fileKey(file.path); file.path = next; file.kind = kindForPath(next)
  const nextKey = fileKey(next); selectedKey.value = nextKey
  openKeys.value = openKeys.value.map(key => key === oldKey ? nextKey : key)
  const changed = new Set(modifiedKeys.value); changed.delete(oldKey); changed.add(nextKey); modifiedKeys.value = changed
  selectedFolder.value = parentPath(next)
}
async function removeActive() {
  const file = activeFile.value
  if (!file || file.kind === 'skill') return
  if (!await confirmDialog({ title: '删除文件', message: `确定删除 ${file.path}？保存后无法恢复。`, confirmLabel: '删除', danger: true })) return
  skill.value.files = skill.value.files.filter(item => item.path !== file.path)
  modifiedKeys.value = new Set([...modifiedKeys.value].filter(key => key !== file.key)); closeFile(file.key)
}
async function removeSelectedFolder() {
  const path = selectedFolder.value
  if (!path) return
  const affected = (skill.value.files || []).filter(file => file.path.startsWith(`${path}/`))
  if (!await confirmDialog({
    title: selectedFolderIsDefault.value ? '清空目录' : '删除目录',
    message: affected.length ? `将同时删除 ${path} 中的 ${affected.length} 个文件。` : `确定删除空目录 ${path}？`,
    confirmLabel: selectedFolderIsDefault.value ? '清空' : '删除', danger: true,
  })) return
  const removed = new Set(affected.map(file => fileKey(file.path)))
  skill.value.files = skill.value.files.filter(file => !removed.has(fileKey(file.path)))
  skill.value.directories = (skill.value.directories || []).filter(directory => directory !== path && !directory.startsWith(`${path}/`))
  openKeys.value = openKeys.value.filter(key => !removed.has(key)); modifiedKeys.value = new Set([...modifiedKeys.value].filter(key => !removed.has(key)))
  selectedFolder.value = parentPath(path)
  if (!openKeys.value.includes(selectedKey.value)) openFile(openKeys.value.at(-1) || fileKey('SKILL.md'))
}

function updateCursor() {
  if (!editor.value) return
  const lines = editor.value.value.slice(0, editor.value.selectionStart).split('\n')
  cursor.value = { line: lines.length, column: (lines.at(-1)?.length || 0) + 1 }
}
function editorKeydown(event) {
  if (event.key !== 'Tab' || activeIsBinary.value) return
  event.preventDefault()
  const target = event.target; const position = target.selectionStart + 2
  updateActive(`${target.value.slice(0, target.selectionStart)}  ${target.value.slice(target.selectionEnd)}`)
  nextTick(() => { target.selectionStart = position; target.selectionEnd = position; updateCursor() })
}
function globalKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save() }
}
function generatedSlug() {
  return skill.value.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `skill-${Date.now().toString(36)}`
}
function validationError() {
  const slug = skill.value.slug || generatedSlug()
  if (!skill.value.name.trim()) return 'Skill 名称不能为空'
  if (skill.value.description.trim().length < 10) return '触发说明至少需要 10 个字符'
  if (!skill.value.instructions.trim()) return 'SKILL.md 必须包含正文指令'
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug)) return 'Slug 只能使用小写英文、数字和单连字符'
  const paths = (skill.value.files || []).map(file => file.path)
  if (paths.some(path => !normalizePath(path))) return 'Skill 中存在无效文件路径'
  if (new Set(paths).size !== paths.length) return 'Skill 中存在重复文件路径'
  return ''
}
function upsertStoreSkill(result) {
  const index = store.skills.findIndex(item => String(item.id) === String(result.id))
  index >= 0 ? store.skills.splice(index, 1, clone(result)) : store.skills.push(clone(result))
}
async function persistSkill(status = 'draft', explicit = false) {
  if (!dirty && !explicit) return null
  if (savePromise) { await savePromise; return dirty || explicit ? persistSkill(status, explicit) : null }
  const invalid = validationError()
  if (invalid) { saved.value = 'Needs attention'; if (explicit) store.error = invalid; return null }
  const oldDraftKey = draftKey(); const routeAtStart = route.fullPath; const startVersion = editVersion
  const payload = clone({ ...skill.value, slug: skill.value.slug || generatedSlug(), status, _base_revision: skill.value.revision })
  saved.value = status === 'published' ? 'Saving...' : 'Saving draft...'
  const operation = (async () => {
    try {
      const result = await api.saveSkill(payload)
      const stillEditingThisSkill = route.name === 'skill-dev' && route.fullPath === routeAtStart
      if (stillEditingThisSkill) {
        hydrating = true
        Object.assign(skill.value, { id: result.id, revision: result.revision, updated_at: result.updated_at, slug: result.slug, status: result.status, package_path: result.package_path })
        await nextTick(); hydrating = false
      }
      upsertStoreSkill(result)
      if (stillEditingThisSkill && editVersion === startVersion) {
        dirty = false; modifiedKeys.value = new Set(); saved.value = status === 'published' ? 'Saved' : 'Draft saved'
        localStorage.removeItem(oldDraftKey); localStorage.removeItem(draftKey(result.id))
      } else if (stillEditingThisSkill) { dirty = true; saved.value = 'Unsaved changes' }
      if (route.name === 'skill-dev' && route.fullPath === routeAtStart && String(route.params.id || '') !== String(result.id)) {
        promotedRouteId = String(result.id)
        await router.replace({ path: `/skill-dev/${result.id}`, query: route.query.returnTo ? { returnTo: route.query.returnTo } : {} })
      }
      return result
    } catch (error) { hydrating = false; dirty = true; saved.value = 'Draft pending'; store.error = error.message; return null }
    finally { savePromise = null; if (dirty && route.name === 'skill-dev') scheduleDraft() }
  })()
  savePromise = operation
  return operation
}
async function save() {
  const routeAtStart = route.fullPath
  let result = await persistSkill('published', true)
  if (dirty && result) result = await persistSkill('published', true)
  if (!result) return
  store.notify('Skill package 已保存')
  if (route.name === 'skill-dev' && route.fullPath === routeAtStart && route.query.returnTo) await router.push(String(route.query.returnTo))
}
</script>

<template>
  <div class="dev-page">
    <header class="dev-toolbar">
      <button class="button ghost" @click="router.push('/resources')"><ArrowLeft :size="15" />Capabilities</button>
      <div class="dev-title"><input v-model="skill.name" aria-label="Skill name" @input="markModified('file:SKILL.md')" /><span>{{ saved }}</span></div>
      <div class="dev-toolbar-actions">
        <button class="icon-button" :title="metadataOpen?'隐藏元数据':'显示元数据'" @click="metadataOpen=!metadataOpen"><PanelRightClose v-if="metadataOpen" :size="15" /><PanelRightOpen v-else :size="15" /></button>
        <button class="button primary" @click="save"><Save :size="14" />保存 Skill</button>
      </div>
    </header>
    <div class="dev-layout" :class="{'with-metadata':metadataOpen}">
      <aside class="dev-files">
        <div class="dev-files-head"><strong>EXPLORER</strong><div>
          <button class="dev-action" title="新建文件" @click="startCreate('file')"><FilePlus2 :size="14" /></button>
          <button class="dev-action" title="新建目录" @click="startCreate('folder')"><FolderPlus :size="14" /></button>
          <button class="dev-action" title="折叠目录" @click="expandedFolders=new Set()"><ChevronsDownUp :size="14" /></button>
        </div></div>
        <button class="dev-root" :class="{active:selectedFolder===''}" @click="selectedFolder=''">
          <ChevronDown :size="13" /><FolderOpen :size="14" /><span>{{ skill.slug||'new-skill' }}</span>
        </button>
        <div v-if="creating" class="dev-create-row"><span>{{ creating.parent ? `${creating.parent}/` : '' }}</span><input ref="createInput" v-model="creating.name" @keydown.enter.prevent="finishCreate" @keydown.esc.prevent="creating=null" @blur="finishCreate" /></div>
        <div class="dev-tree">
          <button v-for="entry in treeEntries" :key="entry.key" class="dev-file tree-entry" :class="[{active:selectedKey===entry.key||(entry.type==='folder'&&selectedFolder===entry.path)},entry.type]" :style="{paddingLeft:`${8+entry.depth*14}px`}" @click="entry.type==='folder'?toggleFolder(entry.path):openFile(entry.key)">
            <template v-if="entry.type==='folder'"><ChevronDown v-if="expandedFolders.has(entry.path)" :size="13" /><ChevronRight v-else :size="13" /><FolderOpen v-if="expandedFolders.has(entry.path)" :size="14" /><Folder v-else :size="14" /></template>
            <template v-else><span class="dev-tree-spacer"></span><FileCode2 :size="14" /></template>
            <span>{{ entry.name }}</span><i v-if="entry.type==='file'&&modifiedKeys.has(entry.key)"></i>
          </button>
        </div>
        <div v-if="selectedFolder" class="dev-folder-actions"><span :title="selectedFolder">{{ selectedFolder }}</span>
          <button v-if="!selectedFolderIsDefault" class="dev-action" title="重命名目录" @click="startRenameFolder"><Pencil :size="13" /></button>
          <button class="dev-action danger" :title="selectedFolderIsDefault?'清空目录':'删除目录'" @click="removeSelectedFolder"><Trash2 :size="13" /></button>
        </div>
      </aside>
      <section class="dev-editor">
        <div class="dev-tabs"><button v-for="key in openKeys" :key="key" :class="{active:selectedKey===key}" @click="openFile(key)">
          <FileCode2 :size="13" /><span>{{ baseName(key.slice(5)) }}</span><i v-if="modifiedKeys.has(key)"></i><span v-else class="dev-tab-close" title="关闭" @click.stop="closeFile(key)"><X :size="12" /></span>
        </button></div>
        <div class="editor-head"><input v-if="activeFile?.kind!=='skill'" class="editor-path" :value="activeFile?.path" aria-label="File path" @change="renameActive" /><span v-else>SKILL.md</span><div>
          <span v-if="activeIsBinary" class="tag">Binary · read-only</span><button v-if="activeFile?.kind!=='skill'" class="icon-button dark" title="删除文件" @click="removeActive"><Trash2 :size="13" /></button>
        </div></div>
        <textarea ref="editor" :value="activeIsBinary?'':activeFile?.content" :readonly="activeIsBinary" :placeholder="activeIsBinary?'Binary file is not editable in the text editor.':''" spellcheck="false" @input="updateActive($event.target.value)" @keydown="editorKeydown" @click="updateCursor" @keyup="updateCursor"></textarea>
        <footer class="dev-statusbar"><span v-if="activeFile?.kind==='script'">{{ activeFile.executable?'Executable':'Script' }}</span><span>Ln {{ cursor.line }}, Col {{ cursor.column }}</span><span>{{ activeFile?.encoding==='base64'?'Base64':'UTF-8' }}</span><span>{{ activeLanguage }}</span></footer>
      </section>
      <aside v-if="metadataOpen" class="dev-preview"><div class="dev-preview-head"><strong>SKILL METADATA</strong><span class="status-badge published">LOCAL</span></div><div class="form-grid">
        <div class="field full"><label>Slug</label><input v-model="skill.slug" @input="markModified('file:SKILL.md')" /></div>
        <div class="field full"><label>Description / trigger</label><textarea v-model="skill.description" @input="markModified('file:SKILL.md')"></textarea></div>
        <div class="field"><label>Version</label><input v-model="skill.version" @input="markModified('file:SKILL.md')" /></div><div class="field"><label>Author</label><input v-model="skill.author" @input="markModified('file:SKILL.md')" /></div>
        <label class="dev-enable"><input v-model="skill.enabled" type="checkbox" /><span><Check :size="12" />Enabled</span></label>
      </div></aside>
    </div>
  </div>
</template>
