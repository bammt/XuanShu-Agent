<script setup>
import { computed, nextTick, ref } from 'vue'
import { insertVariable, variableQuery } from '../services/variableInsertion.js'
const props = defineProps({ modelValue: {type: String, default: ''}, variables: {type: Array, default: () => []} })
const emit = defineEmits(['update:modelValue'])
const input = ref(null)
const query = ref(null)
const cursor = ref(0)
const selected = ref(0)
const options = computed(() => props.variables.filter(v => `${v.name} ${v.label || ''}`.toLowerCase().includes((query.value?.query || '').toLowerCase())))
function update(event) {
  const value = event.target.value
  cursor.value = event.target.selectionStart
  query.value = variableQuery(value, cursor.value)
  selected.value = 0
  emit('update:modelValue', value)
}
async function choose(variable) {
  if (!variable || !query.value) return
  const result = insertVariable(props.modelValue, query.value.start, cursor.value, variable.name)
  emit('update:modelValue', result.value)
  query.value = null
  await nextTick()
  input.value.focus()
  input.value.setSelectionRange(result.cursor, result.cursor)
}
function keydown(event) {
  if (!query.value) return
  if (event.key === 'Escape') { query.value = null; event.preventDefault() }
  if (['ArrowDown', 'ArrowUp'].includes(event.key) && options.value.length) {
    event.preventDefault()
    selected.value = (selected.value + (event.key === 'ArrowDown' ? 1 : -1) + options.value.length) % options.value.length
  }
  if (event.key === 'Enter' && options.value.length && !event.isComposing) {
    event.preventDefault(); choose(options.value[selected.value])
  }
}
function close() { query.value = null }
</script>
<template>
  <div class="variable-textarea">
    <textarea ref="input" :value="modelValue" aria-label="支持变量的文本输入" :aria-expanded="Boolean(query)" aria-autocomplete="list" @input="update" @keydown="keydown" @click="close" @blur="close" />
    <small>输入 / 选择变量，支持 ↑ ↓ 和 Enter 插入。</small>
    <div v-if="query" class="variable-menu" role="listbox" aria-label="可用变量">
      <button v-for="(variable,index) in options" :key="variable.name" type="button" role="option" :aria-selected="index === selected" :class="{ selected: index === selected }" @mousedown.prevent="choose(variable)">
        <strong>{{ '{' + variable.name + '}' }}</strong><span>{{ variable.label }}</span>
      </button>
      <span v-if="!options.length">没有匹配的可用变量</span>
    </div>
  </div>
</template>
<style scoped>
.variable-textarea { position:relative; width:100%; }
.variable-textarea textarea { width:100%; min-height:100px; }
.variable-menu { position:absolute; top:100%; left:0; right:0; z-index:50; max-height:260px; overflow:auto; padding:8px; background:var(--surface, #fff); border:1px solid #ccd8cc; border-radius:10px; box-shadow:0 8px 24px #0002; }
.variable-menu button { display:flex; flex-direction:column; align-items:flex-start; width:100%; padding:10px; background:transparent; border:0; text-align:left; font:inherit; cursor:pointer; }
.variable-menu button.selected,.variable-menu button:hover { background:#edf3ec; }
.variable-menu span { font-size:14px; color:#647264; }
</style>
