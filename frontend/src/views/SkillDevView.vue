<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, FileCode2, FilePlus2, Folder, FolderOpen, Save, Sparkles, Trash2 } from 'lucide-vue-next'
import { api } from '../services/api'
import { timestampValue } from '../services/dateFormatting'
import { usePlatformStore } from '../stores/platform'

const route = useRoute()
const router = useRouter()
const store = usePlatformStore()
const skill = ref({ id: Math.random().toString(36).slice(2, 12), name: 'New skill', slug: '', description: 'Describe when this skill applies.', instructions: '## Workflow\n\nWrite the domain procedure here.', category: 'Domain expertise', version: '1.0.0', author: 'local', source: 'local', registry_ref: '', files: [], enabled: true, status: 'draft' })
const selectedKey = ref('skill:SKILL.md')
const saved = ref('Ready')
const folderNames = { reference: 'references', script: 'scripts', asset: 'assets' }
let hydrating = true
let dirty = false
let draftTimer = null

function skillDocument() {
  const slug = skill.value.slug || 'new-skill'; const description = (skill.value.description || '').replace(/\r?\n/g, ' '); const version = skill.value.version || '1.0.0'; const author = skill.value.author || 'local'
  return `---\nname: ${slug}\ndescription: ${description}\nmetadata:\n  author: ${author}\n  version: "${version}"\n---\n\n${skill.value.instructions || ''}`
}
const files = computed(() => [{ key: 'skill:SKILL.md', path: 'SKILL.md', kind: 'skill', content: skillDocument() }, ...(skill.value.files || []).map(file => ({ ...file, key: `${file.kind}:${file.path}` }))])
const activeFile = computed(() => files.value.find(item => item.key === selectedKey.value) || files.value[0])
const treeEntries = computed(() => {
  const result = [{ key: 'skill:SKILL.md', type: 'file', name: 'SKILL.md', depth: 1 }]
  for (const kind of ['reference', 'script', 'asset']) {
    const entries = (skill.value.files || []).filter(file => file.kind === kind).sort((a, b) => a.path.localeCompare(b.path))
    result.push({ key: `folder:${kind}`, type: 'folder', name: folderNames[kind], depth: 1 })
    const seen = new Set()
    for (const file of entries) {
      const parts = file.path.split('/').filter(Boolean)
      parts.slice(0, -1).forEach((part, index) => {
        const folderPath = parts.slice(0, index + 1).join('/')
        const key = `folder:${kind}:${folderPath}`
        if (!seen.has(key)) { seen.add(key); result.push({ key, type: 'folder', name: part, depth: index + 2 }) }
      })
      result.push({ key: `${kind}:${file.path}`, type: 'file', name: parts.at(-1) || file.path, depth: parts.length + 1 })
    }
  }
  return result
})
function draftKey(id = route.params.id || skill.value.id || 'new') { return `xuanshu-skill-draft:${id}` }
function load() {
  hydrating = true
  const item = store.skills.find(entry => entry.id === route.params.id)
  if (item) skill.value = JSON.parse(JSON.stringify(item))
  const local = JSON.parse(localStorage.getItem(draftKey()) || 'null')
  if (local?.data && (!item || timestampValue(local.saved_at) > timestampValue(item.updated_at))) skill.value = local.data
  skill.value.status ||= 'draft'; selectedKey.value = 'skill:SKILL.md'; dirty = false
  nextTick(() => { hydrating = false })
}
onMounted(async () => { if (!store.skills.length) await store.load(); load() })
onBeforeUnmount(() => { if (draftTimer) window.clearTimeout(draftTimer); persistLocalDraft() })
watch(() => route.params.id, load)
watch(skill, () => {
  if (hydrating) return
  dirty = true; saved.value = 'Unsaved changes'; persistLocalDraft()
  if (draftTimer) window.clearTimeout(draftTimer)
  draftTimer = window.setTimeout(saveDraft, 900)
}, { deep: true })

