<script setup>defineProps({ status: { type: String, default: 'draft' } })</script>
<template><span class="status-badge" :class="status"><i></i>{{ status }}</span></template>
