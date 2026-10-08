<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { LayoutDashboard, Workflow, Activity, BookOpen, Boxes, Settings2, SlidersHorizontal, Users, Play } from 'lucide-vue-next'
import { usePlatformStore } from '../stores/platform'

const store = usePlatformStore()
const canEdit = computed(() => store.canEdit)

const navigation = [
  { to: '/', label: '控制台', icon: LayoutDashboard },
  { to: '/automations', label: '智能体编辑', icon: Workflow },
  { to: '/new-automation', label: 'Studio', icon: SlidersHorizontal },
  { to: '/run-agents', label: '运行智能体', icon: Play },
  { to: '/runs', label: '运行记录', icon: Activity },
  { to: '/resources', label: '资源', icon: Boxes },
  { to: '/knowledge', label: '知识库', icon: BookOpen },
]

const visibleNavigation = computed(() => canEdit.value ? navigation : navigation.filter((item) => item.to === '/run-agents'))
</script>

<template>
  <aside class="sidebar">
    <div class="brand-lockup"><img class="brand-mark" src="/xuanshu-mark.svg?v=8" alt="玄枢" /><div><strong>玄枢 XuanShu</strong><small>智能体工作台</small></div></div>
    <nav class="main-nav">
      <RouterLink v-for="item in visibleNavigation" :key="item.to" :to="item.to"><component :is="item.icon" :size="18" /><span>{{ item.label }}</span></RouterLink>
    </nav>
    <div class="sidebar-bottom">
      <RouterLink to="/models"><Settings2 :size="18" /><span>模型连接</span></RouterLink>
      <template v-if="canEdit">
        <RouterLink to="/model-default"><SlidersHorizontal :size="18" /><span>默认模型</span></RouterLink>
        <RouterLink to="/workspace"><Users :size="18" /><span>用户与工作空间</span></RouterLink>
        <div class="runtime-chip"><i></i><div><strong>运行服务在线</strong><small>执行服务</small></div></div>
      </template>
    </div>
  </aside>
</template>
