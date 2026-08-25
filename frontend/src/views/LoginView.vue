<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePlatformStore } from '../stores/platform'
const username=ref('admin'),password=ref(''),error=ref(''),busy=ref(false),router=useRouter(),route=useRoute(),store=usePlatformStore()
onMounted(() => {
  if (route.query.expired === '1') error.value = '登录已失效，请重新登录'
})
async function login(){busy.value=true;error.value='';try{const body=new URLSearchParams({username:username.value,password:password.value});const response=await fetch('/api/auth/token',{method:'POST',body});const data=await response.json();if(!response.ok)throw new Error(data.detail||'登录失败');localStorage.setItem('xuanshu_token',data.access_token);localStorage.setItem('xuanshu_user',JSON.stringify(data.user));await store.load();if(store.error)throw new Error(store.error);router.push('/')}catch(e){localStorage.removeItem('xuanshu_token');localStorage.removeItem('xuanshu_user');error.value=e.message}finally{busy.value=false}}
</script>
<template><div class="auth-page"><form class="auth-panel" @submit.prevent="login"><span class="brand-mark">玄</span><h1>玄枢 XuanShu</h1><p>登录智能体编排控制台</p><label>用户名<input v-model="username" autocomplete="username" /></label><label>密码<input v-model="password" type="password" autocomplete="current-password" /></label><p v-if="error" class="form-error">{{ error }}</p><button class="button primary" :disabled="busy">{{ busy?'登录中…':'登录' }}</button></form></div></template>
