<script setup>
import { computed } from 'vue'
const props = defineProps({ modelValue: {type:Object,default:()=>({})} })
const emit = defineEmits(['update:modelValue'])
const fields = computed(() => Object.entries(props.modelValue.properties || {}))
const typeOptions = [
  {value:'string', label:'字符串'}, {value:'number', label:'数字'},
  {value:'integer', label:'整数'}, {value:'boolean', label:'布尔'},
  {value:'object', label:'对象'}, {value:'array', label:'数组'},
  {value:'file', label:'文件'},
]
function update(name, patch) {
  emit('update:modelValue', {...props.modelValue, type:'object', properties:{...props.modelValue.properties,[name]:{...props.modelValue.properties[name],...patch}}})
}
function changeType(name, value) {
  update(name, value === 'file'
    ? {type:'string', format:'binary', description:props.modelValue.properties?.[name]?.description || '应用工作区中的上传文件'}
    : {type:value, format:undefined})
}
function add() {
  let index = fields.value.length + 1
  while (props.modelValue.properties?.[`variable_${index}`]) index++
  const name = `variable_${index}`
  emit('update:modelValue',{...props.modelValue,type:'object',properties:{...props.modelValue.properties,[name]:{type:'string',description:''}}})
}
function rename(old,event) {
  const name = event.target.value.trim()
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) || (name !== old && props.modelValue.properties?.[name])) { event.target.value=old; return }
  const properties = Object.fromEntries(fields.value.map(([key,value])=>[key===old?name:key,value]))
  emit('update:modelValue',{...props.modelValue,properties,required:(props.modelValue.required||[]).map(key=>key===old?name:key)})
}
function required(name,checked) {
  const list = new Set(props.modelValue.required || [])
  checked ? list.add(name) : list.delete(name)
  emit('update:modelValue',{...props.modelValue,required:[...list]})
}
function remove(name) {
  const properties = {...props.modelValue.properties}; delete properties[name]
  emit('update:modelValue',{...props.modelValue,properties,required:(props.modelValue.required||[]).filter(key=>key!==name)})
}
</script>
<template>
  <section class="tool-input-editor">
    <header><strong>输入变量</strong><button type="button" class="button" @click="add">＋ 添加变量</button></header>
    <p>定义调用工具时需要提供的参数；请求配置决定参数发送到哪里。文件变量可用于 form-data 或 binary 正文。</p>
    <div v-for="[name,field] in fields" :key="name" class="input-variable-row">
      <input :value="name" aria-label="变量名" @change="rename(name,$event)" />
      <select :value="field.format === 'binary' ? 'file' : (field.type || 'string')" aria-label="变量类型" @change="changeType(name,$event.target.value)">
        <option v-for="type in typeOptions" :key="type.value" :value="type.value">{{ type.label }}</option>
      </select>
      <input :value="field.description" aria-label="变量说明" placeholder="变量用途" @input="update(name,{description:$event.target.value})" />
      <label><input type="checkbox" :checked="modelValue.required?.includes(name)" @change="required(name,$event.target.checked)" />必填</label>
      <button type="button" class="button" @click="remove(name)">删除</button>
    </div>
  </section>
</template>
<style scoped>
header {display:flex;align-items:center;justify-content:space-between;gap:12px} p{color:#647264;font-size:14px} .input-variable-row{display:grid;grid-template-columns:1fr 110px 2fr auto auto;gap:8px;margin:10px 0;align-items:center} label{white-space:nowrap} @media(max-width:640px){.input-variable-row{grid-template-columns:1fr 1fr}}
</style>
