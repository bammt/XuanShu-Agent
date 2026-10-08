<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  Clock3,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  Search,
  Trash2,
  UsersRound,
} from 'lucide-vue-next'
import { api } from '../services/api'
import { usePlatformStore } from '../stores/platform'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import RichMessage from '../components/RichMessage.vue'
import { expandedTraceEvents, groupTraceEvents, traceEventPayload } from '../services/traceEvents'
import { confirmDialog } from '../services/dialog'
import {
  formatBeijingDateTime,
  timestampValue,
} from '../services/dateFormatting'

const store = usePlatformStore()
const route = useRoute()
const router = useRouter()
const traces = ref([])
const selected = ref(null)
const search = ref('')
const workflowFilter = ref(String(route.query.workflow || ''))
const page = ref(1)
const pageSize = 8
const loading = ref(false)
const feedback = ref('')
const feedbackBusy = ref(false)
const rawEventsByRun = ref({})
const expandedGroups = ref(new Set())
const loadingRuns = ref(new Set())
const rawEventRequests = new Map()
let timer
let refreshInFlight = false

const eventGroups = computed(() => groupTraceEvents(selected.value?.events || []))

async function toggleEventGroup(group) {
  const next = new Set(expandedGroups.value)
  if (next.has(group.key)) {
    next.delete(group.key)
    expandedGroups.value = next
    return
  }
  next.add(group.key)
  expandedGroups.value = next
  if (!group.runId || rawEventsByRun.value[group.runId]) return
  if (!rawEventRequests.has(group.runId)) {
    loadingRuns.value = new Set([...loadingRuns.value, group.runId])
    const request = api.traceRunEvents(selected.value.id, group.runId)
      .then((result) => { rawEventsByRun.value = { ...rawEventsByRun.value, [group.runId]: result.events || [] } })
      .catch((error) => { store.error = error.message })
      .finally(() => {
        rawEventRequests.delete(group.runId)
        const remaining = new Set(loadingRuns.value)
        remaining.delete(group.runId)
        loadingRuns.value = remaining
      })
    rawEventRequests.set(group.runId, request)
  }
  await rawEventRequests.get(group.runId)
}

const modeLabel = (mode) => ({
  preview: '编排预览',
  application: '应用运行',
  api: 'API 运行',
})[mode] || '应用运行'

const statusLabel = (status) => ({
  completed: '已完成',
  running: '运行中',
  queued: '排队中',
  failed: '失败',
  waiting_input: '等待输入',
  waiting_approval: '等待审核',
})[status] || status || '未知'

const metric = (...keys) => {
  for (const key of keys) {
    if (selected.value?.metrics?.[key] != null) return selected.value.metrics[key]
  }
  return 0
}

const workflowCards = computed(() => {
  const grouped = new Map()
  for (const trace of traces.value) {
    const id = String(trace.workflow_id || '')
    if (!id) continue
    if (!grouped.has(id)) grouped.set(id, [])
    grouped.get(id).push(trace)
  }
  return [...grouped.entries()]
    .map(([id, items]) => {
      const ordered = [...items].sort(
        (a, b) => timestampValue(b.updated_at || b.created_at) - timestampValue(a.updated_at || a.created_at),
      )
      const latest = ordered[0]
      const workflow = store.workflows.find((item) => String(item.id) === id)
      const counts = ordered.reduce((result, item) => {
        result[item.status] = (result[item.status] || 0) + 1
        return result
      }, {})
      return {
        id,
        name: workflow?.name || latest.workflow_name || '未命名智能体',
        description: workflow?.description || '',
        kind: workflow?.kind || 'crew',
        runs: ordered,
        latest,
        counts,
      }
    })
    .filter((item) => `${item.name} ${item.description}`.toLowerCase().includes(search.value.trim().toLowerCase()))
    .sort((a, b) => timestampValue(b.latest.updated_at || b.latest.created_at) - timestampValue(a.latest.updated_at || a.latest.created_at))
})

