<script setup>
import { computed } from 'vue'
import { Plus, Trash2, GitBranch } from 'lucide-vue-next'

const props = defineProps({
  modelValue: { type: Object, required: true },
  variables: { type: Array, default: () => [] },
  root: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'remove'])
const expression = computed(() => props.modelValue || { type: 'group', operator: 'and', conditions: [] })

function clone(value) { return JSON.parse(JSON.stringify(value)) }
function update(next) { emit('update:modelValue', next) }
function updateChild(index, value) {
  const next = clone(expression.value)
  next.conditions[index] = value
  update(next)
}
function addCondition() {
  const next = clone(expression.value)
  next.conditions ||= []
  next.conditions.push({
    type: 'condition', source: props.variables[0]?.source || 'input',
    node_id: props.variables[0]?.node_id || '', variable: props.variables[0]?.variable || '',
    value_type: props.variables[0]?.value_type || 'string', operator: 'equals', value: '',
  })
  update(next)
}
function addGroup() {
  const next = clone(expression.value)
  next.conditions ||= []
  next.conditions.push({ type: 'group', operator: 'and', conditions: [] })
  update(next)
}
function removeChild(index) {
  const next = clone(expression.value)
  next.conditions.splice(index, 1)
  update(next)
}
function setOperator(value) {
  const next = clone(expression.value)
  next.operator = value
  update(next)
}
function setLeaf(index, key, value) {
  const next = clone(expression.value)
  const leaf = next.conditions[index]
  if (key === 'variable_key') {
    const variable = props.variables.find(item => item.key === value)
    if (!variable) return
    Object.assign(leaf, {
      source: variable.source, variable: variable.variable,
      node_id: variable.node_id || '', value_type: variable.value_type || 'string',
    })
    if (leaf.value_type === 'boolean' && typeof leaf.value !== 'boolean') leaf.value = true
    if (leaf.value_type === 'object' && (!leaf.value || typeof leaf.value !== 'object' || Array.isArray(leaf.value))) leaf.value = {}
    if (leaf.value_type === 'array' && !Array.isArray(leaf.value)) leaf.value = []
    if (!availableOperators(leaf.value_type).some(item => item.value === leaf.operator)) leaf.operator = 'equals'
  } else leaf[key] = value
  next.conditions[index] = leaf
  update(next)
}
function availableOperators(type) {
  const common = [{ value: 'equals', label: '等于' }, { value: 'not_equals', label: '不等于' }]
  if (['string', 'text', 'file', 'image'].includes(type)) common.push(
    { value: 'contains', label: '包含' }, { value: 'not_contains', label: '不包含' },
    { value: 'starts_with', label: '开头为' }, { value: 'ends_with', label: '结尾为' },
  )
  if (type === 'number') common.push({ value: 'greater_than', label: '大于' }, { value: 'less_than', label: '小于' })
  common.push({ value: 'is_empty', label: '为空' }, { value: 'is_not_empty', label: '不为空' })
  return common
}
function setExpectedValue(index, child, raw) {
  const type = child.value_type
  let value = raw
  if (type === 'boolean') value = raw === 'true'
  else if (['object', 'array'].includes(type) && raw !== '') {
    try { value = JSON.parse(raw) } catch { value = raw }
  }
  setLeaf(index, 'value', value)
}
function expectedValueText(value) {
  if (value == null) return ''
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}
function variableKey(child) {
  return props.variables.find(item => item.source === child.source && item.variable === child.variable &&
    String(item.node_id || '') === String(child.node_id || ''))?.key || ''
}
function isNoValue(child) { return ['is_empty', 'is_not_empty'].includes(child.operator) }
</script>

