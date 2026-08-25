<script setup>
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import {
  Bot,
  Copy,
  GitBranch,
  Pencil,
  Play,
  Plus,
  Search,
  Trash2,
} from "lucide-vue-next";
import { api } from "../services/api";
import { usePlatformStore } from "../stores/platform";
import StatusBadge from "../components/StatusBadge.vue";
import EmptyState from "../components/EmptyState.vue";
import { confirmDialog } from "../services/dialog";
import { formatBeijingDate } from "../services/dateFormatting";

const store = usePlatformStore();
const router = useRouter();
const search = ref("");
const filter = ref("all");
const busy = ref("");
const filtered = computed(() =>
  store.workflows.filter(
    (item) =>
      (filter.value === "all" || item.status === filter.value) &&
      `${item.name} ${item.description}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
);
async function remove(item) {
  if (!(await confirmDialog({
    title: "删除智能体",
    message: `确认删除“${item.name}”？相关运行入口将不可用。`,
    confirmLabel: "删除智能体",
    danger: true,
  }))) return;
  busy.value = item.id;
  try {
    await api.deleteWorkflow(item.id);
    await store.load();
    store.notify("智能体已删除");
  } finally {
    busy.value = "";
  }
}
async function duplicate(item) {
  const copy = JSON.parse(JSON.stringify(item));
  copy.id = Math.random().toString(36).slice(2, 14);
  copy.name = `${item.name} 副本`;
  copy.status = "draft";
  await api.saveWorkflow(copy);
  await store.load();
  store.notify("已创建副本");
}
function openItem(item) {
  router.push(`/studio/${item.id}`);
}
function runItem(item) {
  if (item.status !== "published") {
    store.error = "请先发布智能体后再运行";
    return;
  }
  router.push(`/automations/${item.id}/run`);
}
</script>

<template>
  <div class="page-heading">
    <div>
      <h2>智能体</h2>
      <p>管理 Crew 与 Flow 智能体的草稿、发布状态和独立运行入口。</p>
    </div>
    <button class="button primary" @click="router.push('/new-automation')">
      <Plus :size="15" />新建智能体
    </button>
  </div>
  <div class="toolbar">
    <div class="toolbar-left">
      <label class="search-input"
        ><Search :size="15" /><input v-model="search" placeholder="搜索智能体"
      /></label>
      <div class="segmented">
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
          全部</button
        ><button
          :class="{ active: filter === 'draft' }"
          @click="filter = 'draft'"
        >
          草稿</button
        ><button
          :class="{ active: filter === 'published' }"
          @click="filter = 'published'"
        >
          已发布
        </button>
      </div>
    </div>
    <span style="font-size: 10px; color: var(--subtle)"
      >{{ filtered.length }} 个智能体</span
    >
  </div>
  <section v-if="filtered.length" class="automation-grid">
    <article
      v-for="item in filtered"
      :key="item.id"
      class="automation-card"
      tabindex="0"
      @click="openItem(item)"
      @keydown.enter="openItem(item)"
    >
      <header>
        <span class="automation-icon"
          ><GitBranch v-if="item.kind === 'flow'" :size="17" /><Bot
            v-else
            :size="17" /></span
        ><StatusBadge :status="item.status" />
      </header>
      <div class="automation-card-copy">
        <span class="eyebrow">{{ item.kind.toUpperCase() }}</span>
        <h3>{{ item.name }}</h3>
        <p>{{ item.description || "暂无说明" }}</p>
      </div>
      <div class="automation-stats">
        <span
          ><strong>{{ item.tasks.length }}</strong> 节点</span
        ><span
          ><strong>{{ item.agents.length }}</strong> Agent</span
        ><span
          ><strong>{{ item.inputs?.length || 0 }}</strong> 输入</span
        >
      </div>
      <footer>
        <small
          >{{ formatBeijingDate(item.updated_at) }} ·
          {{
            store.models.find((x) => x.id === item.model_profile_id)?.name ||
            item.model
          }}</small
        >
        <div>
          <button
            class="icon-button"
            title="编辑编排"
            @click.stop="openItem(item)"
          >
            <Pencil :size="14" /></button
          ><button
            v-if="item.status === 'published'"
            class="icon-button"
            title="打开运行页"
            @click.stop="runItem(item)"
          ><Play :size="14" /></button
          ><button
            class="icon-button"
            title="创建副本"
            @click.stop="duplicate(item)"
          >
            <Copy :size="14" /></button
          ><button class="icon-button" title="删除" @click.stop="remove(item)">
            <Trash2 :size="14" />
          </button>
        </div>
      </footer>
    </article>
  </section>
  <EmptyState
    v-else
    title="没有匹配的智能体"
    detail="调整筛选条件，或创建新的 CrewAI Crew/Flow 智能体。"
  />
</template>
