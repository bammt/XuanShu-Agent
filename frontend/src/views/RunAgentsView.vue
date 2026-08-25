<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Bot, GitBranch, Play, Search } from "lucide-vue-next";
import { usePlatformStore } from "../stores/platform";
import EmptyState from "../components/EmptyState.vue";
import StatusBadge from "../components/StatusBadge.vue";

const store = usePlatformStore();
const router = useRouter();
const search = ref("");
const published = computed(() => store.workflows.filter((item) =>
  item.status === "published" && `${item.name} ${item.description || ""}`
    .toLowerCase().includes(search.value.toLowerCase()),
));
onMounted(() => store.load());
</script>

<template>
  <div class="page-heading">
    <div>
      <h2>运行智能体</h2>
      <p>这里只运行已发布版本，编排修改和发布操作请在编排台完成。</p>
    </div>
  </div>
  <div class="toolbar">
    <label class="search-input"><Search :size="15" /><input v-model="search" placeholder="搜索已发布智能体" /></label>
    <span style="font-size: 10px; color: var(--subtle)">{{ published.length }} 个可运行智能体</span>
  </div>
  <section v-if="published.length" class="automation-grid">
    <article v-for="item in published" :key="item.id" class="automation-card run-agent-card">
      <header>
        <span class="automation-icon"><GitBranch v-if="item.kind === 'flow'" :size="17" /><Bot v-else :size="17" /></span>
        <StatusBadge :status="item.status" />
      </header>
      <div class="automation-card-copy">
        <span class="eyebrow">{{ item.kind.toUpperCase() }}</span>
        <h3>{{ item.name }}</h3>
        <p>{{ item.description || "暂无说明" }}</p>
      </div>
      <div class="automation-stats">
        <span><strong>{{ item.tasks?.length || 0 }}</strong> 节点</span>
        <span><strong>{{ item.agents?.length || 0 }}</strong> Agent</span>
        <span><strong>{{ item.inputs?.length || 0 }}</strong> 输入</span>
      </div>
      <footer>
        <small>运行已发布版本</small>
        <button class="button primary" @click="router.push(`/automations/${item.id}/run`)"><Play :size="14" />开始运行</button>
      </footer>
    </article>
  </section>
  <EmptyState v-else title="暂无可运行智能体" detail="先在编排台生成并发布一个智能体。" />
</template>
