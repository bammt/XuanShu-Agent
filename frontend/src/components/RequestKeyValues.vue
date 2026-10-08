<script setup>
import VariableTextarea from './VariableTextarea.vue'
const props=defineProps({modelValue:{type:Object,default:()=>({})},variables:{type:Array,default:()=>[]},label:{type:String,default:'参数'}})
const emit=defineEmits(['update:modelValue'])
function add(){const result={...props.modelValue};let i=1;while(`key_${i}` in result)i++;result[`key_${i}`]='';emit('update:modelValue',result)}
function update(name,value){emit('update:modelValue',{...props.modelValue,[name]:value})}
function rename(name,event){const next=event.target.value.trim();if(!next||(next!==name&&next in props.modelValue)){event.target.value=name;return}emit('update:modelValue',Object.fromEntries(Object.entries(props.modelValue).map(([k,v])=>[k===name?next:k,v])))}
function remove(name){const result={...props.modelValue};delete result[name];emit('update:modelValue',result)}
</script>
<template>
<section class="request-values">
<header><strong>{{label}}</strong><button type="button" class="button small" @click="add">＋ 添加{{label}}</button></header>
<div v-for="(value,name) in modelValue" :key="name" class="request-value-row">
<input :value="name" :aria-label="label+'名称'" @change="rename(name,$event)" />
<VariableTextarea :model-value="typeof value==='string'?value:JSON.stringify(value)" :variables="variables" @update:model-value="update(name,$event)" />
<button type="button" class="button small" @click="remove(name)">删除</button>
</div>
</section>
</template>
<style scoped>
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px}.request-value-row{display:grid;grid-template-columns:minmax(100px,1fr) minmax(140px,2fr) auto;gap:8px;margin:8px 0;align-items:start}.request-value-row :deep(textarea){min-height:44px} @media(max-width:600px){.request-value-row{grid-template-columns:1fr}.request-value-row button{justify-self:end}}
</style>
