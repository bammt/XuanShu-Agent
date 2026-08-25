<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { LayoutDashboard, Workflow, WandSparkles, Activity, BookOpen, Boxes, Settings2, SlidersHorizontal, HelpCircle, Users, Play } from 'lucide-vue-next'
import { usePlatformStore } from '../stores/platform'

const store = usePlatformStore()
const canEdit = computed(() => store.canEdit)

const navigation = [
  { to: '/', label: '控制台', icon: LayoutDashboard },
  { to: '/automations', label: '智能体编辑', icon: Workflow },
  { to: '/run-agents', label: '运行智能体', icon: Play },
  { to: '/new-automation', label: '玄枢编排台', icon: WandSparkles },
  { to: '/runs', label: 'Runs & Traces', icon: Activity },
  { to: '/resources', label: 'Skills & Tools', icon: Boxes },
  { to: '/knowledge', label: '知识库', icon: BookOpen },
]

const visibleNavigation = computed(() => canEdit.value ? navigation : navigation.filter((item) => item.to === '/run-agents'))
</script>

<template>
  <aside class="sidebar">
    <div class="brand-lockup"><span class="brand-mark">玄</span><div><strong>玄枢 XuanShu</strong><small>AGENT CONTROL PLANE</small></div></div>
    <nav class="main-nav">
      <RouterLink v-for="item in visibleNavigation" :key="item.to" :to="item.to"><component :is="item.icon" :size="18" /><span>{{ item.label }}</span></RouterLink>
    </nav>
    <div class="sidebar-bottom">
      <RouterLink to="/models"><Settings2 :size="18" /><span>模型连接</span></RouterLink>
      <template v-if="canEdit">
        <RouterLink to="/model-default"><SlidersHorizontal :size="18" /><span>默认模型</span></RouterLink>
        <RouterLink to="/workspace"><Users :size="18" /><span>用户与工作空间</span></RouterLink>
        <a href="https://docs.crewai.com" target="_blank" rel="noreferrer"><HelpCircle :size="18" /><span>CrewAI 文档</span></a>
        <div class="runtime-chip"><i></i><div><strong>运行服务在线</strong><small>CREWAI RUNTIME</small></div></div>
      </template>
    </div>
  </aside>
</template>
