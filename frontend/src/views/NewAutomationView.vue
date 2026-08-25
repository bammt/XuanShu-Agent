<script setup>
import { useRouter } from "vue-router";
import {
  ArrowLeft,
  ArrowRight,
  Bot,
  GitBranch,
  Route,
  UsersRound,
} from "lucide-vue-next";

const router = useRouter();
const choices = [
  {
    kind: "flow",
    title: "Flow",
    description: "用事件、状态和显式分支编排 Agent 与 Crew。",
    details: ["Agent steps", "Crew kickoff", "Routers"],
    icon: GitBranch,
  },
  {
    kind: "crew",
    title: "Crew",
    description: "让一组专业 Agent 按顺序或层级流程协作完成任务。",
    details: ["Crew tasks", "Sequential", "Hierarchical"],
    icon: UsersRound,
  },
];
</script>

<template>
  <div class="page-heading create-heading">
    <div>
      <h2>创建智能体</h2>
      <p>选择与执行目标匹配的 CrewAI 结构。</p>
    </div>
    <button class="button" @click="router.push('/automations')">
      <ArrowLeft :size="14" />返回智能体
    </button>
  </div>

  <section class="create-type-grid" aria-label="Automation type">
    <button
      v-for="choice in choices"
      :key="choice.kind"
      class="create-type-option"
      @click="router.push(`/studio/new/${choice.kind}`)"
    >
      <span class="create-type-icon"
        ><component :is="choice.icon" :size="22"
      /></span>
      <span class="create-type-copy"
        ><strong>{{ choice.title }}</strong
        ><small>{{ choice.description }}</small></span
      >
      <span class="create-type-details">
        <span v-for="(detail, index) in choice.details" :key="detail"
          ><Route v-if="choice.kind === 'flow' && index === 2" :size="11" /><Bot
            v-else
            :size="11"
          />{{ detail }}</span
        >
      </span>
      <ArrowRight class="create-type-arrow" :size="18" />
    </button>
  </section>
</template>