function persistLocalDraft() {
  const snapshot = JSON.stringify({ saved_at: new Date().toISOString(), data: skill.value })
  localStorage.setItem(draftKey(), snapshot)
}
async function saveDraft() {
  if (!dirty || !skill.value.name.trim() || !skill.value.description.trim() || !skill.value.instructions.trim()) return
  if (!skill.value.slug) skill.value.slug = skill.value.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `skill-${skill.value.id}`
  if (!/^[a-z0-9_-]+$/.test(skill.value.slug)) return
  dirty = false; saved.value = 'Saving draft...'
  try {
    const previousKey = draftKey(); skill.value.status = 'draft'
    const result = await api.saveSkill(skill.value)
    hydrating = true; skill.value.package_path = result.package_path; await nextTick(); hydrating = false
    saved.value = 'Draft saved'; const snapshot = localStorage.getItem(previousKey); if (snapshot) localStorage.setItem(draftKey(result.id), snapshot)
    await store.load()
    if (route.params.id !== result.id) {
      router.replace({
        path: `/skill-dev/${result.id}`,
        query: route.query.returnTo ? { returnTo: route.query.returnTo } : {},
      })
    }
  } catch (error) { dirty = true; saved.value = 'Draft pending'; store.error = error.message }
}
function updateActive(value) {
  if (activeFile.value.kind === 'skill') {
    const match = value.match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/)
    if (!match) { skill.value.instructions = value; return }
    const front = match[1]; skill.value.instructions = match[2].replace(/^\n/, '')
    const read = key => { const line = front.match(new RegExp(`^${key}:\\s*(.*)$`, 'mi')); return line ? line[1].trim().replace(/^['\"]|['\"]$/g, '') : '' }
    skill.value.slug = read('name') || skill.value.slug; skill.value.description = read('description') || skill.value.description
    const author = front.match(/^\s+author:\s*(.*)$/mi); const version = front.match(/^\s+version:\s*[\"']?(.*?)[\"']?$/mi)
    if (author) skill.value.author = author[1].trim(); if (version) skill.value.version = version[1].trim()
  } else {
    const file = skill.value.files.find(item => `${item.kind}:${item.path}` === selectedKey.value)
    if (file) file.content = value
  }
}
function addFile(kind = 'reference') {
  const extension = kind === 'script' ? 'py' : kind === 'asset' ? 'txt' : 'md'
  const count = skill.value.files.filter(item => item.kind === kind).length + 1
  const path = `untitled-${count}.${extension}`
  skill.value.files.push({ path, kind, content: '', encoding: 'utf8', executable: false })
  selectedKey.value = `${kind}:${path}`
}
function renameActive(path) {
  const file = skill.value.files.find(item => `${item.kind}:${item.path}` === selectedKey.value)
  if (!file) return
  const clean = path.replace(/\\/g, '/').replace(/^\/+/, '')
  if (!clean || clean.split('/').includes('..')) return
  file.path = clean; selectedKey.value = `${file.kind}:${clean}`
}
function removeActive() {
  if (activeFile.value.kind === 'skill') return
  skill.value.files = skill.value.files.filter(item => `${item.kind}:${item.path}` !== selectedKey.value)
  selectedKey.value = 'skill:SKILL.md'
}
async function save() {
  saved.value = 'Saving...'
  try {
    hydrating = true
    if (!skill.value.slug) skill.value.slug = skill.value.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `skill-${Date.now().toString(36)}`
    skill.value.status = 'published'
    const result = await api.saveSkill(skill.value); skill.value = result; await nextTick(); hydrating = false
    await store.load(); saved.value = 'Saved'; store.notify('Skill package 已保存')
    dirty = false; localStorage.removeItem(draftKey(result.id))
    if (route.query.returnTo) await router.push(String(route.query.returnTo))
    else if (route.params.id !== result.id) router.replace(`/skill-dev/${result.id}`)
  } catch (error) { hydrating = false; saved.value = 'Failed'; store.error = error.message }
}
</script>

<template>
  <div class="dev-page"><div class="dev-toolbar"><button class="button ghost" @click="router.push('/resources')"><ArrowLeft :size="15" />Capabilities</button><div class="dev-title"><Sparkles :size="16" /><input v-model="skill.name" /><span>{{ saved }}</span></div><button class="button primary" @click="save"><Save :size="14" />保存 Skill</button></div>
    <div class="dev-layout"><aside class="dev-files"><div class="dev-files-head"><strong>Package explorer</strong><button class="icon-button" title="添加参考文件" @click="addFile('reference')"><FilePlus2 :size="14" /></button></div><div class="dev-file-root"><FolderOpen :size="14" />{{ skill.slug||'new-skill' }}/</div><button v-for="entry in treeEntries" :key="entry.key" class="dev-file tree-entry" :class="[{active:selectedKey===entry.key},entry.type]" :style="{paddingLeft:`${7+(entry.depth-1)*14}px`}" :disabled="entry.type==='folder'" @click="entry.type==='file'&&(selectedKey=entry.key)"><Folder v-if="entry.type==='folder'" :size="14" /><FileCode2 v-else :size="14" />{{ entry.name }}</button><div class="dev-adds"><button class="button small" @click="addFile('reference')">+ Reference</button><button class="button small" @click="addFile('script')">+ Script</button><button class="button small" @click="addFile('asset')">+ Asset</button></div></aside>
      <section class="dev-editor"><div class="editor-head"><input v-if="activeFile?.kind!=='skill'" class="editor-path" :value="activeFile?.path" aria-label="File path" @change="renameActive($event.target.value)" /><span v-else>SKILL.md</span><div><span class="tag">{{ activeFile?.kind==='skill'?'frontmatter + instructions':activeFile?.kind }}</span><button v-if="activeFile?.kind!=='skill'" class="icon-button dark" title="删除文件" @click="removeActive"><Trash2 :size="13" /></button></div></div><textarea :value="activeFile?.content" spellcheck="false" @input="updateActive($event.target.value)"></textarea></section>
      <aside class="dev-preview"><div class="dev-preview-head"><strong>Skill metadata</strong><span class="status-badge published">LOCAL</span></div><div class="form-grid"><div class="field full"><label>Slug</label><input v-model="skill.slug" /></div><div class="field full"><label>Description / trigger</label><textarea v-model="skill.description"></textarea></div><div class="field"><label>Version</label><input v-model="skill.version" /></div><div class="field"><label>Author</label><input v-model="skill.author" /></div><div class="field full"><label>Category</label><input v-model="skill.category" /></div></div><div class="preview-note"><Sparkles :size="14" /><p>保存后写入标准 package；运行时由 `Agent.skills` 按需加载。</p></div></aside>
    </div>
  </div>
</template>
