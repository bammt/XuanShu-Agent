<script setup>
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'
import AppHeader from './components/AppHeader.vue'
import AppDialogHost from './components/AppDialogHost.vue'
import { usePlatformStore } from './stores/platform'
import { useRoute } from 'vue-router'

const store = usePlatformStore()
const route = useRoute()
onMounted(() => {
  if (localStorage.getItem('xuanshu_token')) store.load()
})
</script>

<template>
  <div class="app-shell" :class="{standalone:route.meta.standalone}">
    <AppSidebar v-if="!route.meta.standalone" />
    <div class="app-main">
      <AppHeader v-if="!route.meta.standalone" />
      <main class="page-body"><RouterView /></main>
    </div>
    <div v-if="store.notice" class="toast success">{{ store.notice }}</div>
    <div v-if="store.error" class="toast error" @click="store.error = ''">{{ store.error }}</div>
    <AppDialogHost />
  </div>
</template>
