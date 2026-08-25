<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Bell, LogOut, Plus } from 'lucide-vue-next'
import { usePlatformStore } from '../stores/platform'

const route = useRoute(); const router = useRouter()
const store = usePlatformStore()
const canEdit = computed(() => store.canEdit)
const title = computed(() => route.meta.title || '玄枢编排台')
const eyebrow = computed(() => route.meta.eyebrow || 'WORKSPACE')
function logout(){localStorage.removeItem('xuanshu_token');localStorage.removeItem('xuanshu_workspace');localStorage.removeItem('xuanshu_user');router.push('/login')}
async function switchWorkspace(event){localStorage.setItem('xuanshu_workspace',event.target.value);await store.load();router.push('/')}
</script>

<template>
  <header class="topbar">
    <div><span class="eyebrow">{{ eyebrow }}</span><h1>{{ title }}</h1></div>
    <div class="topbar-actions">
      <select class="workspace-switcher" :value="store.currentWorkspace?.id" @change="switchWorkspace"><option v-for="item in store.workspaces" :key="item.id" :value="item.id">{{ item.name }}</option></select>
      <button class="icon-button" title="通知"><Bell :size="18" /></button>
      <button class="icon-button" title="退出登录" @click="logout"><LogOut :size="18" /></button>
      <button v-if="canEdit" class="button primary" @click="router.push('/new-automation')"><Plus :size="16" />新建智能体</button>
    </div>
  </header>
</template>
