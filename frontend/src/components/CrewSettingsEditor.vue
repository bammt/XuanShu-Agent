<script setup>
import { computed } from 'vue'
import { CircleHelp, Settings2 } from 'lucide-vue-next'

const props = defineProps({
  modelValue: { type: Object, required: true },
  prefix: { type: String, default: '' },
  agents: { type: Array, default: () => [] },
  models: { type: Array, default: () => [] },
  title: { type: String, default: 'Crew 运行设置' },
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['process-change', 'manager-change'])

const names = computed(() => props.prefix ? {
  process: 'crew_process', memory: 'crew_memory', planning: 'crew_planning',
  cache: 'crew_cache', outputLog: 'crew_output_log_file',
  managerAgent: 'crew_manager_agent_id', managerModel: 'crew_manager_model_profile_id',
  planningModel: 'crew_planning_model_profile_id', verbose: 'crew_verbose',
} : {
  process: 'process', memory: 'memory', planning: 'planning', cache: 'cache',
  outputLog: 'output_log_file', managerAgent: 'manager_agent_id',
  managerModel: 'manager_model_profile_id', planningModel: 'planning_model_profile_id',
  verbose: 'verbose',
})
function value(name) { return props.modelValue[names.value[name]] }
function setValue(name, value) { props.modelValue[names.value[name]] = value }
function processChange(event) {
  setValue('process', event.target.value)
  emit('process-change', event.target.value)
}
function managerChange(event) {
  const managerId = event.target.value || null
  setValue('managerAgent', managerId)
  emit('manager-change', managerId)
}
</script>

<template>
  <section class="crew-settings-editor" :class="{ compact }">
    <header class="crew-settings-editor-head">
      <div>
        <span class="inspector-kicker"><Settings2 :size="13" /> CREW RUNTIME</span>
        <h3>{{ title }}</h3>
      </div>
      <span class="crew-settings-parity">同一套 Crew 参数</span>
    </header>
    <p class="crew-settings-intro">这些设置控制当前 Crew 的执行进程、管理角色、记忆和日志。</p>
    <div class="crew-settings-grid">
      <label class="field"><span>执行模式 <small>process</small></span><select :value="value('process') || 'sequential'" @change="processChange"><option value="sequential">顺序协作</option><option value="hierarchical">层级协作</option></select></label>
    </div>
    <div class="crew-switch-grid">
      <label class="toggle-row"><span>记忆 <small>memory</small></span><input :checked="Boolean(value('memory'))" type="checkbox" @change="setValue('memory', $event.target.checked)" /></label>
      <label class="toggle-row"><span>规划 <small>planning</small></span><input :checked="Boolean(value('planning'))" type="checkbox" @change="setValue('planning', $event.target.checked)" /></label>
      <label class="toggle-row"><span>工具缓存 <small>cache</small></span><input :checked="value('cache') !== false" type="checkbox" @change="setValue('cache', $event.target.checked)" /></label>
      <label class="toggle-row"><span>详细日志 <small>verbose</small></span><input :checked="Boolean(value('verbose'))" type="checkbox" @change="setValue('verbose', $event.target.checked)" /></label>
    </div>
    <template v-if="value('process') === 'hierarchical'">
      <label class="field"><span>管理 Agent <small>manager_agent</small></span><select :value="value('managerAgent') || ''" @change="managerChange"><option value="">使用 manager_llm</option><option v-for="agent in agents" :key="agent.id" :value="agent.id">{{ agent.role }}</option></select></label>
      <label v-if="!value('managerAgent')" class="field"><span>管理模型 <small>manager_llm</small></span><select :value="value('managerModel') || ''" @change="setValue('managerModel', $event.target.value || null)"><option value="">工作流默认</option><option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}</option></select></label>
    </template>
    <label v-if="value('planning')" class="field"><span>规划模型 <small>planning_llm</small></span><select :value="value('planningModel') || ''" @change="setValue('planningModel', $event.target.value || null)"><option value="">工作流默认</option><option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}</option></select></label>
    <label class="field"><span>执行日志文件 <small>output_log_file</small></span><input :value="value('outputLog') || ''" placeholder="logs/crew.json" @input="setValue('outputLog', $event.target.value)" /><small class="field-help-text">填写应用工作区内的相对路径，运行完成后作为文件产物展示。</small></label>
  </section>
</template>
