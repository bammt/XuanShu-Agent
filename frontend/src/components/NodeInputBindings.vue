<script setup>
import { computed, watch } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import { isFileVariable } from '../services/nodeBindings.js'

// The backend keeps a small, explicit binding format (input/node/literal).
// The editor presents only two concepts to users: a variable (which can come
// from runtime input or any previous node) and a fixed value.
const props = defineProps({
  modelValue: { type: Object, default: () => ({}) },
  variables: { type: Array, default: () => [] },
  // Kept for old callers while all editors move to the unified variable list.
  inputs: { type: Array, default: () => [] },
  dependencies: { type: Array, default: () => [] },
  schema: { type: Array, default: () => [] },
  title: { type: String, default: '明确输入参数' },
  description: { type: String, default: '' },
  locked: { type: Boolean, default: false },
  // Code signatures are editable contracts: users may add, remove, rename,
  // and re-type parameters. Tool schemas remain locked because they belong
  // to the registered tool itself.
  editableSchema: { type: Boolean, default: false },
  allowFile: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue'])

const hasSchema = computed(() => props.schema.length > 0)
const canEditSchema = computed(() => !props.locked && (props.editableSchema || !hasSchema.value))
const normalizedSchema = computed(() => props.schema.map((field) => ({
  name: field.name,
  label: field.label || field.name,
  description: field.description || '',
  value_type: field.value_type || field.type || 'string',
  required: Boolean(field.required),
})).filter((field) => /^[A-Za-z_]\w*$/.test(field.name || '')))

const variableOptions = computed(() => {
  if (props.variables.length) return props.variables.filter(
    (item) => props.allowFile || !isFileVariable(item),
  )
  const options = []
  for (const input of props.inputs) {
    options.push({
      key: `input:${input.name}`,
      name: input.name,
      label: input.label || input.name,
      source: 'input',
      variable: input.name,
      value_type: input.value_type || input.input_type || 'string',
    })
  }
  for (const dependency of props.dependencies) {
    for (const field of dependency.output_variables || [{ name: 'output', value_type: 'object' }]) {
      options.push({
        key: `node:${dependency.id}:${field.name}`,
        name: field.name,
        label: `${dependency.name}（${dependency.id}）· ${field.name}`,
        source: 'node',
        node_id: dependency.id,
        variable: field.name,
        value_type: field.value_type || 'object',
      })
    }
  }
  return options
})

function defaultBinding(field = {}) {
  return {
    source: field.source || 'input',
    variable: field.variable || '',
    node_id: field.node_id || '',
    value: field.value ?? '',
    ...(field.value_type ? { value_type: field.value_type } : {}),
  }
}

function patch(name, value) {
  emit('update:modelValue', {
    ...props.modelValue,
    [name]: { ...defaultBinding(props.modelValue[name]), ...value },
  })
}

function add() {
  if (!canEditSchema.value) return
  let i = 1
  while (`arg${i}` in props.modelValue) i += 1
  patch(`arg${i}`, { source: 'input', variable: '', value_type: 'string' })
}

function remove(name) {
  if (!canEditSchema.value) return
  const value = { ...props.modelValue }
  delete value[name]
  emit('update:modelValue', value)
}

function rename(name, event) {
  if (!canEditSchema.value) return
  const next = event.target.value.trim()
  if (!/^[A-Za-z_]\w*$/.test(next) || (next !== name && next in props.modelValue)) {
    event.target.value = name
    return
  }
  emit('update:modelValue', Object.fromEntries(
    Object.entries(props.modelValue).map(([key, value]) => [key === name ? next : key, value]),
  ))
}

function typeLabel(type) {
  return {
    string: '文本', text: '文本', long_text: '长文本', number: '数字', integer: '整数',
    boolean: '布尔', object: '对象', json: 'JSON', array: '数组', file: '文件', image: '图片',
  }[type] || type
}

function fieldFor(name) {
  return normalizedSchema.value.find((field) => field.name === name)
}

function optionKey(option) {
  return option.key || (option.source === 'node'
    ? `node:${option.node_id}:${option.variable}`
    : `input:${option.variable || option.name}`)
}

function bindingVariableKey(binding) {
  if (binding?.source === 'node') return `node:${binding.node_id}:${binding.variable || 'output'}`
  if (binding?.source === 'input') return `input:${binding.variable || ''}`
  return ''
}

function selectedVariableKey(binding) {
  const key = bindingVariableKey(binding)
  return variableOptions.value.some((option) => optionKey(option) === key) ? key : ''
}

function chooseSource(name, source) {
  if (source === 'literal') {
    patch(name, { source: 'literal', value: props.modelValue[name]?.value ?? '' })
    return
  }
  const binding = props.modelValue[name] || {}
  const current = variableOptions.value.find((option) => optionKey(option) === bindingVariableKey(binding))
  const first = current || variableOptions.value[0]
  if (!first) {
    patch(name, { source: 'input', variable: '', node_id: '' })
    return
  }
  chooseVariable(name, optionKey(first))
}

function chooseVariable(name, key) {
  const option = variableOptions.value.find((item) => optionKey(item) === key)
  if (!option) {
    patch(name, { source: 'input', variable: '', node_id: '' })
    return
  }
  patch(name, {
    source: option.source === 'node' ? 'node' : 'input',
    variable: option.variable || option.name || '',
    node_id: option.source === 'node' ? option.node_id || '' : '',
  })
}

function fixedValueType(name) {
  return props.modelValue[name]?.value_type || fieldFor(name)?.value_type || 'string'
}

function updateType(name, event) {
  if (!canEditSchema.value) return
  patch(name, { value_type: event.target.value })
}

function fixedValueText(binding) {
  if (binding?.value === null || binding?.value === undefined) return ''
  if (typeof binding.value === 'object') return JSON.stringify(binding.value)
  return String(binding.value)
}

function updateFixedValue(name, event) {
  const type = fixedValueType(name)
  let value = event.target.value
  if (type === 'number' || type === 'integer') value = value === '' ? '' : Number(value)
  else if (type === 'object' || type === 'array' || type === 'json') {
    const raw = String(value || '').trim()
    if (raw) {
      try { value = JSON.parse(raw) } catch { value = raw }
    }
  }
  patch(name, { value })
}

function updateFixedBoolean(name, event) {
  patch(name, { value: Boolean(event.target.checked) })
}

// Tool schemas and code signatures can arrive after the inspector is mounted.
// A deep watch also reacts when the code textarea changes from one argument to
// several arguments, pruning stale names and adding every declared parameter.
watch(normalizedSchema, (schema) => {
  if (!schema.length) return
  const next = {}
  for (const field of schema) next[field.name] = defaultBinding(props.modelValue[field.name])
  if (JSON.stringify(props.modelValue) !== JSON.stringify(next)) emit('update:modelValue', next)
}, { immediate: true, deep: true })
</script>

<template>
  <section class="node-input-bindings">
    <header class="node-input-bindings-head">
      <div>
        <strong>{{ title }}</strong>
        <small v-if="description">{{ description }}</small>
      </div>
      <button v-if="canEditSchema" class="binding-add-button" type="button" title="添加输入参数" @click="add"><Plus :size="14" />输入参数</button>
      <span v-else class="contract-lock">{{ hasSchema ? '契约已锁定' : '等待签名' }}</span>
    </header>
    <p v-if="hasSchema && editableSchema" class="node-input-bindings-note">代码参数可以在这里添加、改名和改类型；修改会同步回 <code>main(...)</code> 签名。每个参数再选择变量或固定值。</p>
    <p v-else-if="hasSchema" class="node-input-bindings-note">参数由工具 schema 定义，只需要为每个参数选择变量或固定值。</p>
    <p v-else-if="!Object.keys(modelValue).length" class="node-input-bindings-empty">先在代码 <code>main(...)</code> 中声明显式参数，再为参数选择变量或固定值。</p>
    <article v-for="(binding, name) in modelValue" :key="name" class="binding-card">
      <div class="binding-card-head">
        <div class="binding-argument">
          <code>{{ name }}</code>
          <span v-if="fieldFor(name)" class="binding-type">{{ typeLabel(fieldFor(name).value_type) }}</span>
          <span v-if="fieldFor(name)?.required" class="binding-required">必填</span>
        </div>
        <button v-if="canEditSchema" class="icon-button binding-remove-button" type="button" title="删除输入参数" @click="remove(name)"><Trash2 :size="13" /></button>
      </div>
      <p v-if="fieldFor(name)?.description" class="binding-description">{{ fieldFor(name).description }}</p>
      <div v-if="canEditSchema" class="binding-name-row">
        <label>参数名</label><input :value="name" aria-label="参数名" @change="rename(name, $event)" />
      </div>
      <div v-if="editableSchema" class="binding-type-row">
        <label>类型</label>
        <select :value="binding.value_type || fieldFor(name)?.value_type || 'string'" aria-label="输入类型" @change="updateType(name, $event)">
          <option value="string">文本（string）</option>
          <option value="number">数字（number）</option>
          <option value="boolean">布尔（boolean）</option>
          <option value="object">对象（object）</option>
          <option value="array">数组（array）</option>
          <option v-if="allowFile" value="file">文件（file）</option>
        </select>
      </div>
      <div class="binding-source-row">
        <label>来源</label>
        <select :value="binding.source === 'literal' ? 'literal' : 'variable'" aria-label="输入来源" @change="chooseSource(name, $event.target.value)">
          <option value="variable">变量</option><option value="literal">固定值</option>
        </select>
        <select v-if="binding.source !== 'literal'" :value="selectedVariableKey(binding)" aria-label="选择变量" @change="chooseVariable(name, $event.target.value)">
          <option value="">选择变量</option>
          <option v-for="option in variableOptions" :key="optionKey(option)" :value="optionKey(option)">{{ option.label || option.name }}（{{ typeLabel(option.value_type || 'string') }}）</option>
        </select>
        <template v-else>
          <textarea v-if="['object', 'array', 'json'].includes(fixedValueType(name))" :value="fixedValueText(binding)" aria-label="固定值" placeholder="输入 JSON" rows="2" @input="updateFixedValue(name, $event)" />
          <label v-else-if="fixedValueType(name) === 'boolean'" class="binding-boolean"><input type="checkbox" :checked="Boolean(binding.value)" @change="updateFixedBoolean(name, $event)" /> true</label>
          <input v-else :type="['number', 'integer'].includes(fixedValueType(name)) ? 'number' : 'text'" :value="fixedValueText(binding)" aria-label="固定值" placeholder="输入固定值" @input="updateFixedValue(name, $event)" />
        </template>
      </div>
      <small v-if="binding.source !== 'literal' && !selectedVariableKey(binding)" class="binding-warning">请选择变量，否则执行时会缺少参数。</small>
    </article>
  </section>
</template>