<template>
  <div v-if="expression.type === 'group'" class="condition-group" :class="{ root }">
    <header class="condition-group-head">
      <span v-if="root" class="condition-root-label">条件组</span>
      <GitBranch v-else :size="13" />
      <select :value="expression.operator || 'and'" aria-label="条件组合方式" @change="setOperator($event.target.value)">
        <option value="and">全部满足（AND）</option>
        <option value="or">任一满足（OR）</option>
      </select>
      <button v-if="!root" class="condition-icon-button" type="button" title="删除条件组" @click="$emit('remove')"><Trash2 :size="13" /></button>
    </header>
    <div v-if="expression.conditions?.length" class="condition-children">
      <div v-for="(child, index) in expression.conditions" :key="child.id || index" class="condition-child">
        <ConditionGroupEditor
          v-if="child.type === 'group'"
          :model-value="child" :variables="variables"
          @update:model-value="updateChild(index, $event)"
          @remove="removeChild(index)"
        />
        <div v-else class="condition-leaf">
          <select :value="variableKey(child)" aria-label="判断变量" @change="setLeaf(index, 'variable_key', $event.target.value)">
            <option value="" disabled>选择变量</option>
            <option v-for="item in variables" :key="item.key" :value="item.key">{{ item.label }}</option>
          </select>
          <select :value="child.operator || 'equals'" aria-label="判断方式" @change="setLeaf(index, 'operator', $event.target.value)">
            <option v-for="operator in availableOperators(child.value_type || 'string')" :key="operator.value" :value="operator.value">{{ operator.label }}</option>
          </select>
          <input v-if="!isNoValue(child) && !['object', 'array', 'boolean'].includes(child.value_type)" :type="child.value_type === 'number' ? 'number' : 'text'" :value="expectedValueText(child.value)" aria-label="条件值" :placeholder="child.value_type || '输入值'" @input="setExpectedValue(index, child, $event.target.value)" />
          <select v-else-if="!isNoValue(child) && child.value_type === 'boolean'" :value="String(child.value ?? true)" aria-label="条件值" @change="setExpectedValue(index, child, $event.target.value)">
            <option value="true">是</option><option value="false">否</option>
          </select>
          <textarea v-else-if="!isNoValue(child)" :value="expectedValueText(child.value)" aria-label="JSON 条件值" placeholder="输入 JSON" @change="setExpectedValue(index, child, $event.target.value)" />
          <button class="condition-icon-button" type="button" title="删除条件" @click="removeChild(index)"><Trash2 :size="13" /></button>
        </div>
      </div>
    </div>
    <div class="condition-group-actions">
      <button type="button" @click="addCondition"><Plus :size="13" />添加条件</button>
      <button type="button" @click="addGroup"><GitBranch :size="13" />嵌套条件组</button>
    </div>
  </div>
</template>

<style scoped>
.condition-group{display:grid;gap:8px;padding:10px;border:1px solid #dce4df;border-radius:6px;background:#fff}
.condition-group.root{padding:0;border:0;background:transparent}
.condition-group-head{display:flex;align-items:center;gap:8px;min-width:0;color:#60716a}
.condition-root-label{font-size:12px;font-weight:700;color:#53635c}
.condition-group-head select{flex:1;min-width:0}
.condition-children{display:grid;gap:8px;padding-left:12px;border-left:2px solid #d9e3dd}
.condition-child{min-width:0}
.condition-leaf{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(100px,.8fr) minmax(90px,1fr) 30px;gap:6px;align-items:center;padding:7px;background:#f1f3f5;border-radius:6px}
.condition-leaf select,.condition-leaf input,.condition-leaf textarea,.condition-group-head select{height:34px;min-width:0;border:1px solid #d9e0dc;border-radius:5px;background:#fff;padding:0 8px;color:#35453e;font:inherit}
.condition-leaf textarea{resize:vertical;min-height:52px;padding-top:7px}
.condition-icon-button{display:grid;place-items:center;width:30px;height:30px;border:0;background:transparent;color:#75827c;cursor:pointer}
.condition-group-actions{display:flex;flex-wrap:wrap;gap:6px;padding-left:12px}
.condition-group-actions button{display:inline-flex;align-items:center;gap:4px;min-height:30px;padding:0 9px;border:1px solid #d9e0dc;border-radius:5px;background:#fff;color:#52635b;cursor:pointer}
@media(max-width:760px){.condition-leaf{grid-template-columns:minmax(0,1fr) 32px}.condition-leaf select,.condition-leaf input,.condition-leaf textarea{grid-column:1}.condition-leaf .condition-icon-button{grid-column:2;grid-row:1 / span 4}}
</style>
