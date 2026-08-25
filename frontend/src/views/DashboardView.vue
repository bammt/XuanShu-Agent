<script setup>
import { computed } from "vue";
import { useRouter } from "vue-router";
import {
  Activity,
  Bot,
  CircleCheck,
  Cpu,
  GitBranch,
  Plus,
  Workflow,
} from "lucide-vue-next";
import { usePlatformStore } from "../stores/platform";
import StatusBadge from "../components/StatusBadge.vue";
import EmptyState from "../components/EmptyState.vue";
import { formatBeijingDate, formatBeijingDateTime, timestampValue } from "../services/dateFormatting";

const store = usePlatformStore();
const router = useRouter();
const recentWorkflows = computed(() =>
  store.workflows
    .slice()
    .sort((a, b) => timestampValue(b.updated_at) - timestampValue(a.updated_at))
    .slice(0, 6),
);
const successRate = computed(() =>
  store.stats.runs
    ? Math.round((store.stats.successful / store.stats.runs) * 100)
    : 0,
);
const metrics = computed(() => [
  {
    label: "工作流",
    value: store.stats.workflows,
    detail: `${store.stats.published} 已发布`,
    icon: Workflow,
  },
  {
    label: "累计运行",
    value: store.stats.runs,
    detail: "所有本地执行",
    icon: Activity,
  },
  {
    label: "成功率",
    value: `${successRate.value}%`,
    detail: `${store.stats.successful} 次成功`,
    icon: CircleCheck,
  },
  {
    label: "模型连接",
    value: store.models.length,
    detail: store.defaultModel?.name || "尚未配置",
    icon: Cpu,
  },
]);
</script>

<template>
  <div class="page-heading">
    <div>
      <h2>工作空间概览</h2>
      <p>构建、运行并观察 CrewAI Crew 与 Flow 智能体。</p>
    </div>
    <button class="button primary" @click="router.push('/new-automation')">
      <Plus :size="15" />创建智能体
    </button>
  </div>
  <section class="metrics-grid">
    <article v-for="metric in metrics" :key="metric.label" class="metric-card">
      <div class="metric-top">
        <span>{{ metric.label }}</span
        ><span class="metric-icon"
          ><component :is="metric.icon" :size="16"
        /></span>
      </div>
      <strong>{{ metric.value }}</strong
      ><small>{{ metric.detail }}</small>
    </article>
  </section>

  <div class="section-heading">
    <h2>最近智能体</h2>
    <button class="button ghost small" @click="router.push('/automations')">
      查看全部
    </button>
  </div>
  <section v-if="recentWorkflows.length" class="workflow-grid">
    <article
      v-for="item in recentWorkflows"
      :key="item.id"
      class="workflow-card"
      @click="router.push(`/studio/${item.id}`)"
    >
      <div class="workflow-card-top">
        <span class="workflow-kind"
          ><GitBranch v-if="item.kind === 'flow'" :size="16" /><Bot
            v-else
            :size="16" /></span
        ><StatusBadge :status="item.status" />
      </div>
      <h3>{{ item.name }}</h3>
      <p>{{ item.description || "暂无描述" }}</p>
      <div class="workflow-card-footer">
        <span
          >{{ item.tasks.length }} steps · {{ item.agents.length }} agents</span
        ><span>{{ formatBeijingDate(item.updated_at) }}</span>
      </div>
    </article>
  </section>
  <EmptyState
    v-else
    title="还没有智能体"
    detail="选择 Flow 或 Crew，并通过自然语言生成第一个智能体。"
    ><button class="button accent" @click="router.push('/new-automation')">
      创建智能体
    </button></EmptyState
  >

  <div class="section-heading">
    <h2>最近运行</h2>
    <button class="button ghost small" @click="router.push('/runs')">
      打开 Traces
    </button>
  </div>
  <section class="panel dashboard-runs-panel">
    <div v-if="store.runs.length" class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>WORKFLOW</th>
            <th>STATUS</th>
            <th>MODEL</th>
            <th>STARTED</th>
            <th>DURATION</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="run in store.runs.slice(0, 6)"
            :key="run.id"
            @click="router.push(`/runs/${run.conversation_id || run.id}`)"
          >
            <td>
              <div class="cell-title">
                <span class="avatar"><Activity :size="14" /></span>
                <div>
                  <strong>{{ run.workflow_name }}</strong
                  ><small>{{ run.conversation_id || run.id }}</small>
                </div>
              </div>
            </td>
            <td><StatusBadge :status="run.status" /></td>
            <td>{{ run.model || "default" }}</td>
            <td>{{ formatBeijingDateTime(run.created_at) }}</td>
            <td>
              {{
                run.metrics?.duration_seconds
                  ? `${run.metrics.duration_seconds}s`
                  : "—"
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <EmptyState
      v-else
      title="暂无运行记录"
      detail="测试工作流后，状态、输出和性能摘要会显示在这里。"
    />
  </section>
</template>