const activeCard = computed(() => workflowCards.value.find((item) => item.id === workflowFilter.value) || null)
const activeRuns = computed(() => {
  const card = activeCard.value
  if (!card) return []
  return [...card.runs].sort(
    (a, b) => timestampValue(b.updated_at || b.created_at) - timestampValue(a.updated_at || a.created_at),
  )
})
const totalPages = computed(() => Math.max(1, Math.ceil(activeRuns.value.length / pageSize)))
const pagedRuns = computed(() => activeRuns.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const showingRange = computed(() => {
  if (!activeRuns.value.length) return '0 条记录'
  const start = (page.value - 1) * pageSize + 1
  const end = Math.min(page.value * pageSize, activeRuns.value.length)
  return `${start}-${end} / ${activeRuns.value.length}`
})

function routeToTrace(id, replace = false) {
  const location = { name: 'runs', params: { id }, query: { workflow: workflowFilter.value } }
  return replace ? router.replace(location) : router.push(location)
}

async function choose(id, replace = false) {
  if (!id) return
  try {
    if (String(selected.value?.id || '') !== String(id)) expandedGroups.value = new Set()
    selected.value = await api.trace(id)
    workflowFilter.value = String(selected.value.workflow_id || workflowFilter.value)
    const index = activeRuns.value.findIndex((item) => String(item.id) === String(id))
    page.value = Math.max(1, Math.ceil((index + 1) / pageSize))
    if (String(route.params.id || '') !== String(id)) await routeToTrace(id, replace)
  } catch (error) {
    selected.value = null
    store.error = error.message
  }
}

async function openWorkflow(card) {
  workflowFilter.value = card.id
  page.value = 1
  selected.value = null
  await router.push({ name: 'runs', query: { workflow: card.id } })
  if (card.runs.length) await choose(card.runs[0].id, true)
}

async function refresh() {
  if (refreshInFlight) return
  refreshInFlight = true
  loading.value = true
  try {
    traces.value = await api.traces()
    if (route.params.id) {
      await choose(route.params.id, true)
    } else if (workflowFilter.value) {
      page.value = Math.min(page.value, totalPages.value)
      const first = pagedRuns.value[0]
      if (first) await choose(first.id, true)
    }
  } catch (error) {
    store.error = error.message
  } finally {
    loading.value = false
    refreshInFlight = false
  }
}

async function refreshSummary() {
  if (refreshInFlight) return
  refreshInFlight = true
  try {
    traces.value = await api.traces()
    const current = route.params.id
      ? traces.value.find((item) => String(item.id) === String(route.params.id))
      : null
    if (current && selected.value) {
      selected.value = {
        ...selected.value,
        status: current.status,
        updated_at: current.updated_at,
        run_count: current.run_count,
        workflow_run_count: current.workflow_run_count,
        metrics: current.metrics,
      }
    }
  } catch (error) {
    store.error = error.message
  } finally {
    refreshInFlight = false
  }
}

function backToAgents() {
  selected.value = null
  workflowFilter.value = ''
  page.value = 1
  router.push({ name: 'runs' })
}

function setPage(next) {
  page.value = Math.min(Math.max(1, next), totalPages.value)
  const first = pagedRuns.value[0]
  if (first && !selected.value) choose(first.id, false)
}

async function submitFeedback(outcome) {
  if (!selected.value) return
  feedbackBusy.value = true
  try {
    selected.value = await api.submitRunFeedback(selected.value.id, outcome, feedback.value)
    feedback.value = ''
    await refresh()
  } catch (error) {
    store.error = error.message
  } finally {
    feedbackBusy.value = false
  }
}

async function removeTrace(trace) {
  if (!(await confirmDialog({
    title: '删除运行记录',
    message: `删除“${trace.workflow_name || activeCard.value?.name}”的这条会话轨迹？删除后无法恢复。`,
    confirmLabel: '删除记录',
    danger: true,
  }))) return
  try {
    await api.deleteTrace(trace.id)
    selected.value = null
    await refresh()
    if (!activeRuns.value.length) backToAgents()
  } catch (error) {
    store.error = error.message
  }
}

onMounted(async () => {
  await refresh()
  timer = setInterval(() => {
    if (selected.value && ['queued', 'running'].includes(selected.value.status)) refreshSummary()
  }, 1800)
})

onUnmounted(() => clearInterval(timer))

watch(() => route.query.workflow, (value) => {
  workflowFilter.value = String(value || '')
  page.value = 1
})

watch(() => route.params.id, (id) => {
  if (id && String(id) !== String(selected.value?.id || '')) choose(id, true)
  if (!id) selected.value = null
})
</script>

<template>
  <div class="page-heading trace-heading">
    <div>
      <span class="eyebrow">OBSERVABILITY</span>
      <h2>运行追踪</h2>
      <p>{{ selected ? '查看当前智能体的会话、节点事件和最终交付。' : '先选择智能体，再查看它的运行轨迹。' }}</p>
    </div>
    <button class="button" :disabled="loading" @click="refresh">
      <RefreshCw :size="14" :class="{ 'spin-once': loading }" />刷新
    </button>
  </div>

  <section v-if="!workflowFilter && !selected" class="trace-home">
    <div class="trace-home-toolbar">
      <label class="search-input trace-search">
        <Search :size="14" /><input v-model="search" placeholder="搜索智能体" />
      </label>
      <span class="trace-count">{{ workflowCards.length }} 个智能体有运行记录</span>
    </div>
    <section v-if="workflowCards.length" class="trace-agent-grid">
      <button v-for="card in workflowCards" :key="card.id" class="trace-agent-card" @click="openWorkflow(card)">
        <div class="trace-agent-card-head">
          <span class="trace-agent-icon"><GitBranch v-if="card.kind === 'flow'" :size="18" /><UsersRound v-else :size="18" /></span>
          <StatusBadge :status="card.latest.status" />
        </div>
        <div class="trace-agent-card-body">
          <div><span class="eyebrow">{{ card.kind.toUpperCase() }} AGENT</span><h3>{{ card.name }}</h3></div>
          <p>{{ card.description || '查看该智能体的全部会话运行和节点事件。' }}</p>
        </div>
        <div class="trace-agent-card-footer">
          <span><Activity :size="13" />{{ card.runs.length }} 条运行记录</span>
          <span><Clock3 :size="13" />{{ formatBeijingDateTime(card.latest.updated_at || card.latest.created_at) }}</span>
        </div>
      </button>
    </section>
    <EmptyState v-else title="暂无运行记录" detail="运行一个已发布智能体后，这里会按智能体展示追踪记录。" />
  </section>

  <section v-else class="trace-workspace">
    <aside class="trace-history-panel">
      <header class="trace-history-head">
        <button class="icon-button" title="返回智能体列表" aria-label="返回智能体列表" @click="backToAgents"><ArrowLeft :size="15" /></button>
        <div><span class="eyebrow">{{ activeCard?.kind?.toUpperCase() || 'AGENT' }}</span><strong>{{ activeCard?.name || selected?.workflow_name || '智能体运行' }}</strong></div>
      </header>
      <div class="trace-history-meta"><span>{{ activeRuns.length }} 条运行记录</span><span>{{ showingRange }}</span></div>
      <div v-if="pagedRuns.length" class="trace-history-list">
        <article v-for="run in pagedRuns" :key="run.id" class="trace-history-item" :class="{ active: selected?.id === run.id }" @click="choose(run.id)">
          <div class="trace-history-item-top"><span class="tag">{{ modeLabel(run.runtime_mode) }}</span><StatusBadge :status="run.status" /></div>
          <strong>{{ statusLabel(run.status) }}</strong>
          <time>{{ formatBeijingDateTime(run.updated_at || run.created_at) }}</time>
          <button class="icon-button trace-delete" title="删除运行记录" aria-label="删除运行记录" @click.stop="removeTrace(run)"><Trash2 :size="13" /></button>
        </article>
      </div>
      <EmptyState v-else title="暂无运行记录" detail="该智能体还没有可查看的运行轨迹。" />
      <footer v-if="totalPages > 1" class="trace-pagination">
        <button class="icon-button" :disabled="page <= 1" title="上一页" aria-label="上一页" @click="setPage(page - 1)"><ChevronLeft :size="15" /></button>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
        <button class="icon-button" :disabled="page >= totalPages" title="下一页" aria-label="下一页" @click="setPage(page + 1)"><ChevronRight :size="15" /></button>
      </footer>
    </aside>

    <main v-if="selected" class="trace-detail">
      <header class="trace-detail-head">
        <div><span class="eyebrow">TRACE {{ selected.id }}</span><h2>{{ selected.workflow_name }}</h2><p>{{ modeLabel(selected.runtime_mode) }} · {{ formatBeijingDateTime(selected.created_at) }}</p></div>
        <StatusBadge :status="selected.status" />
      </header>
      <div class="trace-summary">
        <div class="trace-metric"><small>运行方式</small><strong>{{ (selected.metrics?.runtime_modes || [selected.runtime_mode]).map(modeLabel).join(' / ') }}</strong></div>
        <div class="trace-metric"><small>对话轮次</small><strong>{{ metric('turns') }}</strong></div>
        <div class="trace-metric"><small>工作流运行</small><strong>{{ metric('workflow_runs') }}</strong></div>
        <div class="trace-metric"><small>节点事件</small><strong>{{ selected.events?.length || 0 }}</strong></div>
      </div>
      <section v-if="selected.status === 'waiting_approval'" class="feedback-gate"><header><div><span class="eyebrow">HUMAN REVIEW</span><h3>{{ selected.pending_feedback.step_name }}</h3></div><span class="tag">等待审核</span></header><p>{{ selected.pending_feedback.message }}</p><pre>{{ selected.pending_feedback.output }}</pre><textarea v-model="feedback" placeholder="添加反馈（可选）"></textarea><div><button v-for="outcome in selected.pending_feedback.outcomes" :key="outcome" class="button" :class="{ primary: outcome === 'approved' }" :disabled="feedbackBusy" @click="submitFeedback(outcome)">{{ outcome }}</button></div></section>
      <section v-else-if="selected.status === 'waiting_input'" class="feedback-gate"><header><div><span class="eyebrow">WAITING FOR USER</span><h3>等待用户补充信息</h3></div><span class="tag">已暂停</span></header><p>{{ selected.waiting_input?.question || selected.output }}</p><p class="muted">在同一会话继续发送消息后，流程会从当前节点恢复。</p></section>
      <div class="trace-section-heading"><div><span class="eyebrow">EVENTS</span><h3>执行时间线</h3></div><span class="tag">{{ selected.events?.length || 0 }} 个事件</span></div>
      <div class="trace-event-groups">
        <section v-for="group in eventGroups" :key="group.key" class="trace-event-group" :class="{ expanded: expandedGroups.has(group.key) }">
          <button class="trace-event-trigger" type="button" :aria-expanded="expandedGroups.has(group.key)" @click="toggleEventGroup(group)">
            <span class="trace-event-marker" :class="group.status"></span>
            <span class="trace-event-summary"><strong>{{ group.title }}</strong><small>{{ group.events.length }} 个事件 · {{ formatBeijingDateTime(group.events[0]?.at) }}</small></span>
            <span class="trace-event-status">{{ statusLabel(group.status) }}</span>
            <ChevronDown :size="16" class="trace-event-chevron" />
          </button>
          <div v-if="expandedGroups.has(group.key)" class="trace-event-details">
            <div v-if="loadingRuns.has(group.runId) && !rawEventsByRun[group.runId]" class="trace-event-loading"><LoaderCircle class="spin" :size="16" />正在读取完整事件…</div>
            <template v-else-if="rawEventsByRun[group.runId]">
              <article v-for="(event, index) in expandedTraceEvents(group, rawEventsByRun[group.runId])" :key="`${event.event_index}-${index}`" class="trace-event-entry">
                <header><span>{{ event.type }}</span><time>{{ formatBeijingDateTime(event.at) }}</time></header>
                <pre v-if="traceEventPayload(event)">{{ traceEventPayload(event) }}</pre>
                <p v-else>{{ event.detail || '事件已记录' }}</p>
              </article>
            </template>
            <div v-else class="trace-event-loading">完整事件读取失败，请再次点击展开。</div>
          </div>
        </section>
      </div>
      <div class="trace-section-heading"><div><span class="eyebrow">OUTPUT</span><h3>{{ selected.status === 'failed' ? '错误信息' : '最终输出' }}</h3></div><span class="tag">{{ statusLabel(selected.status) }}</span></div>
      <RichMessage class="output-box" :text="selected.error || selected.output || (selected.status === 'running' ? '正在运行…' : '等待输出…')" :files="selected.files || []" />
    </main>
    <EmptyState v-else title="选择一次运行" detail="从左侧运行记录打开详细轨迹。" />
  </section>
</template>
