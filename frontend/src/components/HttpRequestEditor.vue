<script setup>
import { computed } from 'vue'
import VariableTextarea from './VariableTextarea.vue'
import RequestKeyValues from './RequestKeyValues.vue'
const props = defineProps({modelValue:{type:Object,required:true}, variables:{type:Array,default:()=>[]}})
const emit = defineEmits(['update:modelValue'])
const config = computed(() => props.modelValue.version === 2 ? props.modelValue : {version:2,params:{},body_type:'json',body:props.modelValue,verify_ssl:true,timeout:30})
function update(patch) { emit('update:modelValue',{...config.value,...patch}) }
const bodyText = computed(()=>typeof config.value.body==='string'?config.value.body:JSON.stringify(config.value.body??{},null,2))
function body(value) {
  if (config.value.body_type==='raw') {update({body:value});return}
  // Preserve editing text; parent validates JSON on save.
  update({body:value,body_is_text:true})
}
</script>
<template>
<section>
  <RequestKeyValues :model-value="config.params || {}" :variables="variables" label="查询参数" @update:model-value="update({params:$event})" />
  <label>请求正文 Body</label>
  <select :value="config.body_type" @change="update({body_type:$event.target.value,body:{},body_is_text:false})">
    <option value="none">无正文</option><option value="json">JSON</option><option value="form">x-www-form-urlencoded</option><option value="multipart">form-data（多部分）</option><option value="raw">原始文本</option><option value="binary">二进制</option>
  </select>
  <RequestKeyValues v-if="['form','multipart'].includes(config.body_type)" :model-value="config.body || {}" :variables="variables" :label="config.body_type==='multipart' ? '表单字段（form-data）' : '表单字段'" @update:model-value="update({body:$event,body_is_text:false})" />
  <VariableTextarea v-if="['json','raw','binary'].includes(config.body_type)" :model-value="bodyText" :variables="variables" @update:model-value="body" />
  <label class="toggle-row">验证 SSL 证书<input type="checkbox" :checked="config.verify_ssl!==false" @change="update({verify_ssl:$event.target.checked})" /></label>
  <label>超时（秒）<input type="number" min="1" max="300" :value="config.timeout || 30" @input="update({timeout:Number($event.target.value)})" /></label>
  <details><summary>失败时重试</summary>
    <label>重试次数<input type="number" min="0" max="5" :value="config.max_retries || 0" @input="update({max_retries:Number($event.target.value)})" /></label>
    <label>重试间隔（毫秒）<input type="number" min="0" max="10000" :value="config.retry_interval_ms ?? 1000" @input="update({retry_interval_ms:Number($event.target.value)})" /></label>
    <small>仅对连接错误、429 和服务端错误重试。默认不重试。</small>
  </details>
</section>
</template>
<style scoped>
header {display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.param-row{display:grid;grid-template-columns:1fr 2fr auto;gap:10px;align-items:start} section>label{display:block;margin:14px 0 8px}
</style>
