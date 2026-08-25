<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Check, Copy, ExternalLink, KeyRound, Plus, Trash2 } from 'lucide-vue-next'
import { api } from '../services/api'
import { usePlatformStore } from '../stores/platform'
import { formatBeijingDateTime } from '../services/dateFormatting'

const route = useRoute()
const router = useRouter()
const store = usePlatformStore()
const keys = ref([])
const newKey = ref(null)
const keyName = ref('生产调用')
const loading = ref(true)
const copied = ref('')
const workflow = computed(() => store.workflows.find(item => item.id === route.params.id))
const token = computed(() => workflow.value?.public_token || '')
const origin = window.location.origin
const basePath = computed(() => `${origin}/api/v1/apps/${token.value}`)
const publicUrl = computed(() => `${origin}/public/${token.value}`)
const sampleInputs = computed(() => Object.fromEntries((workflow.value?.inputs || [])
  .filter(item => !['file', 'image'].includes(item.input_type))
  .map(item => [item.name, item.input_type === 'number' ? 1 : item.input_type === 'boolean' ? true : `填写${item.label}`])))
const sampleFiles = computed(() => Object.fromEntries((workflow.value?.inputs || [])
  .filter(item => ['file', 'image'].includes(item.input_type))
  .map(item => [item.name, ['UPLOAD_ID']])))
const runBody = computed(() => JSON.stringify({ inputs: sampleInputs.value, files: sampleFiles.value }, null, 2))

onMounted(async () => {
  await store.load()
  if (workflow.value?.status === 'published') keys.value = await api.applicationApiKeys(workflow.value.id)
  loading.value = false
})
async function createKey() {
  newKey.value = await api.createApplicationApiKey(workflow.value.id, keyName.value.trim() || 'API Key')
  keys.value = await api.applicationApiKeys(workflow.value.id)
}
async function removeKey(id) {
  await api.deleteApplicationApiKey(workflow.value.id, id)
  keys.value = keys.value.filter(item => item.id !== id)
}
async function copy(value, label) {
  await navigator.clipboard.writeText(value); copied.value = label
  window.setTimeout(() => { copied.value = '' }, 1600)
}
</script>

<template>
  <div v-if="loading" class="develop-loading">正在读取应用接口...</div>
  <section v-else-if="workflow&&workflow.status==='published'" class="develop-page">
    <header class="develop-heading">
      <button class="icon-button" title="返回应用" @click="router.push(`/automations/${workflow.id}/run`)"><ArrowLeft :size="16" /></button>
      <div><span class="eyebrow">DEVELOP</span><h2>{{ workflow.name }} API</h2><p>通过应用专属 API Key 调用已发布的 {{ workflow.kind.toUpperCase() }}。</p></div>
      <a class="button" :href="publicUrl" target="_blank"><ExternalLink :size="14" />打开公开页面</a>
    </header>

    <div class="develop-layout">
      <main class="develop-docs">
        <section><span class="eyebrow">BASE URL</span><div class="copy-line"><code>{{ basePath }}</code><button class="icon-button" title="复制" @click="copy(basePath,'base')"><Check v-if="copied==='base'" :size="14"/><Copy v-else :size="14"/></button></div></section>
        <section><h3>1. 上传文件</h3><p>文件类变量先上传，必须显式传 <code>user_id</code>；返回的 <code>id</code> 可在该用户拥有的任意后续会话中引用，文件默认保留 30 天。</p><pre>curl -X POST '{{ basePath }}/files' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -F 'user_id=USER_ID' -F 'file=@./document.pdf'</pre></section>
        <section><h3>2. 发起运行</h3><p>API 必须显式传 <code>user_id</code>；第一次调用省略 <code>conversation_id</code>，响应会返回会话 ID。后续多轮请求带回同一用户 ID 和会话 ID。变量必须使用编排时确认的英文变量名。</p><pre>curl -X POST '{{ basePath }}/runs' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{{ runBody }}'</pre></section>
        <section><h3>3. 查询与流式事件</h3><p>创建运行、查询、事件流和文件下载都必须显式提供 <code>user_id</code>；API 不会从 Cookie 推断调用方身份。</p><pre>curl -H 'Authorization: Bearer YOUR_API_KEY' \
  '{{ basePath }}/runs/RUN_ID?user_id=USER_ID'

curl -N -H 'Authorization: Bearer YOUR_API_KEY' \
  '{{ basePath }}/runs/RUN_ID/events?user_id=USER_ID'</pre></section>
        <section><h3>4. 人工审批</h3><p>状态为 <code>waiting_approval</code> 时提交审批，批准后从暂停节点之后继续，不重复已完成节点。</p><pre>curl -X POST '{{ basePath }}/runs/RUN_ID/approval?user_id=USER_ID' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"approved","feedback":"审核通过"}'</pre></section>
        <section><h3>5. 会话管理</h3><p>发送“新建对话”或调用下面的接口会创建全新的上下文；清空接口会删除该会话的历史运行。新建后的响应会返回新的 <code>conversation_id</code>。</p><pre>curl -X POST '{{ basePath }}/conversations?user_id=USER_ID' \
  -H 'Authorization: Bearer YOUR_API_KEY'

curl -X DELETE '{{ basePath }}/conversations/CONVERSATION_ID?user_id=USER_ID' \
  -H 'Authorization: Bearer YOUR_API_KEY'</pre></section>
        <section><h3>运行输入</h3><div class="contract-table"><div class="contract-head"><span>显示名称</span><span>变量名</span><span>类型</span><span>规则</span></div><div v-for="input in workflow.inputs" :key="input.name"><strong>{{ input.label }}</strong><code>{{ input.name }}</code><span>{{ input.input_type }}</span><span>{{ input.required?'必填':'可选' }}{{ input.multiple?' · 多文件':'' }}</span></div></div></section>
      </main>

      <aside class="develop-keys">
        <header><div><span class="eyebrow">CREDENTIALS</span><h3>API Keys</h3></div><KeyRound :size="17" /></header>
        <p>Key 只在创建后显示一次。公开聊天链接不需要 Key，外部 API 必须携带。</p>
        <div class="key-create"><input v-model="keyName" placeholder="Key 名称" /><button class="button primary" @click="createKey"><Plus :size="13" />创建</button></div>
        <div v-if="newKey" class="new-api-key"><strong>请立即保存</strong><code>{{ newKey.key }}</code><button class="button" @click="copy(newKey.key,'key')"><Check v-if="copied==='key'" :size="13"/><Copy v-else :size="13"/>{{ copied==='key'?'已复制':'复制 Key' }}</button></div>
        <div class="key-list"><article v-for="item in keys" :key="item.id"><div><strong>{{ item.name }}</strong><small>{{ formatBeijingDateTime(item.created_at) }}</small></div><button class="icon-button" title="撤销" @click="removeKey(item.id)"><Trash2 :size="14" /></button></article><p v-if="!keys.length" class="empty-copy">尚未创建 API Key。</p></div>
      </aside>
    </div>
  </section>
  <div v-else class="develop-loading">应用不存在或尚未发布。</div>
</template>
