<script setup>import { Inbox } from 'lucide-vue-next'; defineProps({ title: String, detail: String })</script>
<template><div class="empty-state"><Inbox :size="26" /><strong>{{ title }}</strong><p>{{ detail }}</p><slot /></div></template>
