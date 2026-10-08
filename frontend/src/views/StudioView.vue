<script setup>
import VariableTextarea from '../components/VariableTextarea.vue';
import NodeInputBindings from '../components/NodeInputBindings.vue';
import ConditionGroupEditor from '../components/ConditionGroupEditor.vue';
import CodeEditor from '../components/CodeEditor.vue';
import CrewSettingsEditor from '../components/CrewSettingsEditor.vue';
import {
  migrateDeterministicBindings,
  removeDependencyBindings,
  selectableNodeVariables,
} from '../services/nodeBindings.js';
import { isLatestRunMessage } from '../services/latestRun.js';
import {
  computed,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  VueFlow,
  Handle,
  MarkerType,
  Panel,
  Position,
  useVueFlow,
} from "@vue-flow/core";
import {
  ArrowUpRight,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  CircleHelp,
  Cpu,
  Download,
  FileText,
  GitBranch,
  GripVertical,
  Image,
  Library,
  ListTodo,
  LoaderCircle,
  LockKeyhole,
  Maximize2,
  MessageCircle,
  Pencil,
  Play,
  Plus,
  Route,
  RotateCcw,
  Save,
  Send,
  Paperclip,
  Settings2,
  Sparkles,
  Trash2,
  UserRound,
  UsersRound,
  Wrench,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-vue-next";
import { api } from "../services/api";
import { mergeDraft } from "../services/draftMerge.js";
import { applyRunFrame, createRunFrameBatcher } from "../services/runStream";
import { stripLocalArtifactReferences } from "../services/messageFormatting";
import { usePlatformStore } from "../stores/platform";
import RunApprovalCard from "../components/RunApprovalCard.vue";
import RichMessage from "../components/RichMessage.vue";
import AutomationDetailsDialog from "../components/AutomationDetailsDialog.vue";
import { formatBeijingDateTime, timestampValue } from "../services/dateFormatting";

const route = useRoute();
const router = useRouter();
const store = usePlatformStore();
const { fitView, zoomIn, zoomOut } = useVueFlow({ id: "studio-flow" });

// Canvas cards have content-driven heights. Keep a generous vertical lane so
// generated Agent cards cannot cover the Task cards below them.
const CANVAS_COLUMN_GAP = 330;
const CANVAS_AGENT_START_Y = 70;
const CANVAS_AGENT_ROW_GAP = 250;
const CANVAS_TASK_Y = 500;

const ParamLabel = (props) =>
  h("span", { class: "param-label" }, [
    props.text,
    props.help
      ? h(
          "button",
          {
            class: "field-help",
            type: "button",
            title: props.help,
            "aria-label": `${props.text}帮助`,
            onClick: (event) => event.stopPropagation(),
          },
          [h(CircleHelp, { size: 12 })],
        )
      : null,
  ]);

const busy = ref(false);
const activity = ref(null);
const attachments = ref([]);
const uploading = ref(false);
const uploadProgress = ref(0);
const pendingUploadNames = ref([]);
const fileInput = ref(null);
const assistantThread = ref(null);
const saveState = ref("Ready");
const pageLoading = ref(true);
const assistantOpen = ref(false);
const historyBefore = ref(0);
const historyHasMore = ref(false);
const historyLoading = ref(false);
const historySessionId = ref("");
const prompt = ref("");
const selectedTaskId = ref("");
const selectedAgentId = ref("");
const selectedEdgeId = ref("");
const canvasAddMenuOpen = ref(false);
// The inspector is opt-in when the canvas is idle. Selecting a node/edge
// opens it automatically; the settings button opens the workflow settings
// view without inventing a node selection.
const inspectorSettingsOpen = ref(false);
const showRun = ref(false);
const previewValues = reactive({});
const previewFiles = reactive({});
const previewMessages = ref([]);
const previewMessage = ref("");
const previewBusy = ref(false);
// Prevent a second submit while save() is awaiting the backend. previewBusy is
// set immediately by sendPreview, but this separate flag also guards the
// synchronous entry point before Vue has rendered the disabled button.
let previewSendInFlight = false;
const previewThread = ref(null);
const previewFileInput = ref(null);
const previewUploading = ref(false);
const previewUploadProgress = ref(0);
const previewPendingUploadNames = ref([]);
let previewFollowBottom = true;
let previewScrollTimer = null;
const previewConversationId = ref("");
const remoteSessions = ref([]);
const confirmation = ref(null);
const removedContractInputNames = new Set();
const proposalSubmittingStage = ref(null);
const capabilityPickerOpen = ref(false);
const proposalCapabilityPickerOpen = ref(false);
const proposalCapabilityTarget = ref(null);
const proposalCapabilitySelection = ref([]);
const automationDetailsOpen = ref(false);
const automationDetailsMode = ref("create");
const automationDetailsKind = ref("crew");
// Type selection only opens the builder. It is not runtime-input approval.
const selectedKind = ref(route.params.kind || null);
const kindPreselected = ref(Boolean(route.params.kind));
const messages = ref([
  {
    role: "assistant",
    text: "你好，我可以和你一起设计可发布、可复用的 CrewAI 智能体。请描述目标，也可以上传参考文件。",
  },
]);
let hydrating = true;
let draftDirty = false;
// Set when the last draft save was rejected as invalid (422). Sending a chat
// message is then still allowed: the message is how the user asks the
// Composer to repair the draft.  Conflicts (409) and network errors still block.
let draftSaveInvalid = false;
let draftTimer = null;
let saveInFlight = null;
let draftEditVersion = 0;
let previewPollTimer = null;
let previewRunAbortController = null;
let persistedWorkflowSnapshot = null;
let pendingManualChanges = [];
const syncedManualChanges = ref([]);
const crewDependencyDrafts = reactive({});

const trackedDraftFields = [
  "name",
  "description",
  "kind",
  "process",
  "planning",
  "memory",
  "interaction_mode",
  "interaction",
  "inputs",
  "agents",
  "tasks",
  "tools",
  "capability_requirements",
  "manager_agent_id",
];

function cloneDraftValue(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function summarizeDraftField(field, value) {
  if (["agents", "tasks", "inputs"].includes(field) && Array.isArray(value)) {
    return value.map((item) => {
      if (field === "agents")
        return {
          id: item.id,
          role: item.role,
          goal: item.goal,
          user_interaction: Boolean(item.user_interaction),
          skills: item.skills || [],
          plugins: item.plugins || [],
          knowledge_base_ids: item.knowledge_base_ids || [],
        };
      if (field === "tasks")
        return {
          id: item.id,
          name: item.name,
          node_type: item.node_type,
          agent_id: item.agent_id,
          depends_on: item.depends_on || [],
          crew_agent_ids: item.crew_agent_ids || [],
        };
      return {
        name: item.name,
        label: item.label,
        input_type: item.input_type,
        required: Boolean(item.required),
        multiple: Boolean(item.multiple),
      };
    });
  }
  if (typeof value === "string") return value.slice(0, 320);
  return cloneDraftValue(value);
}

function draftChangeRecord(before, after) {
  const fields = trackedDraftFields
    .filter((field) => JSON.stringify(before?.[field]) !== JSON.stringify(after?.[field]))
    .map((field) => ({
      name: field,
      before: summarizeDraftField(field, before?.[field]),
      after: summarizeDraftField(field, after?.[field]),
    }));
  if (!fields.length) return null;
  return {
    source: "canvas",
    at: new Date().toISOString(),
    fields,
  };
}

function collectPendingManualChanges() {
  if (!persistedWorkflowSnapshot || !workflow.value.structure_confirmed) return [];
  const record = draftChangeRecord(persistedWorkflowSnapshot, workflow.value);
  if (!record) return pendingManualChanges;
  const fingerprint = JSON.stringify(record.fields);
  const previous = pendingManualChanges.at(-1);
  if (!previous || JSON.stringify(previous.fields) !== fingerprint)
    pendingManualChanges.push(record);
  pendingManualChanges = pendingManualChanges.slice(-20);
  return pendingManualChanges;
}

function currentManualChanges() {
  collectPendingManualChanges();
  return [...syncedManualChanges.value, ...pendingManualChanges].slice(-50);
}

function markWorkflowPersisted(value) {
  persistedWorkflowSnapshot = cloneDraftValue(value);
  pendingManualChanges = [];
  syncedManualChanges.value = cloneDraftValue(value?.draft_sync?.manual_changes || []);
}

const typeInfo = {
  tool: { label: "工具", title: "Tool call", description: "使用明确输入直接调用一个工具" },
  task: {
    label: "任务",
    title: "Crew Task",
    description: "由 Crew 中的 Agent 执行一项明确工作",
  },
  agent: {
    label: "单 Agent",
    title: "Agent call",
    description: "在 Flow 中直接调用一个 Agent",
  },
  crew: {
    label: "Crew",
    title: "Crew call",
    description: "在 Flow 中调用一个由连线成员组成的 Crew",
  },
  router: {
    label: "路由",
    title: "Router",
    description: "按多条件规则选择一个执行分支",
  },
  code: {
    label: "代码",
    title: "Code step",
    description: "从自定义 CrewAI Python 方法解析的流程步骤",
  },
};

const creationKind = () => (route.params.kind === "flow" ? "flow" : "crew");
function nextWorkflowName(prefix) {
  const counters = workflow.value.node_name_counters ||= {};
  let next = Number(counters[prefix] || 0);
  const pattern = new RegExp(`^${prefix}_(\\d+)$`);
  const values = [
    ...(workflow.value.agents || []).map((item) => item.id),
    ...(workflow.value.tasks || []).map((item) => item.id),
    ...(workflow.value.tasks || []).flatMap((item) =>
      (item.crew_tasks || []).map((nested) => nested.id),
    ),
  ];
  values.forEach((value) => {
    const match = String(value || '').match(pattern);
    if (match) next = Math.max(next, Number(match[1]));
  });
  counters[prefix] = next + 1;
  return `${prefix}_${counters[prefix]}`;
}
const emptyWorkflow = (kind = creationKind()) => ({
  id: Math.random().toString(36).slice(2, 14),
  name: "新智能体",
  description: "",
  kind,
  process: "sequential",
  planning: false,
  planning_model_profile_id: null,
  memory: false,
  cache: true,
  verbose: false,
  output_log_file: "",
  manager_agent_id: null,
  manager_model_profile_id: null,
  max_method_calls: 100,
  model: store.defaultModel?.model || "",
  model_profile_id: store.defaultModel?.id || null,
  status: "draft",
  agents: [],
  tasks: [],
  node_name_mode: "manual",
  node_name_counters: { agent: 0, crew: 0, node: 0, task: 0 },
  inputs: [{
    name: "message",
    label: "用户需求",
    input_type: "long_text",
    required: true,
    multiple: false,
    description: "用户本轮对智能体的需求描述，通过 {message} 传入执行流程。",
  }],
  tags: [],
  chat_history: [],
  draft_sync: { source: "canvas", manual_changes: [], workflow: {} },
  structure_confirmed: false,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
});
const workflow = ref(emptyWorkflow());
const studioSessionId = ref("");

const selectedTask = computed(() =>
  workflow.value.tasks.find((item) => item.id === selectedTaskId.value),
);
const flowTools = computed(() => store.plugins.filter((item) => ['http', 'python'].includes(item.kind)));
const selectedFlowTool = computed(() => {
  const id = selectedTask.value?.tool_id;
  return flowTools.value.find((item) => String(item.id) === String(id)) || null;
});
function toolInputSchema(tool) {
  const schema = tool?.input_schema || tool?.args_schema || {};
  const properties = schema.properties || schema.fields || {};
  return Object.entries(properties).map(([name, value]) => ({
    name,
    label: value.title || name,
    description: value.description || '',
    value_type: value.format === 'binary' || value.type === 'file' ? 'file' : (value.type === 'integer' ? 'number' : (value.type || value.format || 'string')),
    required: Array.isArray(schema.required) && schema.required.includes(name),
  }));
}
const selectedToolInputSchema = computed(() => toolInputSchema(selectedFlowTool.value));

function mainSignatureParameters(source) {
  const match = String(source || '').match(/(?:async\s+)?def\s+main\s*\(/m);
  if (!match) return null;
  const start = match.index + match[0].length;
  let depth = 1;
  let quote = '';
  let escaped = false;
  for (let index = start; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if ('([{'.includes(character)) depth += 1;
    else if (')]}'.includes(character)) {
      depth -= 1;
      if (depth === 0) return source.slice(start, index);
    }
  }
  return null;
}

function splitSignatureParameters(value) {
  const fields = [];
  let start = 0;
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if ('([{'.includes(character)) depth += 1;
    else if (')]}' .includes(character)) depth -= 1;
    else if (character === ',' && depth === 0) {
      fields.push(value.slice(start, index).trim());
      start = index + 1;
    }
  }
  const tail = value.slice(start).trim();
  if (tail) fields.push(tail);
  return fields;
}

function codeInputSchema(task) {
  const source = String(task?.code_snippet || '');
  const existing = task?.input_bindings || {};
  const signature = mainSignatureParameters(source);
  if (signature === null) return Object.keys(existing).map((name) => ({
    name, label: name, value_type: 'object', required: true,
    description: '代码输入（请在 main 签名中声明）',
  }));
  const fields = [];
  for (const raw of splitSignatureParameters(signature)) {
    const value = raw.trim();
    if (!value || value.startsWith('*')) continue;
    const left = value.split('=', 1)[0].trim();
    const token = left.trim();
    const annotation = token.includes(':') ? token.split(':').slice(1).join(':').trim().toLowerCase() : '';
    const name = token.split(':', 1)[0].trim();
    if (!/^[A-Za-z_]\w*$/.test(name)) continue;
    fields.push({
      name,
      label: name,
      value_type: existing[name]?.value_type || (annotation.includes('bool') ? 'boolean' : annotation.includes('int') || annotation.includes('float') || annotation.includes('number') ? 'number' : annotation.includes('dict') || annotation.includes('json') ? 'object' : annotation.includes('list') || annotation.includes('array') ? 'array' : 'string'),
      required: !value.includes('='),
      description: annotation ? `main 参数类型：${annotation}` : 'main 显式参数',
    });
  }
  // A **kwargs signature is legacy/dynamic. Keep its existing bindings visible
  // only when the signature itself cannot expose a parameter list. For an
  // explicit signature, the signature is authoritative and removed arguments
  // must disappear from the binding contract as well.
  if (!fields.length) return Object.keys(existing).map((name) => ({
    name, label: name, value_type: 'object', required: true,
    description: '旧版动态输入；建议改为 main 的显式参数',
  }));
  return fields;
}
const selectedCodeInputSchema = computed(() => codeInputSchema(selectedTask.value));

function codeTypeAnnotation(valueType) {
  return {
    number: 'float', boolean: 'bool', object: 'dict', array: 'list',
  }[valueType] || '';
}

function syncCodeSignature(task, bindings) {
  if (!task || task.node_type !== 'code') return;
  const source = String(task.code_snippet || '');
  const match = source.match(/(?:async\s+)?def\s+main\s*\(/m);
  const signature = mainSignatureParameters(source);
  if (!match || signature === null) return;
  const open = source.indexOf('(', match.index);
  const start = open + 1;
  const end = start + signature.length;
  const parameters = Object.entries(bindings || {}).map(([name, binding]) => {
    const annotation = codeTypeAnnotation(binding?.value_type);
    return annotation ? `${name}: ${annotation}` : name;
  }).join(', ');
  const next = `${source.slice(0, start)}${parameters}${source.slice(end)}`;
  if (next !== source) task.code_snippet = next;
}
function selectFlowTool(task, id) {
  task.tool_id = id || null;
  if (!id) {
    task.input_bindings = {};
    return;
  }
  const tool = flowTools.value.find((item) => String(item.id) === String(id));
  const next = {};
  for (const field of toolInputSchema(tool)) {
    next[field.name] = task.input_bindings?.[field.name] || {
      source: 'input', variable: '', node_id: '', value: '',
    };
  }
  task.input_bindings = next;
}
const runInputOptions = computed(() => {
  const items = (workflow.value.inputs || []).map(input => ({
    name: input.name,
    label: input.label || input.description || '运行输入',
    value_type: ({ text: 'string', long_text: 'string', image: 'file', json: 'object' })[input.input_type] || input.input_type || 'string',
  }));
  for (const name of ['message', 'files', 'conversation_history']) {
    if (!items.some(item => item.name === name)) items.push({
      name,
      label: {
        message: '本轮用户消息',
        files: '本次消息附件（系统自动注入）',
        conversation_history: '会话历史',
      }[name],
      value_type: {
        message: 'string',
        files: 'file',
        conversation_history: 'object',
      }[name],
      description: {
        message: '运行页当前发送的文字消息。',
        files: '运行页当前消息上传的附件集合；由系统自动注入，不需要单独填写。',
        conversation_history: '同一会话中之前已完成运行的摘要和问答。',
      }[name],
    });
  }
  return items;
});

function ancestorNodeIds(task, tasks = workflow.value.tasks) {
  if (!task) return new Set();
  const byId = new Map(tasks.map((item) => [item.id, item]));
  const found = new Set();
  const pending = [...(task.depends_on || [])];
  while (pending.length) {
    const id = pending.pop();
    if (found.has(id) || !byId.has(id)) continue;
    found.add(id);
    pending.push(...(byId.get(id).depends_on || []));
  }
  return found;
}

function nodeVariableOptions(task) {
  const items = runInputOptions.value.map((item) => ({
    key: `input:${item.name}`,
    ...item,
    source: 'input',
    variable: item.name,
  }));
  if (!task) return items;
  const ancestors = ancestorNodeIds(task);
  const previousTasks = workflow.value.tasks.filter((item) => ancestors.has(item.id));
  for (const previous of previousTasks) {
    const fields = previous.output_variables?.length
      ? previous.output_variables
      : [{ name: 'output', value_type: 'object', description: '完整节点输出' }];
    for (const field of fields) {
      items.push({
        key: `node:${previous.id}:${field.name}`,
        name: `${previous.id}.${field.name}`,
        label: `${previous.name}（${previous.id}）· ${field.name}`,
        source: 'node',
        node_id: previous.id,
        variable: field.name,
        value_type: field.value_type || 'object',
        description: field.description || '',
      });
    }
  }
  const unique = [...new Map(items.map((item) => [item.key, item])).values()];
  return selectableNodeVariables(unique, task?.node_type);
}
function upstreamTasks(task) {
  const ancestors = ancestorNodeIds(task);
  return workflow.value.tasks.filter((item) => ancestors.has(item.id));
}
const selectedNodeVariableOptions = computed(() => nodeVariableOptions(selectedTask.value));
function conditionVariableOptions(task) {
  return task ? nodeVariableOptions(task) : [];
}
const selectedConditionVariableOptions = computed(() => conditionVariableOptions(selectedTask.value));

function updateDeterministicBindings(task, bindings) {
  if (!task) return;
  // Bindings are the parameter contract. Canvas edges remain execution-order
  // dependencies even when no parameter currently consumes their output.
  const dependencies = new Set(task.depends_on || []);
  const ancestors = ancestorNodeIds(task);
  for (const binding of Object.values(bindings || {})) {
    if (binding?.source === 'node' && binding.node_id && binding.node_id !== task.id &&
        !ancestors.has(binding.node_id))
      dependencies.add(binding.node_id);
  }
  task.depends_on = [...dependencies];
  task.dependency_variables = {};
  task.input_bindings = bindings || {};
  syncCodeSignature(task, task.input_bindings);
}

const taskVariableOptions = computed(() => {
  return nodeVariableOptions(selectedTask.value);
});
function nestedVariableOptions(nested) {
  const items = [...taskVariableOptions.value];
  const nestedTasks = selectedTask.value?.crew_tasks || [];
  const ancestors = ancestorNodeIds(nested, nestedTasks);
  for (const parent of nestedTasks.filter((item) => ancestors.has(item.id))) {
    for (const field of parent.output_variables || fixedOutputVariables('task')) {
      items.push({
        key: `nested:${parent.id}:${field.name}`,
        name: `${String(selectedTask.value?.id || 'crew_1').replaceAll('_', '')}.${String(parent.id).replaceAll('_', '')}.${field.name}`,
        label: `${parent.name}（${String(selectedTask.value?.id || 'crew_1').replaceAll('_', '')}.${String(parent.id).replaceAll('_', '')}）· ${field.name}`,
        source: 'node', node_id: parent.id, variable: field.name,
        value_type: field.value_type || 'object', description: field.description || '',
      });
    }
  }
  return [...new Map(items.map(item => [item.name, item])).values()];
}
const selectedAgent = computed(() =>
  workflow.value.agents.find((item) => item.id === selectedAgentId.value),
);
const activeProposalIndex = computed(() => {
  // A stale clarification from an earlier discovery turn must not hide a
  // newer inputs/architecture proposal restored from the session transcript.
  const currentClarification = confirmation.value?.clarification;
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const item = messages.value[index];
    if (
      item.proposal &&
      !item.clarification &&
      item.proposal.capability_card
    )
      return index;
    if (
      item.proposal &&
      !item.clarification &&
      !item.proposal.confirmed_stages.includes(item.proposal.stage)
    )
      return index;
  }
  if (currentClarification) return -1;
  return -1;
});
const attachedSkills = computed(() =>
  store.skills.filter((item) => selectedAgent.value?.skills?.includes(item.id)),
);
const attachedPlugins = computed(() =>
  store.plugins.filter((item) =>
    selectedAgent.value?.plugins?.includes(item.id),
  ),
);
const availableSkills = computed(() =>
  store.skills.filter(
    (item) => !selectedAgent.value?.skills?.includes(item.id),
  ),
);
const availablePlugins = computed(() =>
  store.plugins.filter(
    (item) => !selectedAgent.value?.plugins?.includes(item.id),
  ),
);
const attachedKnowledge = computed(() =>
  store.knowledge.filter((item) =>
    selectedAgent.value?.knowledge_base_ids?.includes(item.id),
  ),
);
const availableKnowledge = computed(() =>
  store.knowledge.filter(
    (item) => !selectedAgent.value?.knowledge_base_ids?.includes(item.id),
  ),
);
const decisionOptions = computed(() => {
  const values = [];
  for (const id of selectedTask.value?.depends_on || []) {
    const dependency = workflow.value.tasks.find((item) => item.id === id);
    if (dependency?.node_type === "router" && !dependency.router_rules?.length)
      values.push("matched", "not_matched");
    if (dependency?.human_feedback)
      values.push(...dependency.feedback_outcomes);
  }
  return [...new Set(values)];
});
const taskNodes = computed(() =>
  workflow.value.tasks.map((task, index) => ({
    id: task.id,
    type: "step",
    position: Object.keys(task.position || {}).length
      ? task.position
      : { x: 120 + index * CANVAS_COLUMN_GAP, y: CANVAS_TASK_Y },
    data: {
      task,
      agent: workflow.value.agents.find((item) => item.id === task.agent_id),
      model: modelName(task),
      type: typeInfo[task.node_type] || { label: "不支持的旧节点", title: "Legacy step" },
    },
    selected: task.id === selectedTaskId.value,
  })),
);
const agentNodes = computed(() =>
  workflow.value.agents.map((agent, index) => ({
    id: `agent:${agent.id}`,
    type: "agentDef",
    position: Object.keys(agent.position || {}).length
      ? agent.position
      : {
          x: 120 + index * CANVAS_COLUMN_GAP,
          y: CANVAS_AGENT_START_Y + index * CANVAS_AGENT_ROW_GAP,
        },
    data: {
      agent,
      model: agentModelName(agent),
      bindings:
        (agent.skills?.length || 0) +
        (agent.plugins?.length || 0) +
        (agent.knowledge_base_ids?.length || 0),
    },
    selected: agent.id === selectedAgentId.value,
  })),
);
const nodes = computed(() => [...agentNodes.value, ...taskNodes.value]);

const dependencyEdges = computed(() => {
  const dependencies = workflow.value.tasks.flatMap((task) => task.depends_on.flatMap((source) => {
    const upstream = workflow.value.tasks.find(item => item.id === source);
    if (upstream?.node_type === 'router' && Object.values(upstream.routes || {}).some(targets => targets.includes(task.id)))
      return Object.entries(upstream.routes || {}).filter(([, targets]) => targets.includes(task.id)).map(([branch]) => ({
        source, target: task.id, edgeType: 'route', branch,
        label: routerBranchLabel(upstream, branch),
      }));
    if (task.node_type === 'router') {
      const variables = routerConditionVariables(task, source);
      if (variables.length) return [{
        source, target: task.id, edgeType: 'router-condition',
        label: `条件变量：${variables.join('、')}`,
      }];
    }
    return [{ source, target: task.id, edgeType: 'dependency' }];
  }));
  return dependencies.map((edge) => {
    const task = workflow.value.tasks.find((item) => item.id === edge.target);
    return {
      id: edge.edgeType === 'route' ? `route:${edge.source}:${edge.branch}:${edge.target}` : `${edge.edgeType}:${edge.source}:${edge.target}`,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.edgeType === 'route' ? `route:${edge.branch}` : "context-out",
      targetHandle: "context-in",
      label: ['route', 'router-condition'].includes(edge.edgeType) ? edge.label : "上游",
      edgeType: edge.edgeType,
      branch: edge.branch,
    };
  });
});
const assignmentEdges = computed(() =>
  workflow.value.tasks.flatMap((task) => {
    if (task.node_type === "crew")
      return (task.crew_agent_ids || []).map((agentId) => ({
        id: `member:${agentId}:${task.id}`,
        source: `agent:${agentId}`,
        target: task.id,
        sourceHandle: "agent-out",
        targetHandle: "agent-in",
        edgeType: "member",
      }));
    return task.agent_id
      ? [
          {
            id: `assign:${task.agent_id}:${task.id}`,
            source: `agent:${task.agent_id}`,
            target: task.id,
            sourceHandle: "agent-out",
            targetHandle: "agent-in",
            edgeType: "assignment",
          },
        ]
      : [];
  }),
);
const edges = computed(() =>
  [...dependencyEdges.value, ...assignmentEdges.value].map((edge) => {
    const selected = edge.id === selectedEdgeId.value;
    const relation = edge.edgeType !== "dependency";
    return {
      ...edge,
      type: "default",
      selected,
      markerEnd: MarkerType.ArrowClosed,
      animated: false,
      style: {
        stroke: selected ? "#b84343" : relation ? "#5579a4" : "#7d8a80",
        strokeWidth: selected ? 2 : 1.4,
        strokeDasharray: relation ? "5 4" : undefined,
      },
    };
  }),
);
const graphNodes = ref([]);
const graphEdges = ref([]);
const flowInstance = ref(null);
let restoringEdges = false;
const selectedEdge = computed(() =>
  edges.value.find((item) => item.id === selectedEdgeId.value),
);
const builderReady = computed(() =>
  Boolean(
    selectedKind.value ||
    route.params.kind ||
    workflow.value.structure_confirmed,
  ),
);
const inspectorVisible = computed(() => Boolean(
  builderReady.value && (
    inspectorSettingsOpen.value ||
    selectedAgent.value ||
    selectedTask.value ||
    selectedEdge.value
  ),
));
const previewPrimaryInput = computed(
  () =>
    workflow.value.inputs.find((item) =>
      ["text", "long_text"].includes(item.input_type),
    ) || null,
);
const previewFileInputs = computed(() =>
  workflow.value.inputs.filter((item) =>
    ["file", "image"].includes(item.input_type),
  ),
);
const previewVariableInputs = computed(() =>
  workflow.value.inputs.filter(
    (item) =>
      item.name !== previewPrimaryInput.value?.name &&
      !["file", "image"].includes(item.input_type),
  ),
);
const previewSelectedFiles = computed(() => Object.values(previewFiles).flat());
const previewCanSend = computed(
  () =>
    !previewBusy.value &&
    !previewUploading.value &&
    Boolean(
      previewMessage.value.trim() ||
      previewSelectedFiles.value.length ||
      previewVariableInputs.value.some(previewHasValue),
    ),
);
const historyProjects = computed(() => {
  const remote = remoteSessions.value
    .filter((item) => !item.application_id)
    .map((item) => ({
      ...item,
      remote: true,
      description:
        item.description ||
        (item.status === "generated" ? "已生成编排" : "未完成的编排对话"),
    }));
  const saved = store.workflows.map((item) => ({
    id: item.id,
    application_id: item.id,
    session_id: item.studio_session?.id || item.session_id || '',
    name: item.name,
    description: item.description,
    kind: item.kind,
    status: item.status,
    created_at: item.created_at,
    updated_at: item.updated_at,
  }));
  const seen = new Set();
  const semanticKey = (item) =>
    item.application_id
      ? `app:${item.application_id}`
      : item.remote
        ? `session:${item.id}`
        : `app:${item.id}`;
  return [...saved, ...remote]
    .filter((item) => {
      const key = semanticKey(item);
      return !seen.has(key) && seen.add(key);
    })
    .sort((a, b) =>
      timestampValue(b.created_at || b.updated_at) -
      timestampValue(a.created_at || a.updated_at),
    )
    .slice(0, 8);
});
const fixedOutputVariables = (nodeType = 'agent') => {
  if (['task', 'agent', 'crew'].includes(nodeType)) return [
    { name: 'object', value_type: 'object', description: '模型生成的结构化 JSON 对象' },
    { name: 'file', value_type: 'file', description: '此节点实际生成或接收的文件列表' },
    { name: 'text', value_type: 'string', description: '面向用户的正文文本' },
  ];
  if (nodeType === 'tool') return [
    { name: 'object', value_type: 'object', description: '工具的结构化返回值' },
    { name: 'file', value_type: 'file', description: '工具实际生成的文件列表' },
    { name: 'text', value_type: 'string', description: '工具返回的文本表示' },
  ];
  if (nodeType === 'code') return [
    { name: 'output', value_type: 'object', description: 'main() 返回的字典对象' },
    { name: 'file', value_type: 'file', description: '代码节点实际生成的文件列表' },
    { name: 'text', value_type: 'string', description: 'output.text 的文本值（若存在）' },
  ];
  if (nodeType === 'router') return [
    { name: 'route', value_type: 'string', description: '命中的分支 ID' },
  ];
  return [];
};
const defaultCrewTask = (task, index = 0) => ({
  id: `task_${index + 1}`,
  name: `内部任务 ${index + 1}`,
  description: "完成 Crew 中的一项明确工作",
  expected_output: "可验证的任务结果",
  // Flow Crew members are selected by Agent→Crew edges. A nested task may be
  // assigned after a member is connected, but it must not start with a hidden
  // first-Agent default.
  agent_id: workflow.value.kind === "crew"
    ? task?.crew_agent_ids?.[0] || workflow.value.agents[0]?.id || null
    : null,
  depends_on: [],
  output_variables: fixedOutputVariables('task'),
  output_mode: 'text',
  dependency_variables: {},
  async_execution: false,
  markdown: false,
  output_file: "",
  create_directory: true,
});
function routerConditionVariables(task, sourceId) {
  const found = new Set();
  const visit = expression => {
    if (expression?.type === 'group') (expression.conditions || []).forEach(visit);
    else if (expression?.source === 'node' && expression.node_id === sourceId && expression.variable)
      found.add(expression.variable);
  };
  (task?.router_rules || []).forEach(rule => visit(rule.expression));
  return [...found];
}
function routerCases(task) {
  if (!task?.router_rules?.length) {
    const legacyBranches = Object.keys(task?.routes || {}).filter(id => id !== 'else');
    return [
      ...legacyBranches.map((id, index) => ({ id, label: index ? `CASE ${index + 1}` : 'IF', summary: task.condition || '旧版路由分支' })),
      { id: 'else', label: 'ELSE', summary: '其他情况' },
    ];
  }
  return [
    ...(task?.router_rules || []).map((rule, index) => ({
      id: rule.id, label: index ? `ELIF ${index + 1}` : 'IF',
      summary: routerExpressionSummary(rule.expression),
    })),
    { id: 'else', label: 'ELSE', summary: '其他情况' },
  ];
}
function routerExpressionSummary(expression, depth = 0) {
  const labels = {
    equals: '等于', not_equals: '不等于', contains: '包含', not_contains: '不包含',
    starts_with: '开头为', ends_with: '结尾为', greater_than: '大于', less_than: '小于',
    is_empty: '为空', is_not_empty: '不为空',
  };
  if (expression?.type === 'group') {
    const children = (expression.conditions || []).map(child => routerExpressionSummary(child, depth + 1)).filter(Boolean);
    const summary = children.length ? children.join(expression.operator === 'or' ? ' OR ' : ' AND ') : '未设置条件';
    return depth ? `(${summary})` : summary;
  }
  const variable = expression?.source === 'node'
    ? `${taskName(expression.node_id)} · ${expression.variable || '变量'}`
    : (runInputOptions.value.find(item => item.name === expression?.variable)?.label || expression?.variable || '运行输入');
  const operator = labels[expression?.operator] || expression?.operator || '等于';
  if (['is_empty', 'is_not_empty'].includes(expression?.operator)) return `${variable} ${operator}`;
  const value = expression?.value == null ? '' : typeof expression.value === 'object' ? JSON.stringify(expression.value) : String(expression.value);
  return `${variable} ${operator} ${value || '""'}`;
}
function routerBranchLabel(task, branch) {
  return routerCases(task).find(item => item.id === branch)?.label || branch;
}
function addRouterRule(task) {
  const variables = conditionVariableOptions(task);
  const selected = variables[0] || { source: 'input', variable: 'message', value_type: 'string' };
  const index = (task.router_rules || []).length + 1;
  task.router_rules ||= [];
  const id = `case_${Math.random().toString(36).slice(2, 9)}`;
  task.router_rules.push({
    id,
    expression: {
      type: 'group', operator: 'and', conditions: [{
        type: 'condition', source: selected.source, node_id: selected.node_id || '',
        variable: selected.variable, value_type: selected.value_type || 'string',
        operator: 'is_empty', value: '',
      }],
    },
  });
  task.routes ||= {};
  task.routes[id] ||= [];
}
function removeRouterRule(task, index) {
  const [removed] = task.router_rules.splice(index, 1);
  if (removed) delete task.routes?.[removed.id];
  if (removed) {
    const stillRouted = new Set(Object.values(task.routes || {}).flat());
    workflow.value.tasks.forEach(target => {
      if (!stillRouted.has(target.id))
        target.depends_on = (target.depends_on || []).filter(id => id !== task.id);
    });
  }
}
function updateRouterRules(task, rules) {
  const previousSources = new Set();
  const nextSources = new Set();
  const collectSources = (items, target) => {
    const visit = expression => {
      if (expression?.type === 'group') (expression.conditions || []).forEach(visit);
      else if (expression?.source === 'node' && expression.node_id) target.add(expression.node_id);
    };
    (items || []).forEach(rule => visit(rule.expression));
  };
  collectSources(task.router_rules, previousSources);
  collectSources(rules, nextSources);
  task.router_rules = rules;
  const dependencies = new Set(task.depends_on || []);
  previousSources.forEach(id => { if (!nextSources.has(id)) dependencies.delete(id); });
  nextSources.forEach(id => dependencies.add(id));
  task.depends_on = [...dependencies];
}
function updateRouterRule(task, index, expression) {
  const rules = [...(task.router_rules || [])];
  rules[index] = { ...rules[index], expression };
  updateRouterRules(task, rules);
}

function modelName(task) {
  const agent = workflow.value.agents.find((item) => item.id === task.agent_id);
  return agentModelName(agent);
}
function agentModelName(agent) {
  const id = agent?.model_profile_id || workflow.value.model_profile_id;
  return (
    store.models.find((item) => item.id === id)?.name ||
    store.defaultModel?.name ||
    "No model"
  );
}
function taskName(id) {
  return (
    workflow.value.tasks.find((item) => item.id === id)?.name ||
    workflow.value.agents.find((item) => `agent:${item.id}` === id)?.role ||
    id
  );
}
function fitCanvas(duration = 180) {
  nextTick(() =>
    window.setTimeout(
      () => fitView({ padding: 0.16, duration, maxZoom: 1.1 }),
      40,
    ),
  );
}
async function removeHistoryProject(item) {
  if (item.application_id) return;
  const id = String(item.id || "");
  if (!id) return;
  const currentSession =
    !route.params.id && String(route.query.session || "") === id;
  const previousSessions = remoteSessions.value;
  remoteSessions.value = remoteSessions.value.filter(
    (entry) => String(entry.id) !== id,
  );
  try {
    await api.deleteStudioSession(id);
    store.notify("历史对话已删除");
    if (currentSession) {
      const oldAttachments = attachments.value.splice(0);
      oldAttachments.forEach((attachment) =>
        api.deleteStudioAttachment(attachment.id).catch(() => {}),
      );
      await router.replace("/new-automation");
      hydrateWorkflow(emptyWorkflow());
    }
  } catch (error) {
    remoteSessions.value = previousSessions;
    store.error = error.message;
  }
}
async function openHistoryProject(item) {
  // A design session can be either an unbound draft or already linked to an
  // application. Keep the session id in the URL for both cases so returning
  // from another page restores its chat/proposal state instead of opening a
  // blank builder. The loader will use the linked application's draft when it
  // exists and the session proposal otherwise.
  if (item.remote || (!item.application_id && item.id)) {
    await router.push({ path: "/new-automation", query: { session: String(item.id) } });
    return;
  }
  if (item.application_id) {
    // The overview store may contain only the application summary. Resolve its
    // authoritative linked DesignSession before navigating, otherwise leaving
    // Studio and returning from Recent projects loses the in-progress chat.
    let sessionId = item.session_id || '';
    if (!sessionId) {
      try {
        const latest = await api.workflow(item.application_id);
        sessionId = latest?.studio_session?.id || '';
      } catch (_) { /* the application route remains a valid fallback */ }
    }
    const query = sessionId ? { session: String(sessionId) } : undefined;
    await router.push({ path: `/studio/${item.application_id}`, ...(query ? { query } : {}) });
    return;
  }
  await router.push(`/studio/${item.id}`);
}
async function startNewSession() {
  const oldAttachments = attachments.value.splice(0);
  oldAttachments.forEach((item) =>
    api.deleteStudioAttachment(item.id).catch(() => {}),
  );
  try {
    const created = await api.createStudioSession(
      route.params.kind || workflow.value.kind || "crew",
    );
    remoteSessions.value = [
      created,
      ...remoteSessions.value.filter(
        (item) => String(item.id) !== String(created.id),
      ),
    ];
    await router.replace({
      path: "/new-automation",
      query: { session: created.id },
    });
  } catch (error) {
    // Never leave a deleted session id in the URL when creation fails.
    store.error = error.message;
    await router.replace({
      path: "/new-automation",
      query: { fresh: Date.now().toString() },
    });
  }
}
function migrate(item) {
  const legacyTasks = (item.tasks || []).map(task => ({
    ...task, output_variables: [...(task.output_variables || [])],
  }));
  const fallback = item.agents[0]?.id || null;
  item.original_request ??= item.request || "";
  item.stage_summaries ??= {};
  item.planning ??= false;
  item.planning_model_profile_id ??= null;
  item.memory ??= false;
  item.cache ??= true;
  item.verbose ??= false;
  delete item.share_crew;
  item.output_log_file ??= "";
  item.manager_agent_id ??= null;
  item.manager_model_profile_id ??= null;
  item.max_method_calls ??= 100;
  item.interaction_mode =
    item.interaction_mode === "multi_turn" ? "multi_turn" : "single_run";
  item.node_name_counters ||= { agent: 0, crew: 0, node: 0, task: 0 };
  item.interaction ??= {};
  item.chat_history ??= [];
  item.inputs ??= [];
  item.structure_confirmed ??= true;
  item.draft_revision ??= null;
  item.agents = item.agents.map((agent, index) => ({
    max_execution_time: null,
    max_retry_limit: 2,
    max_reasoning_attempts: null,
    respect_context_window: true,
    multimodal: false,
    allow_code_execution: false,
    user_interaction: false,
    inject_date: false,
    date_format: "%Y-%m-%d",
    use_system_prompt: true,
    function_calling_model_profile_id: null,
    ...agent,
    position:
      agent.position || {
        x: 120 + index * CANVAS_COLUMN_GAP,
        y: CANVAS_AGENT_START_Y + index * CANVAS_AGENT_ROW_GAP,
      },
  }));
  item.tasks = item.tasks.map((task) => ({
    ...task,
    node_type:
      item.kind === "crew"
        ? "task"
        : task.node_type === "task"
          ? "agent"
          : task.node_type,
    crew_process:
      task.crew_process === "hierarchical" ? "hierarchical" : "sequential",
    crew_memory: Boolean(task.crew_memory),
    crew_planning: Boolean(task.crew_planning),
    crew_cache: task.crew_cache ?? true,
    crew_output_log_file: task.crew_output_log_file || "",
    crew_manager_agent_id: task.crew_manager_agent_id || null,
    crew_manager_model_profile_id: task.crew_manager_model_profile_id || null,
    crew_planning_model_profile_id: task.crew_planning_model_profile_id || null,
    crew_verbose: Boolean(task.crew_verbose),
    agent_id:
      item.kind === "crew" && item.process === "hierarchical"
        ? null
      : item.kind === "crew"
        ? task.agent_id || fallback
      : item.kind === "flow" && ["router", "code", "tool", "crew"].includes(task.node_type)
          ? null
          : task.agent_id,
    crew_agent_ids:
      item.kind === "crew" && task.node_type === "crew" && !(task.crew_agent_ids || []).length
        ? item.agents.map((agent) => agent.id)
        : task.crew_agent_ids || [],
    output_variables: fixedOutputVariables(task.node_type),
    output_mode: task.output_mode === 'json' ? 'json' : 'text',
    dependency_variables: task.dependency_variables || {},
    async_execution: task.async_execution || false,
    human_feedback: (item.kind === "flow" && task.human_feedback) || false,
    feedback_message: task.feedback_message || "Please review this step output",
    feedback_outcomes: task.feedback_outcomes?.length
      ? task.feedback_outcomes
      : ["approved", "revise"],
    feedback_default_outcome: task.feedback_default_outcome || null,
    markdown: task.markdown || false,
    output_file: task.output_file || "",
    create_directory: task.create_directory ?? true,
    guardrail: "",
    guardrail_max_retries: 0,
    crew_tasks: (task.crew_tasks || []).map((nested, index) => ({
      ...defaultCrewTask(task, index),
      ...nested,
      agent_id: task.crew_process === 'hierarchical' ? null : (nested.agent_id || null),
      depends_on: Array.isArray(nested.depends_on) ? nested.depends_on : [],
      output_variables: fixedOutputVariables('task'),
      output_mode: nested.output_mode === 'json' ? 'json' : 'text',
      dependency_variables: {},
      create_directory: nested.create_directory ?? true,
      guardrail: "",
      guardrail_max_retries: 0,
    })),
  }));
  item.node_name_counters ||= { agent: 0, crew: 0, node: 0, task: 0 };
  item.tasks.forEach((task) => {
    if (task.node_type === 'crew') syncCrewOutputMode(task);
    delete task.crew_share_crew;
    (task.crew_tasks || []).forEach((nested) => delete nested.crew_share_crew);
    if (item.kind === "flow" && ["router", "code", "tool"].includes(task.node_type))
      task.agent_id = null;
    if (task.node_type === 'crew' && task.crew_process === 'hierarchical') {
      (task.crew_tasks || []).forEach((nested) => { nested.agent_id = null; });
    }
    if (['code', 'tool'].includes(task.node_type)) {
      const bindings = migrateDeterministicBindings(task, legacyTasks);
      task.input_bindings = bindings;
      task.dependency_variables = {};
      syncCodeSignature(task, bindings);
    } else task.dependency_variables = {};
  });
  // Older generated graphs stored Agents in a left vertical rail and Tasks
  // in a right vertical rail. Reflow that legacy shape once when it enters
  // the builder; manually moved nodes keep their persisted coordinates.
  const taskXs = new Set(item.tasks.map((task) => Number(task.position?.x)));
  const legacyLayout = item.tasks.length > 1 && taskXs.size <= 2 &&
    item.tasks.some((task) => Number(task.position?.x) >= 350);
  if (legacyLayout) {
    item.tasks.forEach((task, index) => {
      task.position = { x: 120 + index * CANVAS_COLUMN_GAP, y: CANVAS_TASK_Y };
    });
    const columnRows = new Map();
    item.agents.forEach((agent, index) => {
      const taskIndex = item.tasks.findIndex((task) =>
        task.agent_id === agent.id || (task.crew_agent_ids || []).includes(agent.id),
      );
      const column = taskIndex >= 0 ? taskIndex : index;
      const row = columnRows.get(column) || 0;
      columnRows.set(column, row + 1);
      agent.position = {
        x: 120 + column * CANVAS_COLUMN_GAP,
        y: CANVAS_AGENT_START_Y + row * CANVAS_AGENT_ROW_GAP,
      };
    });
  }
  const delegationManagers = new Set();
  if (item.kind === 'crew' && item.process === 'hierarchical' && item.manager_agent_id)
    delegationManagers.add(item.manager_agent_id);
  item.tasks.forEach((task) => {
    if (task.node_type !== 'crew' || task.crew_process !== 'hierarchical') return;
    const managerId = task.crew_manager_agent_id ||
      (!task.crew_manager_model_profile_id ? task.crew_agent_ids?.[0] : null);
    if (managerId) delegationManagers.add(managerId);
  });
  item.agents.forEach((agent) => {
    if (!delegationManagers.has(agent.id)) agent.allow_delegation = false;
  });
  item.agents.forEach((agent) => { delete agent.code_execution_mode; });
  return item;
}
function hydrateWorkflow(loaded, proposal = null) {
  hydrating = true;
  workflow.value = migrate(loaded);
  // An ungenerated application has no useful canvas to inspect yet. Open the
  // natural-language designer automatically when entering such a session.
  if (!workflow.value.structure_confirmed) assistantOpen.value = true;
  markWorkflowPersisted(workflow.value);
  if (proposal?.draft_sync?.manual_changes?.length)
    syncedManualChanges.value = cloneDraftValue(proposal.draft_sync.manual_changes);
  confirmation.value = migrateProposal(proposal);
  kindPreselected.value = Boolean(
    route.params.kind || confirmation.value?.kind_preselected,
  );
  selectedKind.value =
    route.params.kind ||
    (loaded.structure_confirmed
      ? loaded.kind
      : confirmation.value?.kind_preselected
        ? confirmation.value.recommended_kind
      : confirmation.value?.confirmed_stages?.includes("architecture")
        ? confirmation.value.recommended_kind
        : null);
  messages.value = loaded.chat_history?.length
    ? loaded.chat_history.map((item) => ({
        role: item.error ? "error" : item.role,
        text: item.content,
        jobId: item.job_id || "",
        status: item.error ? "failed" : (item.status || ""),
        error: item.error || "",
        attachments: item.attachments || [],
        proposal: migrateProposal(item.proposal),
        clarification: migrateClarification(item.clarification),
        clarificationAnswer: item.clarificationAnswer || "",
        pending: false,
      }))
    : [
        {
          role: "assistant",
          text: "你好，我可以和你一起设计可发布、可复用的 CrewAI 智能体。请描述目标，也可以上传参考文件。",
        },
      ];
  if (confirmation.value) {
    const target = [...messages.value]
      .reverse()
      .find((item) => item.role === "assistant" && item.proposal) ||
      [...messages.value].reverse().find((item) => item.role === "assistant");
    if (target) {
      target.proposal = confirmation.value;
      target.clarification = migrateClarification(
        confirmation.value.clarification,
      );
    }
  }
  draftDirty = false;
  nextTick(() => {
    hydrating = false;
  });
  clearSelection();
  fitCanvas();
}

function mapStudioMessage(item) {
  return {
    role: item.error ? "error" : item.role,
    text: item.content || "",
    jobId: item.job_id || "",
    status: item.error ? "failed" : (item.status || ""),
    error: item.error || "",
    attachments: item.attachments || [],
    proposal: migrateProposal(item.proposal),
    clarification: migrateClarification(item.clarification),
    clarificationAnswer: item.clarificationAnswer || "",
    pending: false,
  };
}

function setStudioHistoryPage(page, { replace = true } = {}) {
  const incoming = (page?.messages || []).map(mapStudioMessage);
  messages.value = replace ? incoming : [...incoming, ...messages.value];
  historyBefore.value = Number(page?.next_before ?? historyBefore.value ?? incoming.length) || 0;
  historyHasMore.value = Boolean(page?.has_more);
}

async function loadOlderStudioMessages() {
  if (historyLoading.value || !historyHasMore.value || !historySessionId.value) return;
  const thread = assistantThread.value;
  if (!thread) return;
  historyLoading.value = true;
  const oldHeight = thread.scrollHeight;
  const oldTop = thread.scrollTop;
  try {
    const page = await api.studioSessionMessages(
      historySessionId.value, 30, historyBefore.value,
    );
    setStudioHistoryPage(page, { replace: false });
    await nextTick();
    // Prepending must keep the message currently being read at the same
    // viewport position instead of jumping to the newly loaded page.
    thread.scrollTop = thread.scrollHeight - oldHeight + oldTop;
    assistantFollowBottom = false;
  } catch (error) {
    store.error = error?.message || String(error);
  } finally {
    historyLoading.value = false;
  }
}

async function loadWorkflow() {
  if (route.query.session) {
    try {
      const session = await api.studioSession(route.query.session);
      const persisted = session.application_id
        ? await api.workflow(session.application_id)
        : null;
      await restoreStudioSession(session, persisted);
      if (session.application_id) {
        await router.replace(`/studio/${session.application_id}`);
      }
      return;
    } catch (error) {
      // Older builds incorrectly used the numeric application ID as the
      // session query value.  Recover those URLs from the authoritative app
      // draft instead of showing an empty builder.
      if (/^\d+$/.test(String(route.query.session))) {
        try {
          hydrateWorkflow(await api.workflow(route.query.session));
          return;
        } catch (_) {
          store.error = error.message;
        }
      } else {
        store.error = error.message;
      }
    }
  }
  let found = store.workflows.find(
    (item) => String(item.id) === String(route.params.id || ""),
  );
  if (found) {
    try {
      found = await api.workflow(route.params.id);
    } catch (_) {
      found = JSON.parse(JSON.stringify(found));
    }
    const session = found.studio_session;
    if (session) await restoreStudioSession(session, found);
    else {
      studioSessionId.value = "";
      hydrateWorkflow(JSON.parse(JSON.stringify(found)));
    }
    return;
  }
  if (route.params.id && /^\d+$/.test(String(route.params.id))) {
    try {
      const found = await api.workflow(route.params.id);
      if (found.studio_session)
        await restoreStudioSession(found.studio_session, found);
      else {
        studioSessionId.value = "";
        hydrateWorkflow(found);
      }
      return;
    } catch (error) {
      store.error = error.message;
    }
  }
  hydrateWorkflow(emptyWorkflow());
  studioSessionId.value = "";
}

async function restoreStudioSession(session, persistedWorkflow = null) {
  studioSessionId.value = String(session.id || "");
  historySessionId.value = studioSessionId.value;
  historyBefore.value = 0;
  historyHasMore.value = false;
  const loaded = persistedWorkflow
    ? JSON.parse(JSON.stringify(persistedWorkflow))
    : emptyWorkflow();
  if (!persistedWorkflow) {
    loaded.id = session.id;
    loaded.name = session.name;
    loaded.kind = session.kind;
  }
  loaded.chat_history = session.messages || loaded.chat_history || [];
  // Once a session is attached to an existing application, the application
  // draft is authoritative.  The session still supplies chat history, but a
  // stale proposal must not reopen an old confirmation card or reset Agent
  // switches such as ask_user.
  hydrateWorkflow(loaded, loaded.structure_confirmed ? null : session.proposal);
  if (session.id) {
    try {
      const transcript = await api.studioSessionMessages(session.id, 30);
      loaded.chat_history = transcript.messages || [];
      workflow.value.chat_history = loaded.chat_history;
      setStudioHistoryPage(transcript);
      // The linked application's canvas is authoritative, but an active
      // unconfirmed architecture/input proposal still belongs to the chat
      // transcript and must remain actionable after restore.
      const latestProposal = [...messages.value]
        .reverse()
        .find((item) => item.proposal && ['inputs', 'architecture'].includes(item.proposal.stage)
          && !item.proposal.confirmed_stages.includes(item.proposal.stage));
      if (latestProposal) confirmation.value = latestProposal.proposal;
    } catch (_) {
      // The canvas remains usable when transcript loading is unavailable.
    }
  }
  const active = session.active_job || {};
  const pendingJob = ["queued", "planning"].includes(active.status) && active.job_id;
  if (pendingJob) {
    const answer = [...messages.value]
      .reverse()
      .find((item) => item.role === "assistant" && item.jobId === active.job_id);
    if (answer) answer.pending = true;
  }
  // Do not await the running job: it can take minutes and the page must show
  // the canvas and transcript right away.  The job keeps streaming into its
  // assistant message in the background.
  if (pendingJob)
    resumeStudioJob(active.job_id, session.id).catch((error) => {
      store.error = error?.message || String(error);
    });
  // A linked application's persisted draft is authoritative. A historical
  // job result can belong to an older canvas revision and must not overwrite
  // manually edited nodes when the session is reopened.
  else if (!persistedWorkflow && active.result?.workflow)
    applyStudioResult(active.result, null);
  if (session.application_id && /^\d+$/.test(String(session.application_id))) {
    hydrating = true;
    workflow.value.id = String(session.application_id);
    await nextTick();
    hydrating = false;
  }
}

function migrateProposal(value) {
  if (
    !value ||
    typeof value !== "object" ||
    value.intent === "conversation" ||
    ![
      value.clarification,
      value.capability_card,
      value.preflight,
      value.resource_selection_confirmed,
      value.interaction_mode_preselected,
      value.kind_preselected,
      value.kind_confirmed,
      value.structure_confirmed,
      ...(Array.isArray(value.resolved_clarifications)
        ? value.resolved_clarifications
        : Object.keys(value.resolved_clarifications || {})),
      ...(Array.isArray(value.confirmed_stages) ? value.confirmed_stages : []),
      ...(Array.isArray(value.inputs) ? value.inputs : []),
      ...(Array.isArray(value.agents) ? value.agents : []),
      ...(Array.isArray(value.tasks) ? value.tasks : []),
    ].some(Boolean)
  )
    return null;
  const rawRequirements = Array.isArray(value.capability_requirements)
    ? value.capability_requirements
    : [];
  const selectedToolIds = new Set(
    rawRequirements
      .filter((item) => item.resource_type === "tool")
      .flatMap((item) => item.selected_ids || [])
      .map((id) => String(id)),
  );
  (value.tools || []).forEach((id) => selectedToolIds.add(String(id)));
  (value.agents || []).forEach((agent) =>
    (agent.plugins || agent.tools || []).forEach((id) => selectedToolIds.add(String(id))),
  );
  const retrievalToolSelected = [...selectedToolIds].some((id) => {
    const tool = store.plugins.find((item) => String(item.id) === id);
    const text = `${tool?.name || ""} ${tool?.description || ""}`.toLowerCase();
    return tool?.kind === "mcp_http" || tool?.kind === "mcp_sse" ||
      /knowledge|知识|rag|检索|retrieve/.test(text);
  });
  const clarification = migrateClarification(value.clarification);
  const clarificationText = `${clarification?.id || ""} ${clarification?.question || ""}`;
  const resourceClarification = /knowledge|知识库|平台知识|普通知识|绑定|skill|工具|mcp/i.test(clarificationText);
  const displayRequirements = rawRequirements.length || retrievalToolSelected || !resourceClarification
    ? rawRequirements
    : [{
        id: "platform_knowledge",
        resource_type: "knowledge",
        label: "平台知识库",
        reason: "当前方案需要可检索的知识库资源，请从工作空间中选择或新建一个知识库。",
        required: true,
        selected_ids: [],
      }];
  const normalizedClarification =
    resourceClarification && !value.preflight
      ? null
      : clarification;
  const interactionMode =
    value.interaction_mode === "multi_turn" ? "multi_turn" : "single_run";
  const rawInputs = Array.isArray(value.inputs) ? value.inputs : [];
  const existingMessage = rawInputs.find((item) =>
    ["message", "dialogue_message"].includes(item.name),
  );
  const additionalInputs = rawInputs
    .filter((item) => !["message", "dialogue_message"].includes(item.name))
    .filter(
      (item) =>
        interactionMode !== "multi_turn" ||
        ["file", "image"].includes(item.input_type),
    );
  const normalizedInputs = [
    {
      name: "message",
      label: existingMessage?.label || "用户需求",
      input_type: "long_text",
      required: true,
      description:
        existingMessage?.description ||
        "用户本轮对智能体的需求描述，通过 {message} 传入执行流程。",
      multiple: false,
    },
    ...additionalInputs.map((item) => ({
      name: item.name || "input",
      label: item.label || item.name || "输入",
      input_type: item.input_type || "text",
      required: item.required !== false,
      description: item.description || "",
      multiple: Boolean(item.multiple),
    })),
  ];
  return {
    title: value.title || "",
    request: value.request || "",
    original_request: value.original_request || value.request || "",
    stage_summaries:
      value.stage_summaries && typeof value.stage_summaries === "object"
        ? JSON.parse(JSON.stringify(value.stage_summaries))
        : {},
    summary: value.summary || "",
    interaction_mode: interactionMode,
    interaction_mode_preselected: Boolean(value.interaction_mode_preselected),
    interaction:
      value.interaction && typeof value.interaction === "object"
        ? JSON.parse(JSON.stringify(value.interaction))
        : {},
    recommended_kind:
      (value.recommended_kind || value.kind) === "crew" ? "crew" : "flow",
    recommended_process: [
      "sequential",
      "hierarchical",
    ].includes(value.recommended_process)
      ? value.recommended_process
      : value.recommended_kind === "flow"
        ? "sequential"
        : "sequential",
    process_reason: value.process_reason || "",
    architecture_reason: value.architecture_reason || "",
    agents: Array.isArray(value.agents)
      ? value.agents.map((item) => ({
          id: item.id || "",
          role: item.role || "未命名 Agent",
          purpose: item.purpose || item.goal || "",
          goal: item.goal || item.purpose || "",
          backstory: item.backstory || item.context || "",
          responsibilities: Array.isArray(item.responsibilities)
            ? item.responsibilities
            : [],
          tools: Array.isArray(item.tools) ? item.tools : [],
          skills: Array.isArray(item.skills) ? item.skills : [],
          plugins: Array.isArray(item.plugins) ? item.plugins : [],
          memory: Boolean(item.memory),
          reasoning: Boolean(item.reasoning),
          ...(Object.hasOwn(item, 'user_interaction')
            ? { user_interaction: Boolean(item.user_interaction) } : {}),
          ...(Object.hasOwn(item, 'allow_code_execution')
            ? { allow_code_execution: Boolean(item.allow_code_execution) } : {}),
          knowledge_base_ids: Array.isArray(item.knowledge_base_ids)
            ? item.knowledge_base_ids
            : [],
        }))
      : [],
    tasks: Array.isArray(value.tasks)
      ? value.tasks.map((item) => ({
          id: item.id || "",
          name: item.name || "未命名任务",
          objective: (item.objective || "").replaceAll(
            "{dialogue_message}",
            "{message}",
          ),
          agent_id: item.agent_id || null,
          agent_role: item.agent_role || "",
          depends_on: Array.isArray(item.depends_on) ? item.depends_on : [],
          expected_output: item.expected_output || "",
          node_type:
            item.node_type ||
            (value.recommended_kind === "flow" ? "agent" : "task"),
          crew_agent_ids: Array.isArray(item.crew_agent_ids)
            ? item.crew_agent_ids
            : [],
          crew_tasks: Array.isArray(item.crew_tasks) ? item.crew_tasks : [],
          crew_process: ["sequential", "hierarchical"].includes(
            item.crew_process,
          )
            ? item.crew_process
            : null,
        }))
      : [],
    tools: Array.isArray(value.tools) ? value.tools : [],
    memory: Boolean(value.memory),
    planning: Boolean(value.planning),
    capability_requirements: displayRequirements
      .filter((item) => !(retrievalToolSelected && item.resource_type === "knowledge" && !(item.selected_ids || []).length))
      .map((item) => ({
          id: item.id,
          resource_type: item.resource_type,
          label: item.label,
          reason: item.reason,
          required: item.required !== false,
          selected_ids: Array.isArray(item.selected_ids)
            ? item.selected_ids
            : [],
        })),
    capability_blocked: Array.isArray(value.capability_blocked)
      ? value.capability_blocked.map((item) => ({
          id: item.id, resource_type: item.resource_type, label: item.label,
          reason: item.reason, required: item.required !== false,
          selected_ids: Array.isArray(item.selected_ids) ? item.selected_ids : [],
        }))
      : [],
    capability_card: Boolean(value.capability_card || (
      value.stage === "generation" && displayRequirements
        .filter((item) => !(retrievalToolSelected && item.resource_type === "knowledge" && !(item.selected_ids || []).length))
        .some((item) =>
        item.required !== false && !(item.selected_ids || []).length)
    )),
    confirmation_prompt:
      value.confirmation_prompt ||
      "确认以上架构方案后，我再生成可运行的 CrewAI 编排。",
    stage: ["discovery", "inputs", "architecture", "generation"].includes(value.stage)
      ? value.stage
      : "inputs",
    preflight: Boolean(value.preflight),
    confirmed_stages: Array.isArray(value.confirmed_stages)
      ? value.confirmed_stages.filter((item) =>
          ["inputs", "architecture", "generation"].includes(item),
        )
      : [],
    kind_confirmed: Array.isArray(value.confirmed_stages) &&
      value.confirmed_stages.includes("architecture"),
    kind_preselected: Boolean(value.kind_preselected),
    clarification: normalizedClarification,
    resolved_clarifications:
      value.resolved_clarifications &&
      typeof value.resolved_clarifications === "object"
        ? { ...value.resolved_clarifications }
        : {},
    inputs: normalizedInputs,
    notes: Array.isArray(value.notes) ? value.notes : [],
  };
}

function migrateClarification(value) {
  if (
    !value ||
    typeof value !== "object" ||
    !value.question ||
    !Array.isArray(value.options)
  )
    return null;
  return {
    id: value.id || "clarification",
    question: value.question,
    options: value.options.map((item) => ({
      label: item.label || item.value,
      value: item.value || item.label,
      description: item.description || "",
      recommended: Boolean(item.recommended),
      patch:
        item.patch && typeof item.patch === "object"
          ? JSON.parse(JSON.stringify(item.patch))
          : {},
    })),
    allow_custom: value.allow_custom !== false,
    locked: Boolean(value.locked),
    selected: value.selected || "",
    selectedLabel: value.selectedLabel || "",
  };
}

onMounted(async () => {
  store.loadResources();
  api.studioSessions().then((sessions) => { remoteSessions.value = sessions; }).catch(() => {});
  try {
    // App.vue already starts the shared workspace load. The editor only needs
    // its selected workflow before showing the canvas; overview and session
    // history continue loading in the background.
    await loadWorkflow();
    if (route.name === "studio-new" && route.params.kind)
      openCreateDetails(route.params.kind);
  } finally {
    pageLoading.value = false;
  }
  window.addEventListener("keydown", handleDeleteKey, true);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleDeleteKey, true);
  attachments.value.forEach((item) =>
    api.deleteStudioAttachment(item.id).catch(() => {}),
  );
  if (draftTimer) window.clearTimeout(draftTimer);
  if (previewPollTimer) window.clearTimeout(previewPollTimer);
  previewRunAbortController?.abort();
});
watch(
  () => [
    route.params.id,
    route.params.kind,
    route.query.session,
    route.query.fresh,
  ],
  async () => {
    // Saving a new workflow only changes its URL. Reloading the same object here
    // would discard transient UI state such as confirmations and preview drawers.
    if (
      route.params.id &&
      String(route.params.id) === String(workflow.value.id)
    )
      return;
    if (busy.value && String(route.query.session || "") === String(workflow.value.id))
      return;
    pageLoading.value = true;
    try { await loadWorkflow(); }
    finally { pageLoading.value = false; }
  },
);
watch(
  () => store.models.length,
  () => {
    if (!workflow.value.model_profile_id)
      workflow.value.model_profile_id = store.defaultModel?.id || null;
  },
);
watch(selectedAgentId, () => {
  capabilityPickerOpen.value = false;
});
watch(
  [selectedAgentId, selectedTaskId, selectedEdgeId],
  ([agentId, taskId, edgeId]) => {
    if (agentId || taskId || edgeId) inspectorSettingsOpen.value = false;
  },
);
function configureAgentInteraction(agent) {
  if (!agent.user_interaction) return;
  if (workflow.value.interaction_mode !== "multi_turn") {
    agent.user_interaction = false;
    store.error = "请先把应用交互方式改为分步骤对话";
    return;
  }
  if (
    workflow.value.kind === "crew" &&
    workflow.value.process === "hierarchical" &&
    workflow.value.manager_agent_id !== agent.id
  ) {
    agent.user_interaction = false;
    store.error = "层级 Crew 只能由管理 Agent 启用 ask_user";
    return;
  }
  const managedCrew = workflow.value.tasks.find(
    (task) =>
      task.node_type === "crew" &&
      task.crew_process === "hierarchical" &&
      task.crew_agent_ids.includes(agent.id) &&
      task.crew_agent_ids[0] !== agent.id,
  );
  if (managedCrew) {
    agent.user_interaction = false;
    store.error = "Flow 内层级 Crew 只允许第一个管理 Agent 启用 ask_user";
    return;
  }
}

function changeInteractionMode(mode) {
  if (mode === "multi_turn") workflow.value.interaction_mode = "multi_turn";
  else workflow.value.interaction_mode = "single_run";
  if (workflow.value.interaction_mode === "multi_turn") {
    // Keep every confirmed business field when switching to a conversational
    // application. ask_user only collects text and files, so typed contracts
    // are converted to text for the Agent to parse instead of being silently
    // deleted from the workflow.
    workflow.value.inputs = workflow.value.inputs.map((input) => {
      if (!["number", "boolean", "json", "image"].includes(input.input_type)) return input;
      const original = input.input_type;
      const hint = original === "image"
        ? "用户通过文件上传提供图片材料。"
        : `用户以文字提供，由 Agent 理解、校验并转换为 ${original}。`;
      const description = input.description || "";
      return {
        ...input,
        input_type: original === "image" ? "file" : original === "json" ? "long_text" : "text",
        multiple: original === "image" ? input.multiple : false,
        description: description.includes(hint)
          ? description
          : `${description}${description ? "；" : ""}${hint}`,
      };
    });
  }
  if (workflow.value.interaction_mode === "single_run") {
    workflow.value.agents.forEach((agent) => {
      agent.user_interaction = false;
    });
    workflow.value.interaction ||= {};
    workflow.value.interaction.interactive_task_ids = [];
  } else if (workflow.value.kind === "crew" && workflow.value.process === "hierarchical") {
    const managerId = workflow.value.manager_agent_id || workflow.value.agents[0]?.id;
    setManager(managerId);
  }
}
watch(
  nodes,
  (value) => {
    graphNodes.value = value;
  },
  // Node data contains the complete reactive task/agent definition. A deep
  // watcher walks every nested condition and input on every edit, then gives
  // Vue Flow a fresh copy of the entire canvas. The node list itself is the
  // structural/position contract; nested data updates are reactive already.
  { immediate: true },
);
watch(
  edges,
  (value) => {
    nextTick(() =>
      window.setTimeout(() => {
        graphEdges.value = value;
        syncEdges(value);
      }, 60),
    );
  },
  { immediate: true },
);
watch(workflow, () => scheduleDraft(), { deep: true });
  watch(
  messages,
  (value) => {
    if (hydrating) return;
    // Chat transcript is persisted by the Studio session API. Keep it out of
    // the application draft watcher: streaming every Composer delta must not
    // enqueue canvas saves or advance draft_revision.
    scrollChat();
  },
  { deep: true },
);
watch(
  confirmation,
  () => {
    scrollChat();
    if (!hydrating) scheduleDraft();
  },
  { deep: true },
);

let assistantFollowBottom = true;
let assistantScrollTimer = null;
function onAssistantScroll() {
  const element = assistantThread.value;
  if (element) {
    assistantFollowBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 96;
    if (element.scrollTop <= 24) loadOlderStudioMessages();
  }
}
function scrollChat(force = false) {
  if (force) assistantFollowBottom = true;
  if (!assistantFollowBottom) return;
  if (assistantScrollTimer) window.clearTimeout(assistantScrollTimer);
  assistantScrollTimer = window.setTimeout(() => {
    assistantScrollTimer = null;
    nextTick(() => {
      if (assistantThread.value && assistantFollowBottom)
        assistantThread.value.scrollTop = assistantThread.value.scrollHeight;
    });
  }, 32);
}
function keepUploadStatusVisible(startedAt, minimumMs = 450) {
  return new Promise((resolve) =>
    window.setTimeout(
      resolve,
      Math.max(0, minimumMs - (Date.now() - startedAt)),
    ),
  );
}

function scheduleDraft() {
  if (hydrating) return;
  if (workflow.value.structure_confirmed && persistedWorkflowSnapshot &&
      JSON.stringify(trackedDraftFields.map((field) => workflow.value[field])) ===
      JSON.stringify(trackedDraftFields.map((field) => persistedWorkflowSnapshot[field]))) {
    draftDirty = false;
    if (saveState.value === "Unsaved changes" || saveState.value === "Saving draft...")
      saveState.value = "Draft saved";
    return;
  }
  draftEditVersion += 1;
  draftDirty = true;
  saveState.value = "Unsaved changes";
  if (draftTimer) window.clearTimeout(draftTimer);
  draftTimer = window.setTimeout(autoSave, 900);
}
async function autoSave() {
  if (!draftDirty) return;
  if (busy.value) {
    draftTimer = window.setTimeout(autoSave, 500);
    return;
  }
  if (!workflow.value.structure_confirmed) {
    if (!route.query.session || !confirmation.value) {
      saveState.value = "等待发送";
      return;
    }
    try {
      await api.updateStudioSession(workflow.value.id, {
        proposal: confirmation.value,
        kind: selectedKind.value || workflow.value.kind,
        title: workflow.value.name,
      });
      draftDirty = false;
      saveState.value = "方案已保存";
    } catch (error) {
      saveState.value = "保存失败";
      store.error = error.message;
    }
    return;
  }
  try {
    await persistWorkflowDraft();
    draftSaveInvalid = false;
  } catch (error) {
    draftDirty = true;
    draftSaveInvalid = error?.status === 422;
    saveState.value = draftSaveInvalid ? "草稿有错误，可发送消息修复" : "Draft pending";
    store.error = error.message;
  }
}

async function persistWorkflowDraft() {
  if (saveInFlight) {
    await saveInFlight;
    if (!draftDirty) return workflow.value;
  }
  const version = draftEditVersion;
  const submitted = cloneDraftValue(workflow.value);
  const baseline = cloneDraftValue(persistedWorkflowSnapshot || submitted);
  const manualChanges = currentManualChanges();
  saveState.value = "Saving draft...";
  const task = (async () => {
    let saved;
    try {
      saved = migrate(await api.saveWorkflow(submitted, manualChanges));
    } catch (error) {
      if (error.status !== 409 || !/^\d+$/.test(String(submitted.id || ''))) throw error;
      const latest = migrate(await api.workflow(submitted.id));
      const { merged, conflicts } = mergeDraft(baseline, submitted, latest);
      if (conflicts.length)
        throw new Error(`草稿中的 ${conflicts.join('、')} 已在别处修改，请刷新后核对这些字段`);
      saved = migrate(await api.saveWorkflow(merged, manualChanges));
    }
    const hadLaterEdits = draftEditVersion !== version;
    hydrating = true;
    if (hadLaterEdits) {
      workflow.value.id = saved.id;
      workflow.value.updated_at = saved.updated_at;
      workflow.value.draft_revision = saved.draft_revision;
      workflow.value.draft_sync = saved.draft_sync;
    } else workflow.value = saved;
    markWorkflowPersisted(saved);
    await nextTick();
    hydrating = false;
    draftDirty = hadLaterEdits;
    saveState.value = hadLaterEdits ? "Unsaved changes" : "Draft saved";
    if (hadLaterEdits) scheduleDraft();
    const index = store.workflows.findIndex((item) => String(item.id) === String(saved.id));
    if (index >= 0) store.workflows.splice(index, 1, saved);
    else store.workflows.unshift(saved);
    if (String(route.params.id || '') !== String(saved.id))
      await router.replace(`/studio/${saved.id}`);
    return saved;
  })();
  saveInFlight = task;
  try { return await task; }
  finally { if (saveInFlight === task) saveInFlight = null; }
}

function syncEdges(value, instance = flowInstance.value) {
  if (!instance) return;
  restoringEdges = true;
  instance.setEdges(value);
  restoringEdges = false;
}
function paneReady(instance) {
  flowInstance.value = instance;
  nextTick(() => window.setTimeout(() => syncEdges(edges.value, instance), 60));
}

async function executeStudioRequest(
  text,
  answer,
  history,
  attachmentIds,
  options = {},
) {
  let finalResult = null;
  // A blank canvas is rendered as Crew, but that is only a visual default.
  // Do not send it as a user choice or discovery will skip the kind question.
  const requestKind =
    options.kind ||
    selectedKind.value ||
    (builderReady.value ? workflow.value.kind : "auto");
  const pending = await api.studioChat({
    message: text,
    kind: requestKind,
    history,
    orchestration_id: String(workflow.value.id),
    model_profile_id: workflow.value.model_profile_id,
    model: store.defaultModel?.model || workflow.value.model,
    attachment_ids: attachmentIds,
    current_workflow: workflow.value,
    confirmed: options.confirmed || false,
    input_contract: options.inputContract || confirmation.value?.inputs || [],
    removed_input_names:
      options.removedInputNames || [...removedContractInputNames],
    proposal: options.proposal || confirmation.value || null,
    kind_preselected:
      options.kindPreselected === undefined
        ? kindPreselected.value
        : Boolean(options.kindPreselected),
    confirmation_stage: options.confirmationStage || null,
    clarification_id: options.clarificationId || "",
    clarification_value: options.clarificationValue || "",
    action: options.action || "message",
    manual_changes: currentManualChanges(),
  });
  if (pending.intent === "conversation") {
    answer.text = pending.reply || "你好！";
    answer.streaming = false;
    answer.routerOnly = true;
    await ensureStudioSessionRoute();
    return;
  }
  answer.jobId = pending.job_id;
  await ensureStudioSessionRoute();
  await consumeStudioJob(pending.job_id, answer);
}

async function ensureStudioSessionRoute() {
  // Existing applications already have a stable /studio/:id route.  Their
  // DesignSession is an internal chat continuation and must not replace the
  // application route with an application ID masquerading as a session ID.
  if (route.params.id && /^\d+$/.test(String(route.params.id))) return;
  if (String(route.query.session || "") === String(workflow.value.id)) return;
  await router.replace({
    path: "/new-automation",
    query: { session: workflow.value.id },
  });
}

function applyStudioResult(finalResult, answer) {
  if (finalResult.workflow) {
    applyGeneratedWorkflow(finalResult.workflow);
  }
  if (finalResult.phase === "failed") {
    if (answer) {
      answer.status = "failed";
      answer.error = finalResult.error || "请求未完成";
      answer.role = "error";
      answer.streaming = false;
      answer.pending = false;
      answer.text ||= `没有完成：${answer.error}`;
    }
    throw new Error(finalResult.error || "请求未完成");
  }
  if (answer) {
    answer.pending = false;
    answer.status = finalResult.phase === 'ready' || finalResult.phase === 'awaiting_confirmation'
      ? 'completed' : (answer.status || 'completed');
    if (!answer.text) answer.text = finalResult.reply || "已完成。";
    answer.streaming = false;
  }
  if (finalResult.phase === "awaiting_confirmation" && finalResult.proposal) {
    const proposalStage = finalResult.proposal.stage;
    if (
      ["architecture", "generation"].includes(proposalStage) &&
      (!Array.isArray(finalResult.proposal.agents) ||
        !finalResult.proposal.agents.length ||
        !Array.isArray(finalResult.proposal.tasks) ||
        !finalResult.proposal.tasks.length)
    ) {
      throw new Error("生成结果缺少 Agent 或 Task，请重试当前编排阶段");
    }
    const nextProposal = migrateProposal(finalResult.proposal);
    if (!nextProposal) return;
    removedContractInputNames.clear();
    confirmation.value = nextProposal;
    if (answer) {
      answer.proposal = nextProposal;
      answer.clarification = migrateClarification(nextProposal.clarification);
    }
    if (
      nextProposal.kind_preselected ||
      nextProposal.confirmed_stages.includes("architecture")
    ) {
      selectedKind.value = nextProposal.recommended_kind;
      workflow.value.kind = nextProposal.recommended_kind;
    }
    workflow.value.structure_confirmed = false;
    fitCanvas(180);
    return;
  }
}

function applyGeneratedWorkflow(value) {
  if (!Array.isArray(value?.agents) || !value.agents.length ||
      !Array.isArray(value?.tasks) || !value.tasks.length) {
    throw new Error("生成结果缺少 Agent 或 Task，未载入画布；请重试生成阶段");
  }
  const historySnapshot = workflow.value.chat_history;
  hydrating = true;
  workflow.value = migrate(value);
  workflow.value.chat_history = historySnapshot;
  workflow.value.structure_confirmed = true;
  // Generated results become the new baseline. Only edits made afterwards
  // are reported as manual canvas changes.
  markWorkflowPersisted(workflow.value);
  confirmation.value = null;
  // Do not force the first task into the inspector after generation. The
  // generated graph should use the available canvas area until the user
  // explicitly selects a node or opens orchestration settings.
  selectedTaskId.value = "";
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  inspectorSettingsOpen.value = false;
  fitCanvas(220);
  draftDirty = false;
  saveState.value = "Draft saved";
  hydrating = false;
  if (/^\d+$/.test(String(workflow.value.id || "")) &&
      String(route.params.id || "") !== String(workflow.value.id)) {
    router.replace(`/studio/${workflow.value.id}`);
  }
}

async function consumeStudioJob(jobId, answer) {
  let finalResult = null;
  await api.studioEvents(jobId, (event) => {
    if (event.type === "delta") answer.text += event.text;
    if (event.type === "workflow_ready" && event.workflow) {
      applyGeneratedWorkflow(event.workflow);
    }
    if (event.type === "progress")
      activity.value = {
        phase: event.phase,
        plan: event.plan || [],
        attempts: event.attempts || 0,
      };
    if (event.type === "done") finalResult = event.response;
    if (event.type === "error") throw new Error(event.message);
  });
  if (!finalResult) finalResult = await api.studioJob(jobId);
  applyStudioResult(finalResult, answer);
}

async function resumeStudioJob(
  jobId,
  sessionId = studioSessionId.value || route.query.session || workflow.value.id,
) {
  let answer = [...messages.value]
    .reverse()
    .find((item) => item.role === "assistant" && item.jobId === jobId);
  if (!answer) {
    answer = { role: "assistant", text: "", jobId };
    messages.value.push(answer);
  }
  answer.pending = true;
  answer.streaming = true;
  busy.value = true;
  activity.value = { phase: "working", plan: [] };
  try {
    await consumeStudioJob(jobId, answer);
  } catch (error) {
    // Redis streams are a transient delivery channel. The final authority is
    // the session row, so reload it when the stream has expired or reconnects.
    const session = await api.studioSession(sessionId);
    const result = session.active_job?.result;
    if (result) applyStudioResult(result, answer);
    else throw error;
  } finally {
    answer.pending = false;
    answer.streaming = false;
    busy.value = false;
    activity.value = null;
  }
}

async function sendMessage() {
  const text =
    prompt.value.trim() ||
    (attachments.value.length ? "请结合我上传的附件继续。" : "");
  if (!store.chatModels.length) {
    store.error = "请先添加一个对话模型连接";
    router.push("/models");
    return;
  }
  if (!store.defaultModel && !workflow.value.model_profile_id) {
    store.error = "请先设置工作空间默认模型";
    router.push("/model-default");
    return;
  }
  if (!text || uploading.value) return;
  // Flush a just-edited canvas before building the next Composer request. The
  // generation request must start from the same persisted revision that the
  // Agent receives. A stale tab is stopped here instead of silently replacing
  // a newer draft through natural-language generation.
  if (draftDirty && workflow.value.structure_confirmed) {
    await autoSave();
    if (draftDirty && !draftSaveInvalid) return;
  }
  const history = messages.value
    .filter((item) => ["user", "assistant"].includes(item.role))
    .slice(-12)
    .map((item) => ({ role: item.role, content: item.text }));
  const sentAttachments = attachments.value.map((item) => ({
    id: item.id,
    name: item.name,
    content_type: item.content_type,
    size: item.size,
  }));
  const pendingClarification = [...messages.value]
    .reverse()
    .find((item) => item.clarification && !item.clarification.locked);
  if (pendingClarification) {
    pendingClarification.clarification.locked = true;
    pendingClarification.clarification.selected = text;
    pendingClarification.clarification.selectedLabel = text;
  }
  messages.value.push({ role: "user", text, attachments: sentAttachments });
  messages.value.push({ role: "assistant", text: "", streaming: true });
  const answer = messages.value[messages.value.length - 1];
  const attachmentIds = attachments.value.map((item) => item.id);
  prompt.value = "";
  attachments.value = [];
  busy.value = true;
  activity.value = { phase: "working", plan: [] };
  try {
    await executeStudioRequest(
      text,
      answer,
      history,
      attachmentIds,
      pendingClarification
        ? {
            clarificationId: pendingClarification.clarification.id,
            clarificationValue: text,
            proposal: pendingClarification.proposal || confirmation.value,
          }
        : {},
    );
  } catch (error) {
    if (pendingClarification) {
      pendingClarification.clarification.locked = false;
      pendingClarification.clarification.selected = "";
      pendingClarification.clarification.selectedLabel = "";
    }
    answer.streaming = false;
    answer.status = "failed";
    answer.error = error.message;
    answer.text ||= `没有完成：${error.message}`;
    answer.role = "error";
    store.error = error.message;
  } finally {
    busy.value = false;
    activity.value = null;
  }
}

// Every failed Studio turn must carry role, error and status together: the
// retry button is rendered only when all of them (plus jobId) are present.
function markStudioFailure(answer, error) {
  const message = error?.message || String(error || "请求未完成");
  answer.streaming = false;
  answer.pending = false;
  answer.status = "failed";
  answer.error = message;
  answer.role = "error";
  answer.text ||= `没有完成：${message}`;
  store.error = message;
}

// Only the latest failed turn can be retried.  Once the user has sent
// another message (or another job started), an older failure is history:
// retrying it would replay a request against a newer proposal state.
function isRetryableStudioMessage(index) {
  const message = messages.value[index];
  if (!message?.jobId || message.streaming || message.role !== "error" || !message.error) return false;
  return !messages.value
    .slice(index + 1)
    .some((item) => item.role === "user" || item.jobId);
}

async function retryStudioMessage(message) {
  const index = messages.value.indexOf(message);
  if (index < 0 || !isRetryableStudioMessage(index) || busy.value || message.retrying) return;
  message.retrying = true;
  busy.value = true;
  message.streaming = true;
  message.role = 'assistant';
  message.error = '';
  message.text = '';
  message.files = [];
  activity.value = { phase: 'working', plan: [] };
  try {
    const pending = await api.retryStudioJob(message.jobId);
    message.jobId = pending.job_id;
    await consumeStudioJob(pending.job_id, message);
  } catch (error) {
    message.streaming = false;
    message.role = 'error';
    message.status = 'failed';
    message.error = error.message;
    message.text ||= `没有完成：${error.message}`;
    store.error = error.message;
  } finally {
    message.retrying = false;
    message.streaming = false;
    busy.value = false;
    activity.value = null;
  }
}

function addContractInput() {
  if (proposalStageLocked(confirmation.value, "inputs")) return;
  const used = new Set(confirmation.value.inputs.map((item) => item.name));
  let name = "input";
  let suffix = 2;
  while (used.has(name)) name = `input_${suffix++}`;
  confirmation.value.inputs.push({
    name,
    label: "新输入",
    input_type: "text",
    required: true,
    description: "",
    multiple: false,
  });
}
function removeContractInput(index) {
  if (proposalStageLocked(confirmation.value, "inputs")) return;
  if (confirmation.value.inputs[index]?.name === "message") return;
  removedContractInputNames.add(confirmation.value.inputs[index]?.name);
  confirmation.value.inputs.splice(index, 1);
}
function addWorkflowInput() {
  const used = new Set(workflow.value.inputs.map((item) => item.name));
  let name = "input";
  let suffix = 2;
  while (used.has(name)) name = `input_${suffix++}`;
  workflow.value.inputs.push({
    name,
    label: "新输入",
    input_type: "text",
    required: true,
    description: "",
    multiple: false,
  });
}
function normalizeProposalInputType(item) {
  if (!['file', 'image'].includes(item.input_type)) return;
  item.name = 'files';
  item.label = '本轮文件';
  item.input_type = 'file';
  item.multiple = true;
  item.description = '用户每轮对话上传的文件列表，后续轮次追加，不覆盖已有文件。';
  const duplicates = confirmation.value.inputs
    .map((value, index) => ({ value, index }))
    .filter(({ value }) => value !== item && ['file', 'image'].includes(value.input_type));
  duplicates.reverse().forEach(({ index }) => confirmation.value.inputs.splice(index, 1));
}
function renameWorkflowInput(input, event) {
  const next = event.target.value.trim();
  const current = input.name;
  if (current === 'message' || !/^[A-Za-z_]\w*$/.test(next) ||
      workflow.value.inputs.some((item) => item !== input && item.name === next)) {
    event.target.value = current;
    return;
  }
  input.name = next;
  for (const task of workflow.value.tasks) {
    for (const binding of Object.values(task.input_bindings || {})) {
      if (binding.source === 'input' && binding.variable === current) binding.variable = next;
    }
  }
}
function changeWorkflowInputType(input, value) {
  input.input_type = value;
  if (['file', 'image'].includes(value)) {
    const existing = workflow.value.inputs.find((item) =>
      item !== input && ['file', 'image'].includes(item.input_type),
    );
    if (existing) workflow.value.inputs.splice(workflow.value.inputs.indexOf(existing), 1);
    input.name = 'files';
    input.label = '本轮文件';
    input.multiple = true;
    input.description = '用户每轮对话上传的文件列表，后续轮次追加，不覆盖已有文件。';
  } else {
    input.multiple = false;
  }
}
function removeWorkflowInput(input, index) {
  if (!input || input.name === 'message') return;
  const removedName = input.name;
  workflow.value.inputs.splice(index, 1);
  // Do not leave a saved code/tool contract pointing at a deleted runtime
  // field. Preserve the argument so the inspector can show the missing
  // variable and the user can rebind it before saving.
  for (const task of workflow.value.tasks) {
    for (const binding of Object.values(task.input_bindings || {})) {
      if (binding.source === 'input' && binding.variable === removedName) {
        binding.variable = '';
      }
    }
  }
}
function isActiveProposal(index) {
  return index === activeProposalIndex.value;
}

function proposalStageLocked(proposal, stage) {
  return Boolean(proposal?.confirmed_stages?.includes(stage));
}

function pendingStageText(message) {
  const stage = activity.value?.phase && activity.value.phase !== "planning"
    ? activity.value.phase
    : message?.proposal?.stage || "discovery";
  return {
    discovery: "正在理解用户信息并回复…",
    inputs: "正在设计发布后的运行输入…",
    architecture: "正在设计 Agent 职责与任务关系…",
    generation: "正在生成可运行编排…",
    generation_review: "正在审查，请耐心等待…",
    generation_correction: "正在根据审查结果修正编排，这可能需要几分钟…",
  }[stage] || "正在处理当前编排阶段…";
}

function setProposalKind(proposal, kind) {
  if (
    proposal?.stage !== "architecture" ||
    proposal.confirmed_stages.includes("architecture") ||
    !["crew", "flow"].includes(kind)
  )
    return;
  proposal.recommended_kind = kind;
  proposal.recommended_process =
    kind === "flow"
      ? "sequential"
      : ["sequential", "hierarchical"].includes(proposal.recommended_process)
        ? proposal.recommended_process
        : "sequential";
  proposal.process_reason =
    kind === "flow"
      ? "确认后将按顺序、状态与分支关系重新生成 Flow。"
      : "确认后将按 Agent 与 Task 的协作关系重新生成 Crew。";
  confirmation.value = proposal;
  scheduleDraft();
}

async function confirmProposalStage(message, messageIndex) {
  const active = message?.proposal;
  if (
    !active ||
    !isActiveProposal(messageIndex) ||
    busy.value ||
    proposalSubmittingStage.value
  )
    return;
  // Discovery is completed only through clarification/capability actions;
  // it is never a free-standing confirmation card.
  if (active.stage === "discovery") return;
  if (!["inputs", "architecture"].includes(active.stage)) return;
  if (active.stage === "inputs") {
    const invalid = active.inputs.find(
      (item) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(item.name),
    );
    if (invalid) {
      store.error = `输入变量名“${invalid.name}”无效，请使用英文、数字和下划线`;
      return;
    }
  }
  const proposal = JSON.parse(JSON.stringify(active));
  const stage = proposal.stage;
  proposalSubmittingStage.value = stage;
  message.submitting = true;
  if (!active.confirmed_stages.includes(stage)) {
    active.confirmed_stages = [...active.confirmed_stages, stage];
  }
  confirmation.value = active;
  const history = messages.value
    .filter((item) => ["user", "assistant"].includes(item.role))
    .slice(-12)
    .map((item) => ({ role: item.role, content: item.text }));
  const confirmationText = `确认${stage === "inputs" ? "运行输入" : "编排架构"}。`;
  messages.value.push({ role: "user", text: confirmationText });
  messages.value.push({ role: "assistant", text: "", streaming: true });
  const answer = messages.value[messages.value.length - 1];
  busy.value = true;
  activity.value = { phase: "planning", plan: [] };
  try {
    await executeStudioRequest(confirmationText, answer, history, [], {
      confirmed: false,
      kind:
        stage === "inputs" && !proposal.kind_preselected
          ? "auto"
          : proposal.recommended_kind,
      kindPreselected: Boolean(proposal.kind_preselected),
      inputContract: proposal.inputs,
      proposal,
      confirmationStage: stage,
      action: "confirm_stage",
    });
  } catch (error) {
    active.confirmed_stages = active.confirmed_stages.filter(
      (item) => item !== stage,
    );
    confirmation.value = active;
    markStudioFailure(answer, error);
  } finally {
    busy.value = false;
    message.submitting = false;
    proposalSubmittingStage.value = null;
    activity.value = null;
    scheduleDraft();
  }
}

async function chooseClarificationOption(message, option) {
  const clarification = message?.clarification;
  if (!clarification || clarification.locked || busy.value) return;
  clarification.locked = true;
  clarification.selected = option.value;
  clarification.selectedLabel = option.label;
  const history = messages.value
    .filter((item) => ["user", "assistant"].includes(item.role))
    .slice(-12)
    .map((item) => ({ role: item.role, content: item.text }));
  messages.value.push({ role: "user", text: option.label });
  messages.value.push({ role: "assistant", text: "", streaming: true });
  const answer = messages.value[messages.value.length - 1];
  busy.value = true;
  activity.value = { phase: "planning", plan: [] };
  try {
    const choseKind = clarification.id === "orchestration_kind";
    if (choseKind) {
      selectedKind.value = option.value;
      kindPreselected.value = true;
    }
    await executeStudioRequest(option.label, answer, history, [], {
      kind:
        choseKind ? option.value : undefined,
      kindPreselected: choseKind || undefined,
      proposal: message.proposal || confirmation.value,
      clarificationId: clarification.id,
      clarificationValue: option.value,
      action: "resolve_clarification",
    });
  } catch (error) {
    clarification.locked = false;
    clarification.selected = "";
    clarification.selectedLabel = "";
    markStudioFailure(answer, error);
  } finally {
    busy.value = false;
    activity.value = null;
    scheduleDraft();
  }
}

async function chooseAttachments(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (!files.length) return;
  const startedAt = Date.now();
  uploading.value = true;
  uploadProgress.value = 0;
  pendingUploadNames.value = files.map((file) => file.name);
  try {
    attachments.value.push(
      ...(await api.uploadStudioAttachments(files, (value) => {
        uploadProgress.value = value;
      })),
    );
  } catch (error) {
    store.error = error.message;
  } finally {
    await keepUploadStatusVisible(startedAt);
    uploading.value = false;
    uploadProgress.value = 0;
    pendingUploadNames.value = [];
  }
}
async function removeAttachment(id) {
  attachments.value = attachments.value.filter((item) => item.id !== id);
  try {
    await api.deleteStudioAttachment(id);
  } catch (_) {
    /* Upload cleanup is best-effort. */
  }
}
function attachmentIcon(item) {
  return item.content_type?.startsWith("image/") ? Image : FileText;
}
function formatBytes(value) {
  return value < 1024 * 1024
    ? `${Math.max(1, Math.round(value / 1024))} KB`
    : `${(value / 1024 / 1024).toFixed(1)} MB`;
}
function capabilityLabel(id, resourceType = "") {
  const collections = {
    skill: store.skills,
    tool: store.plugins,
    knowledge: store.knowledge,
  };
  const selected = collections[resourceType];
  if (selected) {
    return (
      selected.find((item) => String(item.id) === String(id))?.name || id
    );
  }
  return (
    [...store.skills, ...store.plugins, ...store.knowledge].find(
      (item) => String(item.name) === String(id),
    )?.name || id
  );
}
async function save() {
  if (!workflow.value.structure_confirmed) {
    draftDirty = true;
    await autoSave();
    store.notify("方案对话已保存，生成完成后才能保存为智能体");
    return;
  }
  saveState.value = "Saving...";
  try {
    syncAgentDelegation();
    await persistWorkflowDraft();
    if (draftDirty) await persistWorkflowDraft();
    saveState.value = "Saved";
    store.notify("工作流已保存");
    if (!route.params.id || route.params.id !== workflow.value.id)
      router.replace(`/studio/${workflow.value.id}`);
    return true;
  } catch (error) {
    store.error = error.message;
    saveState.value = "Failed";
    return false;
  }
}
async function publish() {
  if (!workflow.value.structure_confirmed) {
    store.error = "请先确认运行输入并生成 Crew 或 Flow";
    return;
  }
  try {
    if (!(await save())) throw new Error("当前编排保存失败，无法发布");
    const published = await api.publishWorkflow(Number(workflow.value.id));
    hydrating = true;
    workflow.value = migrate(published);
    markWorkflowPersisted(workflow.value);
    await nextTick();
    hydrating = false;
    draftDirty = false;
    saveState.value = "Published";
    await store.load();
    store.notify("已发布当前编排版本");
  } catch (error) {
    store.error = error.message;
  }
}
async function ensurePreviewApplicationId() {
  if (!workflow.value.structure_confirmed)
    throw new Error("请先完成编排并生成可运行方案");
  // Previewing an unchanged saved draft does not need another full graph
  // persistence cycle (relations, resources and revision metadata).
  if (/^\d+$/.test(String(workflow.value.id || "")) && !draftDirty && !saveInFlight)
    return Number(workflow.value.id);
  if (draftTimer) {
    window.clearTimeout(draftTimer);
    draftTimer = null;
  }
  saveState.value = "Saving draft...";
  syncAgentDelegation();
  let saved = await persistWorkflowDraft();
  if (draftDirty) saved = await persistWorkflowDraft();
  if (!/^\d+$/.test(String(saved.id || "")))
    throw new Error("应用保存后未返回有效的整数 ID");
  saved.id = Number(saved.id);
  return saved.id;
}

async function initializePreview(applicationId) {
  for (const input of workflow.value.inputs) {
    previewValues[input.name] = input.input_type === "boolean" ? false : "";
    previewFiles[input.name] = [];
  }
  // Same conversation API as api.createConversation(applicationId), explicitly
  // marked as preview so it can use the current draft graph.
  const conversation = await api.createConversation(applicationId, { preview: true });
  previewConversationId.value = conversation.id;
  previewMessages.value = [
    {
      role: "assistant",
      text:
        workflow.value.description ||
        "你好，可以输入消息、上传文件或填写运行变量来测试这个自动化。",
    },
  ];
  previewMessage.value = "";
}
async function launchRun() {
  try {
    const applicationId = await ensurePreviewApplicationId();
    await initializePreview(applicationId);
    showRun.value = true;
  } catch (error) {
    store.error = error.message;
  }
}
function closePreview() {
  showRun.value = false;
  if (previewPollTimer) window.clearTimeout(previewPollTimer);
  previewRunAbortController?.abort();
  previewRunAbortController = null;
  previewBusy.value = false;
}
function previewHasValue(input) {
  const value = previewValues[input.name];
  return input.input_type === "boolean"
    ? value === true
    : value !== "" && value !== null && value !== undefined;
}
function previewInputPayload() {
  const result = {};
  for (const input of workflow.value.inputs) {
    // Preview uploads are attachment IDs, never ordinary input values. This
    // keeps file names from overwriting the conversation's text field.
    if (["file", "image"].includes(input.input_type)) continue;
    if (input.name === previewPrimaryInput.value?.name)
      result[input.name] = previewMessage.value.trim();
    else if (input.input_type === "json" && previewValues[input.name]) {
      try {
        result[input.name] = JSON.parse(previewValues[input.name]);
      } catch (_) {
        throw new Error(`${input.label} 不是有效的 JSON`);
      }
    } else if (
      input.input_type === "number" &&
      previewValues[input.name] !== ""
    )
      result[input.name] = Number(previewValues[input.name]);
    else result[input.name] = previewValues[input.name];
  }
  return result;
}
function validatePreview(inputs, attachmentBindings) {
  if (workflow.value.interaction_mode === "multi_turn") return;
  const missing = workflow.value.inputs
    .filter(
      (input) =>
        input.required &&
        (["file", "image"].includes(input.input_type)
          ? !attachmentBindings[input.name]?.length
          : inputs[input.name] === "" ||
            inputs[input.name] === null ||
            inputs[input.name] === undefined),
    )
    .map((input) => input.label);
  if (missing.length) throw new Error(`请先填写：${missing.join("、")}`);
}
async function choosePreviewFiles(event) {
  const selected = Array.from(event.target.files || []);
  event.target.value = "";
  if (!selected.length || !previewFileInputs.value.length) return;
  const startedAt = Date.now();
  previewUploading.value = true;
  previewUploadProgress.value = 0;
  previewPendingUploadNames.value = selected.map((file) => file.name);
  try {
    const uploaded = await api.uploadStudioAttachments(selected, (value) => {
      previewUploadProgress.value = value;
    });
    for (const file of uploaded) {
      const matching = previewFileInputs.value.filter(
        (input) =>
          input.input_type !== "image" ||
          file.content_type?.startsWith("image/"),
      );
      const target =
        matching.find(
          (input) => input.multiple || !previewFiles[input.name]?.length,
        ) ||
        matching[0] ||
        previewFileInputs.value[0];
      previewFiles[target.name] ||= [];
      previewFiles[target.name] = target.multiple
        ? [...previewFiles[target.name], file]
        : [file];
    }
  } catch (error) {
    store.error = error.message;
  } finally {
    await keepUploadStatusVisible(startedAt);
    previewUploading.value = false;
    previewUploadProgress.value = 0;
    previewPendingUploadNames.value = [];
  }
}
function removePreviewFile(inputName, index) {
  previewFiles[inputName].splice(index, 1);
}
async function retryPreviewMessage(message) {
  if (!isLatestRunMessage(previewMessages.value, message) || previewBusy.value) return;
  message.retrying = true;
  message.role = 'assistant';
  message.error = '';
  message.text = '';
  message.files = [];
  message.turns = [];
  message.finalTurnId = '';
  message.activity = '';
  message.streaming = true;
  previewBusy.value = true;
  try {
    await api.retryRun(message.runId);
    await streamPreview(message.runId, message);
  } catch (error) {
    message.role = 'error';
    message.error = error.message;
    message.text = `没有完成：${error.message}`;
    message.streaming = false;
    store.error = error.message;
  } finally {
    message.retrying = false;
    previewBusy.value = false;
  }
}
function onPreviewScroll() {
  const element = previewThread.value;
  if (element)
    previewFollowBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
}
function scrollPreview(force = false) {
  if (force) previewFollowBottom = true;
  if (!previewFollowBottom) return;
  if (previewScrollTimer) window.clearTimeout(previewScrollTimer);
  previewScrollTimer = window.setTimeout(() => {
    previewScrollTimer = null;
    nextTick(() => {
      if (previewThread.value && previewFollowBottom)
        previewThread.value.scrollTop = previewThread.value.scrollHeight;
    });
  }, 32);
}
function openPreviewFeedback(runId, pendingFeedback, answer) {
  answer.approvals ||= [];
  const existing = answer.approvals.find(
    (item) =>
      item.runId === runId &&
      item.step_id === pendingFeedback?.step_id &&
      item.status === "pending",
  );
  if (!existing)
    answer.approvals.push({
      ...(pendingFeedback || {}),
      runId,
      feedback: "",
      status: "pending",
      busy: false,
    });
  answer.text = "";
  answer.status = "waiting_approval";
  answer.streaming = false;
  previewBusy.value = false;
  scrollPreview();
}
async function submitPreviewFeedback(answer, approval, outcome) {
  if (!approval?.runId || approval.busy || approval.status !== "pending") return;
  approval.busy = true;
  try {
    await api.submitRunFeedback(
      approval.runId,
      outcome,
      approval.feedback,
    );
    approval.status = "submitted";
    approval.outcome = outcome;
    answer.text = "";
    answer.turns = [];
    answer.finalTurnId = "";
    answer.activity = "";
    answer.streaming = true;
    answer.status = "queued";
    previewBusy.value = true;
    await streamPreview(approval.runId, answer);
  } catch (error) {
    store.error = error.message;
  } finally {
    approval.busy = false;
  }
}
async function pollPreview(id, answer) {
  try {
    const record = await api.run(id);
    answer.status = record.status;
    answer.runId = record.id;
    if (record.status === "completed") {
      answer.text = stripLocalArtifactReferences(record.output) || "执行已完成。";
      answer.files = record.files || [];
      answer.streaming = false;
      answer.routerOnly =
        record.metrics?.runtime_type === "conversation_router";
      previewBusy.value = false;
      scrollPreview();
      return;
    }
    if (record.status === "failed") {
      answer.role = record.output ? "assistant" : "error";
      answer.text = stripLocalArtifactReferences(record.output);
      answer.error = record.error || "执行失败";
      answer.streaming = false;
      previewBusy.value = false;
      scrollPreview();
      return;
    }
    if (record.status === "waiting_approval") {
      openPreviewFeedback(record.id, record.pending_feedback, answer);
      return;
    }
    previewPollTimer = window.setTimeout(() => pollPreview(id, answer), 900);
  } catch (error) {
    answer.role = "error";
    answer.text = error.message;
    answer.streaming = false;
    previewBusy.value = false;
    store.error = error.message;
  }
}
async function downloadPreviewArtifact(file) {
  try {
    await api.downloadRunFile(file);
  } catch (error) {
    store.error = error.message;
  }
}
async function streamPreview(id, answer) {
  const controller = new AbortController();
  previewRunAbortController?.abort();
  previewRunAbortController = controller;
  let terminal = "";
  try {
    const batcher = createRunFrameBatcher((frame) => {
        terminal = applyRunFrame(answer, frame) || terminal;
        if (frame.type === "approval.required")
          openPreviewFeedback(id, frame.pending_feedback || {}, answer);
        scrollPreview();
      });
    await api.runEvents(
      id,
      (frame) => batcher.push(frame),
      controller.signal,
      answer.eventCursor || 0,
    );
    await batcher.finish();
    if (!terminal) return pollPreview(id, answer);
    previewBusy.value = false;
    scrollPreview();
  } catch (error) {
    if (error.name !== "AbortError") await pollPreview(id, answer);
  } finally {
    if (previewRunAbortController === controller)
      previewRunAbortController = null;
  }
}
async function sendPreview() {
  if (previewSendInFlight || !previewCanSend.value || !store.chatModels.length) return;
  previewSendInFlight = true;
  previewBusy.value = true;
  let answer = null;
  try {
    if (!(await save())) throw new Error("当前编排保存失败，无法预览运行");
    const inputs = previewInputPayload();
    const attachmentBindings = Object.fromEntries(
      Object.entries(previewFiles)
        .filter(([, items]) => items?.length)
        .map(([name, items]) => [name, items.map((item) => item.id)]),
    );
    validatePreview(inputs, attachmentBindings);
    const text =
      previewMessage.value.trim() ||
      (previewSelectedFiles.value.length
        ? "请判断并处理我上传的文件。"
        : "运行自动化。");
    const submittedMessage = previewMessage.value.trim();
    const shownFiles = previewSelectedFiles.value.map((item) => ({
      id: item.id,
      name: item.name,
    }));
    previewMessages.value.push({ role: "user", text, attachments: shownFiles });
    answer = reactive({
      role: "assistant",
      text: "",
      streaming: true,
      status: "queued",
      runId: "",
      steps: [],
      turns: [],
      finalTurnId: "",
      retryPayload: {
        inputs: JSON.parse(JSON.stringify(inputs)),
        attachments: JSON.parse(JSON.stringify(attachmentBindings)),
        message: submittedMessage,
      },
    });
    previewMessages.value.push(answer);
    previewMessage.value = "";
    scrollPreview();
    const record = await api.runWorkflow(
      workflow.value.id,
      inputs,
      attachmentBindings,
      {
        conversation_id: previewConversationId.value,
        preview: true,
        // The placeholder is only for the local transcript. Do not persist it
        // as a user answer while an ask_user request is waiting.
        message: submittedMessage,
      },
    );
    Object.keys(previewFiles).forEach((key) => {
      previewFiles[key] = [];
    });
    answer.runId = record.id;
    await streamPreview(record.id, answer);
  } catch (error) {
    previewBusy.value = false;
    if (answer?.streaming) {
      answer.role = "error";
      answer.error = error.message;
      answer.status = "failed";
      answer.streaming = false;
    }
    store.error = error.message;
  } finally {
    previewSendInFlight = false;
  }
}
function changeProcess(process) {
  workflow.value.process = process;
  if (process === "sequential") {
    workflow.value.manager_agent_id = null;
    workflow.value.manager_model_profile_id = null;
    workflow.value.tasks.forEach((task) => {
      if (!task.agent_id)
        task.agent_id = workflow.value.agents[0]?.id || addAgent();
    });
    syncAgentDelegation();
  } else {
    setManager(workflow.value.manager_agent_id || workflow.value.agents[0]?.id || addAgent());
  }
}
function hierarchicalManagerIds() {
  const ids = new Set();
  if (workflow.value.kind === 'crew' && workflow.value.process === 'hierarchical' &&
      workflow.value.manager_agent_id) {
    ids.add(workflow.value.manager_agent_id);
  }
  for (const task of workflow.value.tasks) {
    if (task.node_type !== 'crew' || task.crew_process !== 'hierarchical') continue;
    const managerId = task.crew_manager_agent_id ||
      (!task.crew_manager_model_profile_id ? task.crew_agent_ids?.[0] : null);
    if (managerId) ids.add(managerId);
  }
  return ids;
}
const selectedAgentCanDelegate = computed(() =>
  Boolean(selectedAgent.value && hierarchicalManagerIds().has(selectedAgent.value.id)),
);
function syncAgentDelegation() {
  const managers = hierarchicalManagerIds();
  workflow.value.agents.forEach((agent) => {
    if (!managers.has(agent.id)) agent.allow_delegation = false;
  });
}
function setManager(agentId) {
  workflow.value.manager_agent_id = agentId || null;
  if (agentId) {
    workflow.value.tasks.forEach((task) => {
      if (task.agent_id === agentId) task.agent_id = null;
    });
    const manager = workflow.value.agents.find((agent) => agent.id === agentId);
    if (manager) manager.allow_delegation = true;
    workflow.value.agents.forEach((agent) => {
      if (workflow.value.interaction_mode === "multi_turn")
        agent.user_interaction = agent.id === agentId;
    });
  }
  syncAgentDelegation();
}
function changeEmbeddedCrewProcess(task, process) {
  task.crew_process = process;
  if (process !== "hierarchical") {
    task.crew_manager_agent_id = null;
    task.crew_manager_model_profile_id = null;
    syncAgentDelegation();
    return;
  }
  if (!task.crew_agent_ids.length) return;
  const managerId = task.crew_manager_agent_id || task.crew_agent_ids[0];
  task.crew_manager_agent_id = managerId;
  const manager = workflow.value.agents.find((agent) => agent.id === managerId);
  if (manager) manager.allow_delegation = true;
  workflow.value.agents.forEach((agent) => {
    if (workflow.value.interaction_mode === "multi_turn" && task.crew_agent_ids.includes(agent.id))
      agent.user_interaction = agent.id === managerId;
  });
  task.crew_tasks.forEach((nested) => {
    nested.agent_id = null;
  });
  syncAgentDelegation();
}
function changeEmbeddedCrewManager(task, managerId) {
  task.crew_manager_agent_id = managerId || null;
  if (managerId) {
    const manager = workflow.value.agents.find((agent) => agent.id === managerId);
    if (manager) manager.allow_delegation = true;
  }
  if (task.crew_process === 'hierarchical') {
    (task.crew_tasks || []).forEach((nested) => { nested.agent_id = null; });
  }
  syncAgentDelegation();
}
function toggleCrewMember(agentId) {
  const task = selectedTask.value;
  if (!task || task.node_type !== "crew") return;
  const index = task.crew_agent_ids.indexOf(agentId);
  if (index >= 0) {
    if (task.crew_agent_ids.length === 1) {
      store.error = "Crew kickoff 至少需要一个 Agent";
      return;
    }
    task.crew_agent_ids.splice(index, 1);
    if (task.agent_id === agentId) task.agent_id = null;
    if (task.crew_manager_agent_id === agentId) {
      task.crew_manager_agent_id = task.crew_process === 'hierarchical'
        ? task.crew_agent_ids[0] || null
        : null;
    }
    (task.crew_tasks || []).forEach((item) => {
      if (item.agent_id === agentId)
        item.agent_id = null;
    });
  } else {
    task.crew_agent_ids.push(agentId);
    if (task.crew_process === 'hierarchical' && !task.crew_manager_agent_id &&
        !task.crew_manager_model_profile_id) {
      task.crew_manager_agent_id = task.crew_agent_ids[0] || null;
    }
  }
  syncAgentDelegation();
}
function addAgent() {
  workflow.value.structure_confirmed = true;
  workflow.value.node_name_mode = 'manual';
  const id = nextWorkflowName('agent');
  const index = workflow.value.agents.length;
  workflow.value.agents.push({
    id,
    role: "New specialist",
    goal: "Complete assigned work reliably",
    backstory: "An experienced specialist with explicit quality standards.",
    model_profile_id: null,
    skills: [],
    plugins: [],
    knowledge_base_ids: [],
    max_iter: 12,
    max_execution_time: null,
    max_retry_limit: 2,
    reasoning: false,
    max_reasoning_attempts: null,
    allow_delegation: false,
    memory: false,
    respect_context_window: true,
    multimodal: false,
    allow_code_execution: false,
    user_interaction: false,
    inject_date: false,
    date_format: "%Y-%m-%d",
    use_system_prompt: true,
    function_calling_model_profile_id: null,
    position: {
      x: 120 + index * CANVAS_COLUMN_GAP,
      y: CANVAS_AGENT_START_Y + index * CANVAS_AGENT_ROW_GAP,
    },
  });
  selectedAgentId.value = id;
  selectedTaskId.value = "";
  selectedEdgeId.value = "";
  fitCanvas();
  return id;
}
function addStep(requestedType) {
  workflow.value.structure_confirmed = true;
  workflow.value.node_name_mode = 'manual';
  const type = workflow.value.kind === "crew" ? "task" : requestedType;
  const prefix = workflow.value.kind === 'crew'
    ? 'task'
    : requestedType === 'agent'
      ? 'agent'
      : requestedType === 'crew'
        ? 'crew'
        : 'node';
  const id = nextWorkflowName(prefix);
  // Flow execution is defined by Agent -> step edges. Do not silently attach
  // the first Agent: an unconnected step must remain visibly unassigned until
  // the user draws the intended relation on the canvas.
  const agentId = workflow.value.kind === "crew"
    ? workflow.value.agents[0]?.id || null
    : null;
  const count = workflow.value.tasks.length;
  workflow.value.tasks.push({
    id,
    name: `New ${typeInfo[type].title}`,
    description: typeInfo[type].description,
    expected_output:
      type === "router"
        ? "A deterministic route label."
        : "A complete and verifiable result.",
    agent_id: agentId,
    crew_agent_ids: [],
    crew_tasks: [],
    depends_on: [],
    output_variables: fixedOutputVariables(type),
    output_mode: 'text',
    dependency_variables: {},
    node_type: type,
    execution_contract: ['code','tool'].includes(type) ? 'object' : 'legacy',
    input_bindings: {},
    tool_id: null,
    code_snippet: type === 'code' ? 'def main(message):\n    return {"text": message}\n' : '',
    ...(type === 'code' ? {
      input_bindings: { message: { source: 'input', variable: 'message', node_id: '', value: '' } },
    } : {}),

    crew_process: "sequential",
    crew_memory: false,
    crew_planning: false,
    crew_cache: true,
    crew_output_log_file: "",
    crew_manager_agent_id: null,
    crew_manager_model_profile_id: null,
    crew_planning_model_profile_id: null,
    crew_verbose: false,
    condition: "",
    run_if: "",
    routes: type === 'router' ? { else: [] } : {},
    router_rules: [],
    async_execution: false,
    human_feedback: false,
    feedback_message: "Please review this step output",
    feedback_outcomes: ["approved", "revise"],
    feedback_default_outcome: null,
    markdown: false,
    output_file: "",
    create_directory: true,
    guardrail: "",
    guardrail_max_retries: 0,
    position: { x: 120 + count * CANVAS_COLUMN_GAP, y: CANVAS_TASK_Y },
  });
  if (type === 'router') {
    const task = workflow.value.tasks[workflow.value.tasks.length - 1];
    const message = runInputOptions.value.find(item => item.name === 'message');
    task.router_rules.push({
      id: 'case_1',
      expression: { type: 'group', operator: 'and', conditions: [{
        type: 'condition', source: 'input', variable: message?.name || 'message',
        value_type: message?.value_type || 'string', operator: 'is_empty', value: '',
      }] },
    });
    task.routes.case_1 = [];
  }
  selectedTaskId.value = id;
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  fitCanvas();
}
function addCanvasNode(type) {
  canvasAddMenuOpen.value = false;
  if (type === 'agent-definition') addAgent();
  else addStep(type);
}
// The Crew container exposes the final internal task's output contract.
function crewFinalTask(task) {
  const pending = [...(task?.crew_tasks || [])]; const done = new Set(); let last = null;
  while (pending.length) {
    const index = pending.findIndex(item => (item.depends_on || []).every(id => done.has(id)));
    if (index < 0) break;
    last = pending.splice(index, 1)[0]; done.add(last.id);
  }
  return last;
}
function syncCrewOutputMode(task) {
  const final = crewFinalTask(task);
  task.output_mode = final?.output_mode || 'text';
}
function addCrewTask(task) {
  if (!task || task.node_type !== "crew") return;
  workflow.value.node_name_mode = 'manual';
  task.crew_tasks ||= [];
  const nested = defaultCrewTask(task, task.crew_tasks.length);
  nested.id = nextWorkflowName('task');
  task.crew_tasks.push(nested);
  syncCrewOutputMode(task);
}
function removeCrewTask(task, index) {
  if (!task?.crew_tasks?.length) return;
  const removed = task.crew_tasks[index];
  task.crew_tasks.splice(index, 1);
  task.crew_tasks.forEach((item) => {
    item.depends_on = (item.depends_on || []).filter(
      (id) => id !== removed?.id,
    );
  });
  syncCrewOutputMode(task);
}
function availableCrewTaskDependencies(task, nested) {
  const nestedTasks = task?.crew_tasks || [];
  return nestedTasks.filter((candidate) =>
    candidate.id !== nested?.id,
  );
}
function nestedDependencyWouldCycle(task, nested, candidateId) {
  const byId = new Map((task?.crew_tasks || []).map((item) => [item.id, item]));
  const pending = [candidateId];
  const visited = new Set();
  while (pending.length) {
    const id = pending.pop();
    if (id === nested?.id) return true;
    if (visited.has(id)) continue;
    visited.add(id);
    pending.push(...(byId.get(id)?.depends_on || []));
  }
  return false;
}
function dependencyRowKey(task, nested) {
  return `${task?.id || ''}:${nested?.id || ''}`;
}
function dependencyDraft(task, nested) {
  return crewDependencyDrafts[dependencyRowKey(task, nested)] || '';
}
function setCrewTaskDependencyDraft(task, nested, dependencyId) {
  crewDependencyDrafts[dependencyRowKey(task, nested)] = dependencyId || '';
}
function addCrewTaskDependency(task, nested) {
  const key = dependencyRowKey(task, nested);
  const dependencyId = dependencyDraft(task, nested);
  if (!dependencyId || !availableCrewTaskDependencies(task, nested).some((item) => item.id === dependencyId)) return;
  if (!nested.depends_on) nested.depends_on = [];
  if (!nested.depends_on.includes(dependencyId) && !nestedDependencyWouldCycle(task, nested, dependencyId))
    nested.depends_on.push(dependencyId);
  crewDependencyDrafts[key] = '';
  syncCrewOutputMode(task);
}
function setCrewTaskDependency(task, nested, dependencyId) {
  setCrewTaskDependencyDraft(task, nested, dependencyId);
}
function removeCrewTaskDependency(nested, dependencyId) {
  nested.depends_on = (nested.depends_on || []).filter(
    (id) => id !== dependencyId,
  );
  const crew = workflow.value.tasks.find(task => (task.crew_tasks || []).some(item => item.id === nested.id));
  if (crew) syncCrewOutputMode(crew);
}
function isValidConnection(connection) {
  if (restoringEdges) return true;
  const { source, target, sourceHandle } = connection;
  if (!source || !target || source === target || target.startsWith("agent:"))
    return false;
  const targetTask = workflow.value.tasks.find((item) => item.id === target);
  if (!targetTask) return false;
  if (source.startsWith("agent:")) {
    const agentId = source.slice(6);
    if (
      workflow.value.kind === "crew" &&
      workflow.value.process === "hierarchical" &&
      agentId === workflow.value.manager_agent_id
    )
      return false;
    if (workflow.value.kind === "flow") {
      if (targetTask.node_type === "crew")
        return !targetTask.crew_agent_ids.includes(agentId);
      return !["router", "code", "tool"].includes(targetTask.node_type)
        && !targetTask.agent_id;
    }
    return targetTask.node_type === "crew"
      ? !targetTask.crew_agent_ids.includes(agentId)
      : !["router", "code", "tool"].includes(targetTask.node_type) && targetTask.agent_id !== agentId;
  }
  const sourceTask = workflow.value.tasks.find((item) => item.id === source);
  const branch = sourceHandle?.startsWith('route:') ? sourceHandle.slice(6) : '';
  const duplicateBranch = branch && sourceTask?.routes?.[branch]?.includes(target);
  if (!sourceTask || duplicateBranch || (!branch && targetTask.depends_on.includes(source)))
    return false;
  if (branch && (sourceTask.node_type !== 'router' || !routerCases(sourceTask).some(item => item.id === branch))) return false;
  const stack = [target];
  const visited = new Set();
  while (stack.length) {
    const current = stack.pop();
    if (current === source) return false;
    if (visited.has(current)) continue;
    visited.add(current);
    workflow.value.tasks
      .filter((item) => item.depends_on.includes(current))
      .forEach((item) => stack.push(item.id));
  }
  return true;
}
function connect(connection) {
  if (!isValidConnection(connection)) {
    store.error = "该连接无效、重复或会形成依赖环路";
    return;
  }
  const target = workflow.value.tasks.find(
    (item) => item.id === connection.target,
  );
  if (connection.source.startsWith("agent:")) {
    const agentId = connection.source.slice(6);
    if (target.node_type === "crew") {
      target.crew_agent_ids ||= [];
      if (!target.crew_agent_ids.includes(agentId)) target.crew_agent_ids.push(agentId);
      if (target.crew_process === 'hierarchical' && !target.crew_manager_agent_id &&
          !target.crew_manager_model_profile_id) {
        target.crew_manager_agent_id = target.crew_agent_ids[0] || null;
      }
      syncAgentDelegation();
    } else {
      target.agent_id = agentId;
    }
  } else {
    if (!target.depends_on.includes(connection.source)) target.depends_on.push(connection.source);
    if (connection.sourceHandle?.startsWith('route:')) {
      const branch = connection.sourceHandle.slice(6);
      const source = workflow.value.tasks.find(item => item.id === connection.source);
      source.routes ||= {};
      source.routes[branch] ||= [];
      if (!source.routes[branch].includes(target.id)) source.routes[branch].push(target.id);
      return;
    }
    target.dependency_variables = {};
  }
}

function nodeClick({ node }) {
  inspectorSettingsOpen.value = false;
  selectedEdgeId.value = "";
  if (node.id.startsWith("agent:")) {
    selectedAgentId.value = node.id.slice(6);
    selectedTaskId.value = "";
  } else {
    selectedTaskId.value = node.id;
    selectedAgentId.value = "";
  }
}
function edgeClick({ edge }) {
  inspectorSettingsOpen.value = false;
  selectedEdgeId.value = edge.id;
  selectedTaskId.value = "";
  selectedAgentId.value = "";
}
function nodeDragStop({ node }) {
  if (node.id.startsWith("agent:")) {
    const agent = workflow.value.agents.find(
      (item) => item.id === node.id.slice(6),
    );
    if (agent) agent.position = { ...node.position };
  } else {
    const task = workflow.value.tasks.find((item) => item.id === node.id);
    if (task) task.position = { ...node.position };
  }
}
function clearSelection() {
  selectedTaskId.value = "";
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  inspectorSettingsOpen.value = false;
  canvasAddMenuOpen.value = false;
}
function toggleOrchestrationSettings() {
  const settingsOnly = inspectorSettingsOpen.value &&
    !selectedAgent.value && !selectedTask.value && !selectedEdge.value;
  if (settingsOnly) {
    inspectorSettingsOpen.value = false;
    return;
  }
  selectedTaskId.value = "";
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  canvasAddMenuOpen.value = false;
  inspectorSettingsOpen.value = true;
}
function removeEdge() {
  const edge = selectedEdge.value;
  if (!edge) return;
  const target = workflow.value.tasks.find((item) => item.id === edge.target);
  if (edge.edgeType === 'route') {
    const source = workflow.value.tasks.find(item => item.id === edge.source);
    source.routes[edge.branch] = (source.routes[edge.branch] || []).filter(id => id !== edge.target);
    const stillRouted = Object.values(source.routes).some(targets => targets.includes(edge.target));
    if (!stillRouted) target.depends_on = target.depends_on.filter(id => id !== edge.source);
  } else if (edge.edgeType === "dependency") {
    target.depends_on = target.depends_on.filter((id) => id !== edge.source);
    target.dependency_variables = {};
    if (['code', 'tool'].includes(target.node_type) && !ancestorNodeIds(target).has(edge.source)) {
      const bindings = removeDependencyBindings(target.input_bindings, edge.source);
      target.input_bindings = bindings;
      syncCodeSignature(target, bindings);
    }
  } else if (edge.edgeType === "member") {
    if (workflow.value.kind === "crew" && target.crew_agent_ids.length === 1) {
      store.error = "Crew kickoff 至少需要一个 Agent";
      return;
    }
    const removedAgent = edge.source.slice(6);
    target.crew_agent_ids = target.crew_agent_ids.filter(
      (id) => id !== removedAgent,
    );
    if (target.crew_manager_agent_id === removedAgent) {
      target.crew_manager_agent_id = target.crew_process === 'hierarchical'
        ? target.crew_agent_ids[0] || null
        : null;
    }
    (target.crew_tasks || []).forEach((item) => {
      if (item.agent_id === removedAgent)
        item.agent_id = null;
    });
    syncAgentDelegation();
  } else {
    // Assignment is represented by the edge. Removing it intentionally leaves
    // the Flow step unassigned until another Agent edge is drawn.
    target.agent_id = null;
  }
  selectedEdgeId.value = "";
}
function removeTask() {
  const task = selectedTask.value;
  if (!task) return;
  workflow.value.tasks.forEach(item => {
    if (item.node_type === 'router') {
      Object.keys(item.routes || {}).forEach(branch => {
        item.routes[branch] = item.routes[branch].filter(id => id !== task.id);
      });
    }
  });
  workflow.value.tasks = workflow.value.tasks
    .filter((item) => item.id !== task.id)
    .map((item) => {
      const dependency_variables = { ...item.dependency_variables };
      delete dependency_variables[task.id];
      return {
        ...item,
        depends_on: item.depends_on.filter((id) => id !== task.id),
        dependency_variables,
      };
    });
  selectedTaskId.value = "";
}
function removeAgent() {
  const agent = selectedAgent.value;
  if (!agent) return;
  const used = workflow.value.tasks.some(
    (task) =>
      task.agent_id === agent.id || task.crew_agent_ids.includes(agent.id),
  );
  if (used || workflow.value.manager_agent_id === agent.id) {
    store.error =
      "该 Agent 正被 Task、Flow step 或 Crew manager 使用，请先重新分配。";
    return;
  }
  workflow.value.agents = workflow.value.agents.filter(
    (item) => item.id !== agent.id,
  );
  selectedAgentId.value = "";
}
function handleDeleteKey(event) {
  if (!["Delete", "Backspace"].includes(event.key)) return;
  const target = event.target;
  const tag = target?.tagName?.toLowerCase();
  if (
    ["input", "textarea", "select"].includes(tag) ||
    target?.isContentEditable
  )
    return;
  if (selectedEdge.value) {
    event.preventDefault();
    removeEdge();
  } else if (selectedTask.value) {
    event.preventDefault();
    removeTask();
  } else if (selectedAgent.value) {
    event.preventDefault();
    removeAgent();
  }
}
function importCapability(key, id) {
  if (!selectedAgent.value) return;
  selectedAgent.value[key] ||= [];
  if (!selectedAgent.value[key].includes(id)) selectedAgent.value[key].push(id);
}
function detachCapability(key, id) {
  if (!selectedAgent.value?.[key]) return;
  selectedAgent.value[key] = selectedAgent.value[key].filter(
    (item) => item !== id,
  );
}
function addProposalCapability(proposal, requirement, id) {
  const selected = [...(requirement.selected_ids || [])];
  if (!selected.some((value) => String(value) === String(id))) selected.push(id);
  setProposalCapabilities(proposal, requirement, selected);
}
function setProposalCapabilities(proposal, requirement, selectedIds) {
  const requirementKey = capabilityRequirementKey(requirement);
  const previous = [...(requirement.selected_ids || [])];
  const selected = proposalResources(requirement)
    .filter((resource) =>
      selectedIds.some((id) => String(id) === String(resource.id)),
    )
    .map((resource) => resource.id);
  for (const collection of [
    proposal.capability_requirements || [],
    proposal.capability_blocked || [],
  ]) {
    const target = collection.find(
      (item) => capabilityRequirementKey(item) === requirementKey,
    );
    if (target) target.selected_ids = [...selected];
  }
  requirement.selected_ids = [...selected];
  const key =
    requirement.resource_type === "knowledge"
      ? "knowledge_base_ids"
      : requirement.resource_type === "skill"
        ? "skills"
        : "plugins";
  const selectedElsewhere = new Set(
    (proposal.capability_requirements || [])
      .filter((item) => capabilityRequirementKey(item) !== requirementKey)
      .filter((item) => item.resource_type === requirement.resource_type)
      .flatMap((item) => item.selected_ids || [])
      .map(String),
  );
  const removed = new Set(
    previous
      .filter((id) => !selected.some((value) => String(value) === String(id)))
      .map(String),
  );
  for (const agent of proposal.agents || []) {
    agent[key] = (agent[key] || []).filter(
      (id) => !removed.has(String(id)) || selectedElsewhere.has(String(id)),
    );
  }
  // The capability card confirms an application-level resource selection.
  // Do not attach it to the first Agent: the generation step assigns the
  // resource to the Agent whose task actually needs it.
  if (selected.length) {
    proposal.capability_blocked = (proposal.capability_blocked || []).filter(
      (item) => capabilityRequirementKey(item) !== requirementKey,
    );
  } else if (requirement.required !== false) {
    proposal.capability_blocked ||= [];
    if (
      !proposal.capability_blocked.some(
        (item) => capabilityRequirementKey(item) === requirementKey,
      )
    )
      proposal.capability_blocked.push(requirement);
  }
  confirmation.value = proposal;
}
function removeProposalCapability(proposal, requirement, id) {
  const selected = (requirement.selected_ids || []).filter(
    (item) => String(item) !== String(id),
  );
  setProposalCapabilities(proposal, requirement, selected);
}
async function finishCapabilityConfiguration(message) {
  const proposal = message?.proposal;
  if (!proposal || busy.value) return;
  if (missingProposalCapabilities(proposal).length) return;
  if (!proposal.preflight) {
    proposal.capability_card = false;
    proposal.capability_blocked = [];
    confirmation.value = proposal;
    scheduleDraft();
    return;
  }
  const submitted = JSON.parse(JSON.stringify(proposal));
  const history = messages.value
    .filter((item) => ["user", "assistant"].includes(item.role))
    .slice(-12)
    .map((item) => ({ role: item.role, content: item.text }));
  const confirmationText = "确认能力配置。";
  message.submitting = true;
  messages.value.push({ role: "user", text: confirmationText });
  messages.value.push({ role: "assistant", text: "", streaming: true });
  const answer = messages.value[messages.value.length - 1];
  busy.value = true;
  activity.value = { phase: "planning", plan: [] };
  try {
    await executeStudioRequest(confirmationText, answer, history, [], {
      proposal: submitted,
      kind: submitted.kind_preselected ? submitted.recommended_kind : "auto",
      kindPreselected: Boolean(submitted.kind_preselected),
      action: "confirm_capabilities",
    });
  } catch (error) {
    markStudioFailure(answer, error);
  } finally {
    busy.value = false;
    message.submitting = false;
    activity.value = null;
    scheduleDraft();
  }
}
function proposalResources(requirement) {
  if (requirement.resource_type === "knowledge")
    return store.knowledge.filter((item) => item.status === "ready");
  if (requirement.resource_type === "skill") return store.skills;
  return store.plugins;
}
function proposalCapabilityTypeLabel(requirement) {
  if (requirement?.resource_type === "knowledge") return "知识库";
  if (requirement?.resource_type === "skill") return "技能";
  return "工具";
}
function proposalCapabilityAddLabel(requirement) {
  return requirement?.resource_type === "skill"
    ? "添加新技能"
    : "添加" + proposalCapabilityTypeLabel(requirement);
}
const proposalCapabilityOptions = computed(() =>
  proposalCapabilityTarget.value
    ? proposalResources(proposalCapabilityTarget.value.requirement)
    : [],
);
function openProposalCapabilityPicker(proposal, requirement) {
  proposalCapabilityTarget.value = { proposal, requirement };
  proposalCapabilitySelection.value = [...(requirement.selected_ids || [])];
  proposalCapabilityPickerOpen.value = true;
}
function closeProposalCapabilityPicker() {
  proposalCapabilityPickerOpen.value = false;
  proposalCapabilityTarget.value = null;
  proposalCapabilitySelection.value = [];
}
function proposalCapabilitySelected(id) {
  return proposalCapabilitySelection.value.some(
    (value) => String(value) === String(id),
  );
}
function toggleProposalCapability(id) {
  proposalCapabilitySelection.value = proposalCapabilitySelected(id)
    ? proposalCapabilitySelection.value.filter(
        (value) => String(value) !== String(id),
      )
    : [...proposalCapabilitySelection.value, id];
}
function confirmProposalCapabilityPicker() {
  const target = proposalCapabilityTarget.value;
  if (!target) return;
  setProposalCapabilities(
    target.proposal,
    target.requirement,
    proposalCapabilitySelection.value,
  );
  closeProposalCapabilityPicker();
  scheduleDraft();
}
function capabilityRequirementKey(requirement) {
  return `${requirement?.resource_type || "unknown"}:${requirement?.id || requirement?.label || "unnamed"}`;
}
function missingProposalCapabilities(proposal) {
  const byId = new Map();
  for (const item of [
    ...(proposal?.capability_requirements || []),
    ...(proposal?.capability_blocked || []),
  ]) byId.set(capabilityRequirementKey(item), item);
  const requirements = [...byId.values()];
  return requirements.filter((requirement) => {
    if (requirement.required === false) return false;
    const available = new Set(
      proposalResources(requirement).map((item) => String(item.id)),
    );
    return !(requirement.selected_ids || []).some((id) =>
      available.has(String(id)),
    );
  });
}
function proposalHasMissingCapabilities(proposal) {
  return missingProposalCapabilities(proposal).length > 0;
}
function showCapabilityCard(proposal) {
  return Boolean(proposal?.capability_card);
}
function capabilityCardRequirements(proposal) {
  return proposal?.capability_requirements || proposal?.capability_blocked || [];
}
async function openCapabilityManager(requirement) {
  closeProposalCapabilityPicker();
  if (route.query.session && confirmation.value) {
    try {
      await api.updateStudioSession(workflow.value.id, {
        proposal: confirmation.value,
        kind: selectedKind.value || workflow.value.kind,
        title: workflow.value.name,
      });
      draftDirty = false;
    } catch (error) {
      store.error = error.message;
      return;
    }
  }
  const returnTo = route.fullPath;
  if (requirement.resource_type === "skill") {
    await router.push({ path: "/skill-dev", query: { returnTo } });
  } else if (requirement.resource_type === "knowledge") {
    await router.push({ path: "/knowledge", query: { create: "1", returnTo } });
  } else {
    await router.push({ path: "/resources", query: { tab: "actions", create: "1", returnTo } });
  }
}
function short(text, length = 82) {
  return text?.length > length ? `${text.slice(0, length)}...` : text;
}
function openCreateDetails(kind) {
  if (
    builderReady.value &&
    workflow.value.kind !== kind &&
    (workflow.value.agents.length || workflow.value.tasks.length)
  ) {
    store.error = "已有节点的草稿不能直接切换 Crew/Flow，请新建草稿。";
    return;
  }
  automationDetailsMode.value = "create";
  automationDetailsKind.value = kind;
  automationDetailsOpen.value = true;
}
function editWorkflowDetails() {
  automationDetailsMode.value = "edit";
  automationDetailsKind.value = workflow.value.kind;
  automationDetailsOpen.value = true;
}
function cancelAutomationDetails() {
  automationDetailsOpen.value = false;
  if (automationDetailsMode.value === "create" && route.name === "studio-new") {
    selectedKind.value = null;
    kindPreselected.value = false;
    router.replace("/new-automation");
  }
}
function confirmAutomationDetails(details) {
  workflow.value.name = details.name;
  workflow.value.description = details.description;
  if (automationDetailsMode.value === "create") {
    selectedKind.value = automationDetailsKind.value;
    kindPreselected.value = true;
    workflow.value.kind = automationDetailsKind.value;
    workflow.value.structure_confirmed = false;
    saveState.value = "基本信息已填写，等待编排";
  }
  automationDetailsOpen.value = false;
  draftDirty = true;
  scheduleDraft();
}
function chooseStructure(kind) {
  openCreateDetails(kind);
}
</script>

<template>
  <div class="studio-page" :class="{ 'is-loading': pageLoading }">
    <div v-if="pageLoading" class="studio-loading" role="status"><LoaderCircle class="spin" :size="26" /><span>正在加载编排…</span></div>
    <header class="studio-toolbar">
      <div class="studio-title">
        <GitBranch v-if="workflow.kind === 'flow'" :size="17" /><UsersRound
          v-else
          :size="17"
        /><input v-model="workflow.name" aria-label="Workflow name" /><button
          v-if="builderReady"
          class="icon-button"
          type="button"
          title="编辑名称和介绍"
          aria-label="编辑名称和介绍"
          @click="editWorkflowDetails"
        >
          <Pencil :size="13" />
        </button><span
          class="save-state"
          >{{ saveState }}</span
        ><button class="icon-button assistant-toggle natural-language-trigger" type="button" title="自然语言编排" aria-label="打开自然语言编排" :aria-pressed="assistantOpen" @click="assistantOpen = !assistantOpen"><span class="natural-language-trigger-icon"><MessageCircle :size="16" /><Sparkles :size="9" /></span></button>
      </div>
      <div class="toolbar-right">
        <button v-if="!route.params.id" class="button" @click="startNewSession">
          <Plus :size="14" />新对话
        </button>
        <span class="workflow-kind-lock"
          ><GitBranch v-if="workflow.kind === 'flow'" :size="14" /><UsersRound
            v-else
            :size="14" />{{
            builderReady
              ? workflow.kind === "flow"
                ? "Flow"
                : "Crew"
              : "Auto"
          }}<LockKeyhole v-if="builderReady" :size="12"
        /></span>
        <button
          v-if="builderReady"
          class="button orchestration-settings-button"
          :class="{ active: inspectorSettingsOpen && !selectedAgent && !selectedTask && !selectedEdge }"
          :aria-pressed="inspectorSettingsOpen && !selectedAgent && !selectedTask && !selectedEdge"
          @click="toggleOrchestrationSettings"
        >
          <Settings2 :size="14" />编排设置
        </button>
        <button class="button" @click="save"><Save :size="14" />保存</button
        ><button class="button" @click="publish">
          <Check :size="14" />{{
          workflow.published || workflow.status === "published" ? "发布更新" : "发布"
          }}</button
        ><button
          class="button primary"
          :disabled="!workflow.tasks.length || !store.chatModels.length"
          @click="launchRun"
        >
          <Play :size="14" />{{
            "预览运行"
          }}
        </button>
      </div>
    </header>

    <div
      class="studio-layout"
      :class="{
        'awaiting-structure': !builderReady,
        'has-inspector': inspectorVisible,
      }"
    >
      <aside v-show="assistantOpen" class="studio-panel studio-assistant-drawer">
        <div class="studio-panel-head">
          <strong>自然语言编排</strong><button class="icon-button" type="button" title="收起自然语言编排" aria-label="收起自然语言编排" @click="assistantOpen = false"><X :size="16" /></button>
        </div>
        <div ref="assistantThread" class="assistant-thread" @scroll="onAssistantScroll">
          <div v-if="historyLoading" class="assistant-history-loading" role="status">
            正在加载更早的消息…
          </div>
          <div v-else-if="historyHasMore" class="assistant-history-hint">
            上滑加载更早的消息
          </div>
          <div
            v-for="(message, index) in messages"
            :key="index"
            class="assistant-message"
            :class="[message.role, { streaming: message.streaming }]"
          >
            <div class="message-role">
              {{
                message.role === "user"
                  ? "You"
                  : message.role === "error"
                    ? "Error"
                    : "Studio"
              }}
            </div>
            <div v-if="message.attachments?.length" class="message-files">
              <span v-for="file in message.attachments" :key="file.id"
                ><component :is="attachmentIcon(file)" :size="12" />{{
                  file.name
                }}</span
              >
            </div>
            <div
              v-if="message.pending && !message.text"
              class="message-pending"
            >
              {{ pendingStageText(message) }}
            </div>
            <RichMessage class="message-copy" :text="message.text" :files="message.files || []" />
            <div v-if="message.role === 'error' && message.error" class="assistant-error-actions">
              <button
                v-if="isRetryableStudioMessage(index)"
                class="button ghost chat-error-retry"
                type="button"
                :disabled="busy || message.retrying"
                @click="retryStudioMessage(message)"
              >
                <LoaderCircle v-if="message.retrying" class="spin" :size="13" />
                <RotateCcw v-else :size="13" />
                {{ message.retrying ? "正在重试…" : "重试此轮" }}
              </button>
            </div>
            <span v-if="message.streaming" class="stream-caret"></span>

            <section
              v-if="message.clarification"
              class="studio-clarification"
              :class="{ locked: message.clarification.locked }"
            >
              <span class="eyebrow">需要确认</span>
              <strong>{{ message.clarification.question }}</strong>
              <div class="clarification-options">
                <button
                  v-for="option in message.clarification.options"
                  :key="option.value"
                  type="button"
                  :class="{
                    selected: message.clarification.selected === option.value,
                    recommended: option.recommended,
                  }"
                  :disabled="message.clarification.locked || busy"
                  @click="chooseClarificationOption(message, option)"
                >
                  <span
                    ><b>{{ option.label }}</b
                    ><Check
                      v-if="message.clarification.selected === option.value"
                      :size="13"
                  /></span>
                  <small v-if="option.description">{{
                    option.description
                  }}</small>
                </button>
              </div>
              <p v-if="message.clarification.locked">
                已选择：{{
                  message.clarification.selectedLabel ||
                  message.clarification.selected
                }}
              </p>
              <p v-else-if="message.clarification.allow_custom">
                也可以直接在下方输入你的具体要求。
              </p>
            </section>

            <section
              v-if="showCapabilityCard(message.proposal) && !message.clarification"
              class="input-contract capability-proposal"
              :class="{ submitting: message.submitting }"
            >
              <header>
                <div><span class="eyebrow">能力配置</span><strong>{{ message.proposal.preflight ? "确认运行能力" : "补充必要能力" }}</strong></div>
                <span class="proposal-state">{{ message.proposal.preflight ? "进入架构前确认" : "生成前必须处理" }}</span>
              </header>
              <p class="proposal-summary"><strong>已配置能力</strong>：已根据需求默认添加匹配的资源。可以同时添加或删除多个 Skill、Tool 和知识库；标记为必需的能力补齐后才能继续。</p>
              <div v-for="requirement in capabilityCardRequirements(message.proposal)" :key="capabilityRequirementKey(requirement)" class="proposal-capability">
                <div><strong>{{ requirement.label }}</strong><span v-if="requirement.required !== false && !(requirement.selected_ids || []).length" class="status-badge failed">必需，尚未满足</span><p>{{ requirement.reason }}</p></div>
                <div v-if="requirement.selected_ids?.length" class="proposal-capability-actions">
                  <button v-for="id in requirement.selected_ids" :key="id" class="button small" @click="removeProposalCapability(message.proposal, requirement, id)">
                    {{ capabilityLabel(id, requirement.resource_type) }} <X :size="12" />
                  </button>
                </div>
                <div class="proposal-capability-actions">
                  <button class="button small" @click="openProposalCapabilityPicker(message.proposal, requirement)">
                    <Plus :size="12" />{{ proposalCapabilityAddLabel(requirement) }}
                  </button>
                </div>
              </div>
              <button v-if="isActiveProposal(index)" class="button primary contract-confirm" :disabled="busy || message.submitting || proposalHasMissingCapabilities(message.proposal)" @click="finishCapabilityConfiguration(message)">
                <Check :size="13" />{{ message.proposal.preflight ? "确认能力配置，继续选择类型" : "能力配置完成，返回方案确认" }}
              </button>
            </section>
            <section
              v-if="message.proposal && ['inputs', 'architecture'].includes(message.proposal.stage) && !message.proposal.preflight && !message.clarification && !showCapabilityCard(message.proposal)"
              class="input-contract architecture-proposal"
              :class="{
                submitting: message.submitting,
                superseded:
                  !isActiveProposal(index) &&
                  !message.proposal.confirmed_stages.includes(
                    message.proposal.stage,
                  ),
              }"
              :inert="
                proposalStageLocked(message.proposal, message.proposal.stage) ||
                (!isActiveProposal(index) &&
                  !message.proposal.confirmed_stages.includes(
                    message.proposal.stage,
                  ))
              "
            >
              <header>
                <div>
                  <span class="eyebrow">方案确认</span
                  ><strong>{{
                    message.proposal.preflight
                      ? "确认编排前提"
                      : message.proposal.stage === "inputs"
                        ? "确认运行输入"
                        : "确认编排架构"
                  }}</strong>
                </div>
                <span class="proposal-state">{{
                  message.submitting
                    ? "已确认，正在检查"
                    : message.proposal.confirmed_stages.includes(
                          message.proposal.stage,
                        )
                      ? "已确认"
                      : "待确认"
                }}</span>
              </header>
              <p class="proposal-summary">{{ message.proposal.summary }}</p>

              <section
                v-if="message.proposal.stage === 'inputs' && !message.proposal.preflight"
                class="proposal-section proposal-inputs"
              >
                <header>
                  <strong>运行输入</strong><span>{{ proposalStageLocked(message.proposal, "inputs") ? "已锁定" : "确认前可编辑" }}</span
                  ><button
                    v-if="!proposalStageLocked(message.proposal, 'inputs')"
                    class="icon-button"
                    title="添加输入字段"
                    @click="addContractInput"
                  >
                    <Plus :size="13" />
                  </button>
                </header>
                <div
                  v-for="(item, inputIndex) in message.proposal.inputs"
                  :key="inputIndex"
                  class="contract-input-row"
                >
                  <div class="contract-input-main">
                    <input v-model="item.label" aria-label="输入名称" :disabled="proposalStageLocked(message.proposal, 'inputs')" /><input
                      v-model="item.name"
                      aria-label="输入变量"
                      :disabled="item.name === 'message' || proposalStageLocked(message.proposal, 'inputs')"
                    />
                  </div>
                  <div class="contract-input-options">
                    <select v-model="item.input_type" @change="normalizeProposalInputType(item)" :disabled="item.name === 'message' || proposalStageLocked(message.proposal, 'inputs')">
                      <option value="text">短文本</option>
                      <option value="long_text">长文本</option>
                      <option value="file">文件</option>
                      <option value="image">图片</option>
                      <option value="number">数字</option>
                      <option value="boolean">开关</option>
                      <option value="json">JSON</option></select
                    ><label
                      ><input
                        v-model="item.required"
                        type="checkbox"
                        :disabled="item.name === 'message' || proposalStageLocked(message.proposal, 'inputs')"
                      />必填</label
                    ><button
                      v-if="item.name !== 'message' && !proposalStageLocked(message.proposal, 'inputs')"
                      class="icon-button"
                      title="删除输入字段"
                      @click="removeContractInput(inputIndex)"
                    >
                      <Trash2 :size="12" />
                    </button>
                  </div>
                  <input
                    v-model="item.description"
                    class="contract-description"
                    placeholder="说明这个输入如何使用"
                    :disabled="proposalStageLocked(message.proposal, 'inputs')"
                  />
                </div>
              </section>

              <template v-if="message.proposal.stage === 'architecture'">
                <section
                  v-if="message.proposal.stage === 'architecture'"
                  class="proposal-section proposal-kind-section"
                >
                  <header>
                    <strong>编排类型</strong><span>确认前可切换</span>
                  </header>
                  <div v-if="!message.proposal.kind_preselected" class="proposal-kind-picker" role="group" aria-label="选择编排类型">
                    <button
                      type="button"
                      :class="{ active: message.proposal.recommended_kind === 'crew' }"
                      :disabled="!isActiveProposal(index) || message.submitting"
                      @click="setProposalKind(message.proposal, 'crew')"
                    >
                      <UsersRound :size="15" /><strong>Crew</strong><small>顺序或层级协作</small>
                    </button>
                    <button
                      type="button"
                      :class="{ active: message.proposal.recommended_kind === 'flow' }"
                      :disabled="!isActiveProposal(index) || message.submitting"
                      @click="setProposalKind(message.proposal, 'flow')"
                    >
                      <GitBranch :size="15" /><strong>Flow</strong><small>状态、分支与确定性节点</small>
                    </button>
                  </div>
                  <p v-if="!message.proposal.kind_preselected" class="proposal-kind-note">确认后才会锁定类型并生成最终节点。</p>
                  <p v-else class="proposal-kind-note">编排类型已在前置确认中锁定。</p>
                </section>
                <p
                  v-if="message.proposal.architecture_reason"
                  class="proposal-reason"
                >
                  {{ message.proposal.architecture_reason }}
                </p>
                <section class="proposal-section">
                  <header>
                    <strong>子智能体</strong
                    ><span>{{ message.proposal.agents.length }} 个</span>
                  </header>
                  <div
                    v-for="(agent, agentIndex) in message.proposal.agents"
                    :key="`${agent.role}-${agentIndex}`"
                    class="proposal-agent"
                  >
                    <span>{{ agentIndex + 1 }}</span>
                    <div>
                      <strong>{{ agent.role }}</strong>
                      <p>{{ agent.purpose || agent.goal }}</p>
                      <small v-if="agent.backstory">{{ agent.backstory }}</small>
                      <small v-if="agent.responsibilities.length">{{
                        agent.responsibilities.join(" · ")
                      }}</small
                      ><small v-if="agent.tools.length"
                        >工具：{{
                          agent.tools.map((id) => capabilityLabel(id, "tool")).join("、")
                        }}</small
                      >
                    </div>
                  </div>
                </section>
                <section class="proposal-section">
                  <header>
                    <strong>执行计划</strong
                    ><span>{{ message.proposal.tasks.length }} 步</span>
                  </header>
                  <div
                    v-for="(task, taskIndex) in message.proposal.tasks"
                    :key="`${task.name}-${taskIndex}`"
                    class="proposal-task"
                  >
                    <span>{{ taskIndex + 1 }}</span>
                    <div>
                      <strong>{{ task.name }}</strong>
                      <p>{{ task.objective }}</p>
                      <small
                        >{{ task.agent_role || task.node_type
                        }}<template v-if="task.depends_on.length">
                          · 依赖 {{ task.depends_on.join("、") }}</template
                        ><template v-if="task.node_type === 'crew'">
                          ·
                          {{
                            task.crew_process === "hierarchical"
                              ? "层级"
                              : "顺序"
                          }}
                          Crew</template
                        ></small
                      >
                    </div>
                  </div>
                </section>
              </template>
              <div v-if="message.proposal.notes.length" class="contract-notes">
                <span v-for="note in message.proposal.notes" :key="note">{{
                  note
                }}</span>
              </div>
              <p class="proposal-loop-hint">
                确认后本卡片会立即锁定并留在当前消息位置。需要修改已确认内容时，直接在下方聊天说明。
              </p>
              <button
                v-if="isActiveProposal(index)"
                class="button primary contract-confirm"
                :disabled="
                  busy ||
                  message.submitting ||
                  proposalHasMissingCapabilities(message.proposal) ||
                  (!message.proposal.inputs.length &&
                    message.proposal.stage === 'inputs')
                "
                @click="confirmProposalStage(message, index)"
              >
                <LoaderCircle
                  v-if="message.submitting"
                  class="spin"
                  :size="13"
                /><Check v-else :size="13" />{{
                  message.submitting
                    ? "已确认，正在检查…"
                    : message.proposal.stage === "inputs"
                      ? "确认运行输入"
                      : "确认编排架构"
                }}
              </button>
            </section>
          </div>
          <div v-if="busy && !messages.at(-1)?.text" class="assistant-thinking">
            <span></span><span></span><span></span>
          </div>
          <div v-if="activity?.plan?.length" class="assistant-plan">
            <span
              v-for="(item, index) in activity.plan.slice(0, 5)"
              :key="item"
              :class="{ active: index === 0 || activity.phase !== 'planning' }"
              ><Check :size="10" />{{ item }}</span
            >
          </div>
        </div>
        <div v-if="!store.chatModels.length" class="model-required">
          <span><Cpu :size="18" /></span><strong>需要模型连接</strong>
          <p>添加至少一个模型后，才能通过自然语言生成或调整编排。</p>
          <button class="button accent" @click="router.push('/models')">
            添加模型
          </button>
        </div>
        <div
          v-else-if="!store.defaultModel && !workflow.model_profile_id"
          class="model-required"
        >
          <span><Cpu :size="18" /></span><strong>需要默认模型</strong>
          <p>请选择这个工作空间用于编排和运行的默认模型。</p>
          <button class="button accent" @click="router.push('/model-default')">
            设置默认模型
          </button>
        </div>
        <div v-else class="assistant-composer">
          <div v-if="uploading" class="upload-status">
            <div class="upload-status-head">
              <span>正在上传 {{ pendingUploadNames.join("、") }}</span
              ><b>{{ uploadProgress }}%</b>
            </div>
            <div class="upload-progress-track">
              <i :style="{ width: `${uploadProgress}%` }"></i>
            </div>
          </div>
          <div v-if="attachments.length" class="composer-files">
            <span v-for="file in attachments" :key="file.id"
              ><component :is="attachmentIcon(file)" :size="13" /><b>{{
                file.name
              }}</b
              ><small>{{ formatBytes(file.size) }}</small
              ><button title="移除附件" @click="removeAttachment(file.id)">
                <X :size="12" /></button
            ></span>
          </div>
          <textarea
            v-model="prompt"
            :disabled="busy"
            placeholder="输入消息，Shift + Enter 换行"
            @keydown.enter.exact.prevent="sendMessage"
          ></textarea
          ><input
            ref="fileInput"
            type="file"
            hidden
            multiple
            accept="image/*,.pdf,.txt,.md,.csv,.json,.doc,.docx,.xls,.xlsx"
            @change="chooseAttachments"
          />
          <div class="assistant-actions">
            <button
              class="icon-button composer-attach"
              :disabled="busy || uploading"
              title="添加文件或图片"
              @click="fileInput?.click()"
            >
              <LoaderCircle
                v-if="uploading"
                class="spin"
                :size="14"
              /><Paperclip v-else :size="14" /></button
            ><select v-model="workflow.model_profile_id" :disabled="busy">
              <option :value="null">
                默认模型 · {{ store.defaultModel?.name }}
              </option>
              <option
                v-for="model in store.chatModels"
                :key="model.id"
                :value="model.id"
              >
                {{ model.name }}
              </option></select
            ><button
              class="icon-button composer-send"
              :disabled="
                busy || uploading || (!prompt.trim() && !attachments.length)
              "
              title="发送"
              @click="sendMessage"
            >
              <LoaderCircle v-if="busy" class="spin" :size="14" /><Send
                v-else
                :size="14"
              />
            </button>
          </div>
        </div>
      </aside>

      <section v-if="!builderReady" class="creation-chooser">
        <div class="creation-chooser-inner">
          <span class="eyebrow">CHOOSE AN ORCHESTRATION</span>
          <h2>创建 Crew 或 Flow</h2>
          <p>
            前置选择完成后会依次确认运行输入和编排架构，然后直接生成可运行画布。
          </p>
          <div class="creation-options">
            <button class="creation-option" @click="chooseStructure('crew')">
              <span><UsersRound :size="20" /></span>
              <div>
                <strong>创建 Crew</strong>
                <p>
                  适合目标明确、一次性执行的多智能体协作，例如研究、撰写、审校和报告生成。
                </p>
                <small>Task context · Sequential / Hierarchical</small>
              </div>
            </button>
            <button class="creation-option" @click="chooseStructure('flow')">
              <span><GitBranch :size="20" /></span>
              <div>
                <strong>创建 Flow</strong>
                <p>
                  适合需要状态、条件分支、人工审批、事件触发或多个 Crew
                  串联的长期流程。
                </p>
                <small>State · Routing · Human feedback</small>
              </div>
            </button>
          </div>
          <section v-if="historyProjects.length" class="studio-history">
            <header>
              <div>
                <span class="eyebrow">RECENT PROJECTS</span
                ><strong>最近项目</strong>
              </div>
              <button class="text-button" @click="router.push('/automations')">
                查看全部 <ArrowUpRight :size="12" />
              </button>
            </header>
            <div class="studio-history-grid">
              <article
                v-for="item in historyProjects"
                :key="item.id"
                class="studio-history-item"
                @click="openHistoryProject(item)"
              >
                <span class="studio-history-icon"><GitBranch v-if="item.kind === 'flow'" :size="15" /><UsersRound v-else :size="15" /></span>
                <div>
                  <strong>{{ item.name }}</strong>
                  <p>{{ short(item.description, 64) }}</p>
                  <small
                    >{{ item.local ? "未完成对话" : item.status }} ·
                    {{ formatBeijingDateTime(item.updated_at) }}</small
                  >
                </div>
                <button
                  v-if="item.local || (item.remote && !item.application_id)"
                  class="icon-button"
                  title="删除历史项目"
                  @click.stop="removeHistoryProject(item)"
                >
                  <X :size="12" /></button
                ><ArrowUpRight v-else :size="14" />
              </article>
            </div>
          </section>
        </div>
      </section>

      <section v-if="builderReady" class="canvas-wrap">
        <VueFlow
          id="studio-flow"
          :nodes="graphNodes"
          :edges="graphEdges"
          :connect-on-click="false"
          :delete-key-code="null"
          :max-zoom="1.15"
          :is-valid-connection="isValidConnection"
          fit-view-on-init
          @pane-ready="paneReady"
          @connect="connect"
          @node-click="nodeClick"
          @edge-click="edgeClick"
          @pane-click="clearSelection"
          @node-drag-stop="nodeDragStop"
        >
          <template #node-agentDef="{ data, selected }"
            ><div class="flow-node agent-definition" :class="{ selected }">
              <div class="flow-node-head">
                <span class="flow-node-type"
                  ><Bot :size="12" />Agent definition</span
                ><GripVertical :size="13" />
              </div>
              <div class="flow-node-body">
                <strong>{{ data.agent.role }}</strong>
                <p>{{ short(data.agent.goal) }}</p>
                <small class="flow-node-agent-intro">{{ short(data.agent.backstory, 96) }}</small>
              </div>
              <div class="flow-node-agent">
                <span class="agent-dot"><Wrench :size="13" /></span>
                <div>
                  <b>{{ data.model }}</b
                  ><small>{{ data.bindings }} capability bindings</small>
                </div>
              </div>
              <Handle
                id="agent-out"
                type="source"
                :position="Position.Bottom"
              /></div
          ></template>
          <template #node-step="{ data, selected }"
            ><div
              class="flow-node"
              :class="[{ selected }, `type-${data.task.node_type}`]"
            >
              <Handle
                id="context-in"
                type="target"
                :position="Position.Left"
              /><Handle
                v-if="!['router','code','tool'].includes(data.task.node_type)"
                id="agent-in"
                type="target"
                :position="Position.Top"
              />
              <div class="flow-node-head">
                <span class="flow-node-type"
                  ><Route
                    v-if="data.task.node_type === 'router'"
                    :size="12"
                  /><UsersRound
                    v-else-if="data.task.node_type === 'crew'"
                    :size="12"
                  /><ListTodo
                    v-else-if="data.task.node_type === 'task'"
                    :size="12"
                  /><Bot v-else :size="12" />{{ data.type.label
                  }}<em v-if="data.task.human_feedback">Review</em></span
                ><GripVertical :size="13" />
              </div>
              <div class="flow-node-body">
                <strong>{{ data.task.name }}</strong>
                <p>{{ short(data.task.description) }}</p>
              </div>
              <div v-if="data.task.node_type === 'router'" class="router-branch-ports">
                <div v-for="(branch, index) in routerCases(data.task)" :key="branch.id" class="router-branch-port">
                  <div class="router-branch-main"><span>{{ branch.label }}</span><small>{{ data.task.routes?.[branch.id]?.length || 0 }} 个下游</small></div>
                  <small class="router-branch-summary" :title="branch.summary">{{ short(branch.summary, 54) }}</small>
                  <Handle
                    :id="`route:${branch.id}`" type="source" :position="Position.Right"
                    :style="{ right: '-7px' }"
                  />
                </div>
              </div>
              <div v-if="data.task.node_type !== 'router'" class="flow-node-contract">
                <span class="flow-node-contract-label">固定输出</span>
                <span
                  v-for="field in (data.task.output_variables?.length
                    ? data.task.output_variables
                    : fixedOutputVariables(data.task.node_type))"
                  :key="field.name"
                  class="flow-node-output-chip"
                  :title="field.description || field.name"
                >
                  <code>{{ field.name }}</code>
                  <small>{{ field.value_type }}</small>
                </span>
              </div>
              <div class="flow-node-agent">
                <span class="agent-dot"
                  ><UserRound v-if="data.agent" :size="13" /><Route
                    v-else-if="data.task.node_type === 'router'"
                    :size="13" /><Cpu
                    v-else-if="data.task.node_type === 'code'"
                    :size="13" /><Wrench
                    v-else-if="data.task.node_type === 'tool'"
                    :size="13" /><UsersRound v-else :size="13"
                /></span>
                <div>
                  <b>{{
                    data.agent?.role ||
                    (data.task.node_type === "router"
                      ? "Deterministic condition"
                      : data.task.node_type === "code"
                        ? "Python code"
                        : data.task.node_type === "tool"
                          ? "Tool call"
                          : `${data.task.crew_agent_ids.length} Crew members`)
                  }}</b
                  ><small>{{
                    data.task.node_type === "router"
                      ? data.task.condition
                      : data.task.node_type === "code"
                        ? "main(...)"
                        : data.task.node_type === "tool"
                          ? (data.task.tool_id ? "已配置工具" : "未选择工具")
                      : data.model
                  }}</small>
                </div>
              </div>
              <Handle v-if="data.task.node_type !== 'router'"
                id="context-out"
                type="source"
                :position="Position.Right"
              /></div
          ></template>
          <Panel position="top-left" class="canvas-toolbar"
            ><div class="canvas-add-menu" @click.stop>
              <button
                class="icon-button canvas-add-trigger"
                title="添加节点"
                aria-label="添加节点"
                :aria-expanded="canvasAddMenuOpen"
                @click="canvasAddMenuOpen = !canvasAddMenuOpen"
              ><Plus :size="16" /></button>
              <div v-if="canvasAddMenuOpen" class="canvas-add-popover">
                <strong>添加节点</strong>
                <template v-if="workflow.kind === 'flow'">
                  <button type="button" @click="addCanvasNode('agent-definition')"><Bot :size="14" />Agent 定义</button>
                  <button type="button" @click="addCanvasNode('agent')"><Bot :size="14" />单 Agent</button>
                  <button type="button" @click="addCanvasNode('crew')"><UsersRound :size="14" />Crew</button>
                  <button type="button" @click="addCanvasNode('router')"><Route :size="14" />条件路由</button>
                  <button type="button" @click="addCanvasNode('code')"><Cpu :size="14" />代码</button>
                  <button type="button" @click="addCanvasNode('tool')"><Wrench :size="14" />工具</button>
                </template>
                <template v-else>
                  <button type="button" @click="addCanvasNode('agent-definition')"><Bot :size="14" />Agent 定义</button>
                  <button type="button" @click="addCanvasNode('task')"><ListTodo :size="14" />Task</button>
                </template>
              </div>
            </div><button class="icon-button" title="缩小" @click="zoomOut()">
              <ZoomOut :size="14" /></button
            ><button class="icon-button" title="放大" @click="zoomIn()">
              <ZoomIn :size="14" /></button
            ><button
              class="icon-button"
              title="适应画布"
              @click="fitView({ padding: 0.16, duration: 180 })"
            >
              <Maximize2 :size="14" /></button
          ></Panel>
        </VueFlow>
        <div
          v-if="!workflow.tasks.length && !workflow.agents.length"
          class="canvas-placeholder"
        >
          <div>
            <Sparkles :size="25" /><strong>{{
              builderReady
                ? "空白 " + workflow.kind.toUpperCase()
                : "从左侧对话开始"
            }}</strong>
            <p>
              {{
                builderReady
                  ? "添加 Agent 与执行步骤"
                  : "结构会在需求明确后出现"
              }}
            </p>
          </div>
        </div>
      </section>

      <aside v-if="builderReady" class="studio-panel right">
        <div class="studio-panel-head">
          <strong>{{
            selectedAgent
              ? "Agent 参数"
              : selectedTask
                ? "执行节点参数"
                : selectedEdge
                  ? "连接参数"
                  : "编排设置"
          }}</strong
          ><span
            v-if="selectedAgent || selectedTask || selectedEdge"
            class="key-hint"
            >Delete</span
          ><button
            class="icon-button studio-inspector-close"
            type="button"
            title="关闭属性面板"
            aria-label="关闭属性面板"
            @click="clearSelection"
          ><X :size="14" /></button>
        </div>
        <div v-if="selectedAgent" class="studio-panel-scroll">
          <section class="inspector-section">
            <h3>Agent · Agent（agent）</h3>
            <div class="field">
              <label
                ><ParamLabel
                  text="角色（role）"
                  help="Agent 在 CrewAI 中承担的职责名称。" /></label
              ><input v-model="selectedAgent.role" />
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="目标（goal）"
                  help="Agent 需要完成的长期目标；应能指导每次任务决策。" /></label
              ><VariableTextarea v-model="selectedAgent.goal" :variables="runInputOptions" />
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="背景（backstory）"
                  help="提供专业背景和工作边界，帮助模型稳定地扮演角色。" /></label
              ><VariableTextarea v-model="selectedAgent.backstory" :variables="runInputOptions" />
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="模型（llm）"
                  help="该 Agent 使用的 CrewAI LLM；留空则使用工作流默认模型。" /></label
              ><select v-model="selectedAgent.model_profile_id">
                <option :value="null">工作流默认（workflow_default）</option>
                <option
                  v-for="model in store.chatModels"
                  :key="model.id"
                  :value="model.id"
                >
                  {{ model.name }} · {{ model.model }}
                </option>
              </select>
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="工具调用模型（function_calling_llm）"
                  help="需要工具调用时使用的模型；留空则跟随 Agent 模型。" /></label
              ><select
                v-model="selectedAgent.function_calling_model_profile_id"
              >
                <option :value="null">跟随 Agent 模型（same_as_agent）</option>
                <option
                  v-for="model in store.chatModels"
                  :key="model.id"
                  :value="model.id"
                >
                  {{ model.name }}
                </option>
              </select>
            </div>
          </section>
          <section class="inspector-section">
            <h3>运行参数（runtime）</h3>
            <label class="toggle-row"
              ><span
                >推理（reasoning）<button
                  class="field-help"
                  type="button"
                  title="允许 Agent 在执行任务前反思并形成计划，适合复杂任务，响应会更慢。"
                  aria-label="推理帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedAgent.reasoning"
                type="checkbox"
                class="toggle" /></label
            ><label class="toggle-row"
              ><span
                >记忆（memory）<button
                  class="field-help"
                  type="button"
                  title="启用 CrewAI Agent 记忆，在后续任务或会话中召回已保存的信息；Crew 的共享记忆需要同时开启 Crew memory。"
                  aria-label="记忆帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedAgent.memory"
                type="checkbox"
                class="toggle" /></label
            ><label class="toggle-row"
              ><span>允许委派（allow_delegation）</span
              ><input
                v-model="selectedAgent.allow_delegation"
                type="checkbox"
                :disabled="!selectedAgentCanDelegate"
                class="toggle" /></label
            ><small v-if="!selectedAgentCanDelegate" class="field-help-text">仅层级 Crew 的管理 Agent 可开启。</small
            ><label class="toggle-row"
              ><span
                >压缩长上下文（respect_context_window）<button
                  class="field-help"
                  type="button"
                  title="接近上下文窗口上限时压缩历史内容，减少超限失败。"
                  aria-label="上下文压缩帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedAgent.respect_context_window"
                type="checkbox"
                class="toggle" /></label
            ><label class="toggle-row"
              ><span>多模态文件（multimodal）</span
              ><input
                v-model="selectedAgent.multimodal"
                type="checkbox"
                class="toggle"
            /></label>
            <div class="field">
              <label
                ><ParamLabel
                  text="最大迭代次数（max_iter）"
                  help="Agent 单个任务最多进行多少轮思考/工具调用。" /></label
              ><input
                v-model.number="selectedAgent.max_iter"
                type="number"
                min="1"
                max="50"
              />
            </div>
            <div class="field two-col">
              <span
                ><label>每分钟请求数（max_rpm）</label
                ><input
                  v-model.number="selectedAgent.max_rpm"
                  type="number"
                  min="1"
                  placeholder="不限制" /></span
              ><span
                ><label
                  ><ParamLabel
                    text="硬超时秒数（max_execution_time）"
                    help="CrewAI 的总时长硬限制。Studio 流式运行不会应用该限制，避免仍在输出时被误判；导出代码执行时生效。通常建议留空。" /></label
                ><input
                  v-model.number="selectedAgent.max_execution_time"
                  type="number"
                  min="1"
                  placeholder="不设置（推荐）"
              /></span>
            </div>
            <div class="field two-col">
              <span
                ><label>重试次数（max_retry_limit）</label
                ><input
                  v-model.number="selectedAgent.max_retry_limit"
                  type="number"
                  min="0"
                  max="10" /></span
              ><span
                ><label>推理尝试次数（max_reasoning_attempts）</label
                ><input
                  v-model.number="selectedAgent.max_reasoning_attempts"
                  type="number"
                  min="1"
                  placeholder="自动"
              /></span>
            </div>
          </section>
          <section class="inspector-section">
            <h3>执行设置（execution）</h3>
            <label class="toggle-row"
              ><span
                >代码与命令执行（allow_code_execution）<button
                  class="field-help"
                  type="button"
                  title="启用后，Agent 可在玄枢隔离执行器中运行 Python 或 shell 命令。可联网下载字体和依赖，但只能读取或修改当前应用工作目录。"
                  aria-label="代码执行帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedAgent.allow_code_execution"
                type="checkbox"
                class="toggle"
            /></label>
            <label class="toggle-row"
              ><span
                >与用户交互（ask_user）<button
                  class="field-help"
                  type="button"
                  title="开启后，只为该 Agent 绑定 ask_user。它可在缺少信息时通过平台 message 聊天通道向用户提问，暂停当前节点并在同一会话中恢复。"
                  aria-label="用户交互帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedAgent.user_interaction"
                type="checkbox"
                class="toggle"
                @change="configureAgentInteraction(selectedAgent)"
            /></label>
            <p v-if="selectedAgent.user_interaction" class="inspector-hint">
            </p>
            <label class="toggle-row"
              ><span>注入当前日期（inject_date）</span
              ><input
                v-model="selectedAgent.inject_date"
                type="checkbox"
                class="toggle"
            /></label>
            <div v-if="selectedAgent.inject_date" class="field">
              <label>日期格式（date_format）</label
              ><input v-model="selectedAgent.date_format" />
            </div>
            <label class="toggle-row"
              ><span>使用系统提示词（use_system_prompt）</span
              ><input
                v-model="selectedAgent.use_system_prompt"
                type="checkbox"
                class="toggle"
            /></label>
          </section>
          <section class="inspector-section capability-section">
            <div class="section-title-action">
              <h3>Skill、工具与知识库</h3>
              <button class="button small" @click="capabilityPickerOpen = true">
                <Plus :size="12" />导入
              </button>
            </div>
            <p
              v-if="
                !attachedSkills.length &&
                !attachedPlugins.length &&
                !attachedKnowledge.length
              "
              class="inspector-empty capability-empty"
            >
              尚未导入 Skill、工具或知识库。
            </p>
            <div
              v-for="knowledge in attachedKnowledge"
              :key="knowledge.id"
              class="resource-item capability-bound"
            >
              <span><BookOpen :size="13" /></span>
              <div>
                <strong>{{ knowledge.name }}</strong
                ><small>知识库（knowledge）</small>
              </div>
              <button
                class="icon-button"
                title="移除知识库"
                @click.stop="
                  detachCapability('knowledge_base_ids', knowledge.id)
                "
              >
                <X :size="11" />
              </button>
            </div>
            <div
              v-for="skill in attachedSkills"
              :key="skill.id"
              class="resource-item capability-bound"
            >
              <span><Library :size="13" /></span>
              <div>
                <strong>{{ skill.name }}</strong
                ><small>Skill（skill）</small>
              </div>
              <button
                class="icon-button"
                title="移除 Skill"
                @click.stop="detachCapability('skills', skill.id)"
              >
                <X :size="11" />
              </button>
            </div>
            <div
              v-for="plugin in attachedPlugins"
              :key="plugin.id"
              class="resource-item capability-bound"
            >
              <span><Wrench :size="13" /></span>
              <div>
                <strong>{{ plugin.name }}</strong
                ><small>工具（tool）</small>
              </div>
              <button
                class="icon-button"
                title="移除工具"
                @click.stop="detachCapability('plugins', plugin.id)"
              >
                <X :size="11" />
              </button>
            </div>
          </section>
          <button class="button danger" @click="removeAgent">
            <Trash2 :size="14" />删除 Agent
          </button>
        </div>
        <div v-else-if="selectedTask" class="studio-panel-scroll">
          <section
            v-if="selectedTask.node_type === 'crew'"
            class="inspector-section embedded-crew-process"
          >
            <CrewSettingsEditor
              :model-value="selectedTask"
              prefix="crew_"
              :agents="workflow.agents.filter((agent) => (selectedTask.crew_agent_ids || []).includes(agent.id))"
              :models="store.chatModels"
              title="Crew 节点设置"
              @process-change="changeEmbeddedCrewProcess(selectedTask, $event)"
              @manager-change="changeEmbeddedCrewManager(selectedTask, $event)"
            />
          </section>
          <section class="inspector-section">
            <h3>
              {{
                workflow.kind === "crew"
                  ? "任务（Task）"
                  : "Flow 执行节点（flow_step）"
              }}
            </h3>
            <div class="field">
              <label
                ><ParamLabel
                  text="节点类型（node_type）"
                  help="agent：调用一个已连线的 Agent；crew：调用由多个已连线 Agent 组成的 Crew；router：只做确定性分支；code/tool：直接执行确定性步骤。" /></label
              ><div class="tag">{{ typeInfo[selectedTask.node_type]?.label || '任务' }}</div>
              <small>类型在创建节点时确定；需要其他类型时请新增节点。</small>
            </div>
            <div class="field">
              <label>名称（name）</label
              ><input
                v-model="selectedTask.name"
              />
            </div>
            <div v-if="!['code', 'tool'].includes(selectedTask.node_type)" class="field">
              <label
                ><ParamLabel
                  text="任务描述（description）"
                  help="告诉 Agent 要做什么；可以使用已声明的运行输入变量，例如 {contract_file}。" /></label
              ><VariableTextarea v-model="selectedTask.description" :variables="taskVariableOptions" />
            </div>
            <section v-if="selectedTask.node_type === 'tool'" class="node-execution-card tool-node-editor">
              <header class="node-editor-title">
                <div><span class="node-editor-kicker">TOOL INPUT</span><h4>选择已注册工具</h4></div>
                <Wrench :size="16" />
              </header>
              <div class="field">
                <label>执行工具（tool_id）</label>
                <select :value="selectedTask.tool_id || ''" @change="selectFlowTool(selectedTask, $event.target.value)">
                  <option value="">选择工作空间中的工具</option>
                  <option v-for="tool in flowTools" :key="tool.id" :value="String(tool.id)">{{ tool.name }} · {{ tool.kind }}</option>
                </select>
              </div>
              <div v-if="selectedFlowTool" class="selected-tool-summary">
                <div class="selected-tool-icon"><Wrench :size="15" /></div>
                <div><strong>{{ selectedFlowTool.name }}</strong><small>{{ selectedFlowTool.description || '已注册工具' }}</small></div>
              </div>
              <p v-if="!flowTools.length" class="node-editor-empty">工作空间还没有工具。请先在资源中心创建工具，再回到这里选择。</p>
              <p v-else-if="!selectedFlowTool" class="node-editor-empty">选择工具后，输入参数会按照该工具的 schema 自动生成。</p>
              <NodeInputBindings
                v-if="selectedFlowTool"
                :model-value="selectedTask.input_bindings"
                @update:model-value="updateDeterministicBindings(selectedTask, $event)"
                title="工具输入参数"
                :schema="selectedToolInputSchema"
                :variables="selectedNodeVariableOptions"
                locked
              />
            </section>
            <section v-else-if="selectedTask.node_type === 'code'" class="node-execution-card code-node-editor">
              <header class="node-editor-title">
                <div><span class="node-editor-kicker">CODE INPUT</span><h4>定义可复用的代码步骤</h4></div>
                <Cpu :size="16" />
              </header>
              <NodeInputBindings
                :model-value="selectedTask.input_bindings"
                @update:model-value="updateDeterministicBindings(selectedTask, $event)"
                title="代码输入参数"
                description="参数由 Python main(...) 签名决定；来源统一为变量或固定值，变量可选运行输入和所有前置节点输出。"
                :schema="selectedCodeInputSchema"
                :variables="selectedNodeVariableOptions"
                editable-schema
              />
              <div class="field code-editor-field">
                <label>Python 代码（code_snippet）</label>
                <CodeEditor v-model="selectedTask.code_snippet" language="python" min-height="220px" placeholder='def main(message):\n    return {"text": message}' />
                <small>只定义 <code>main(显式参数)</code> 并返回对象；输入列表会从签名同步，平台统一包装为 <code>{"output": 返回对象}</code>。不要在代码里读取外部路径。</small>
              </div>
            </section>
            <div v-if="!['router', 'code', 'tool'].includes(selectedTask.node_type)" class="field">
              <label
                ><ParamLabel
                  text="期望输出（expected_output）"
                  help="CrewAI 将它写入任务提示，用于指导 object 与 text 的内容；平台会校验固定 JSON 外层结构。" /></label
              ><VariableTextarea v-model="selectedTask.expected_output" :variables="taskVariableOptions" />
            </div>
            <section v-if="selectedTask.node_type === 'router'" class="router-rules-editor">
              <div class="router-section-title"><h3>条件分支</h3><small>按顺序检查，命中第一个条件后只执行对应分支。</small></div>
              <article v-for="(rule, index) in selectedTask.router_rules" :key="rule.id" class="router-case-editor">
                <header class="router-case-heading">
                  <div><strong>{{ index === 0 ? 'IF' : 'ELIF' }}</strong><small>CASE {{ index + 1 }}</small></div>
                  <button v-if="index > 0" class="icon-button" type="button" title="删除此分支" @click="removeRouterRule(selectedTask, index)"><Trash2 :size="13" /></button>
                </header>
                <ConditionGroupEditor
                  :model-value="rule.expression"
                  :variables="selectedConditionVariableOptions"
                  root
                  @update:model-value="updateRouterRule(selectedTask, index, $event)"
                />
              </article>
              <div v-if="!selectedTask.router_rules.length" class="field">
                <label>旧版路由表达式（兼容）</label>
                <input v-model="selectedTask.condition" placeholder="contains:approved" />
                <small>新建 IF 条件后，将改用可视化条件规则。</small>
              </div>
              <button class="router-add-case" type="button" @click="addRouterRule(selectedTask)"><Plus :size="14" />{{ selectedTask.router_rules.length ? 'ELIF' : '添加 IF' }}</button>
              <div class="router-else-note"><strong>ELSE</strong><span>以上条件都不满足时，执行 ELSE 连线指定的节点。</span></div>
            </section>
            <div v-if="decisionOptions.length" class="field">
              <label>运行条件（run_if）</label
              ><select v-model="selectedTask.run_if">
                <option value="">任意结果（any）</option>
                <option
                  v-for="outcome in decisionOptions"
                  :key="outcome"
                  :value="outcome"
                >
                  {{ outcome }}
                </option>
              </select>
            </div>
          </section>
          <section v-if="upstreamTasks(selectedTask).length" class="inspector-section variable-section">
            <h3>可用上游变量</h3>
            <div v-for="upstream in upstreamTasks(selectedTask)" :key="upstream.id" class="variable-group">
              <div class="variable-group-head">
                <span>{{ upstream.name }}（{{ upstream.id }}）</span>
              </div>
              <div class="fixed-output-list">
                <span v-for="field in upstream.output_variables || []" :key="field.name" class="upstream-variable">
                  <code>{{ field.name }}</code>
                  <span class="upstream-variable-type">{{ field.value_type }}</span>
                  <small>{{ field.description }}</small>
                </span>
              </div>
            </div>
          </section>
          <section
            v-if="
              workflow.kind === 'crew' &&
              workflow.process !== 'hierarchical' &&
              !['router','code','tool'].includes(selectedTask.node_type) &&
              selectedTask.node_type !== 'crew'
            "
            class="inspector-section"
          >
            <h3>Agent 分配（agent_id）</h3>
            <div class="field">
              <select v-model="selectedTask.agent_id">
                <option
                  v-if="
                    workflow.kind === 'crew' &&
                    workflow.process === 'hierarchical'
                  "
                  :value="null"
                >
                  由管理者分配（manager_assigns）
                </option>
                <option
                  v-for="agent in workflow.agents.filter(
                    (item) => item.id !== workflow.manager_agent_id,
                  )"
                  :key="agent.id"
                  :value="agent.id"
                >
                  {{ agent.role }}
                </option></select
              ><small>Flow 节点通过 Agent 节点连线指定；Crew 任务在这里选择执行 Agent。</small>
            </div>
          </section>
          <section
            v-if="selectedTask.node_type === 'crew'"
            class="inspector-section"
          >
            <h3>Crew 成员（crew_agent_ids）</h3>
            <template v-if="workflow.kind === 'flow'">
              <div v-if="selectedTask.crew_agent_ids.length" class="connected-agent-list">
                <span
                  v-for="agent in workflow.agents.filter((item) => selectedTask.crew_agent_ids.includes(item.id))"
                  :key="agent.id"
                  class="connected-agent-chip"
                ><UserRound :size="12" />{{ agent.role }}</span>
              </div>
              <p class="inspector-hint">Flow Crew 成员只由画布上的 Agent→Crew 连线决定；内部 Task 只能选择这些已连接成员。</p>
            </template>
            <template v-else>
              <label
                v-for="agent in workflow.agents"
                :key="agent.id"
                class="toggle-row"
                ><span><UserRound :size="12" />{{ agent.role }}</span
                ><input
                  :checked="selectedTask.crew_agent_ids.includes(agent.id)"
                  type="checkbox"
                  @click.prevent="toggleCrewMember(agent.id)"
              /></label>
            </template>
            <template v-if="workflow.kind === 'crew'">
              <div class="field">
                <label
                  ><ParamLabel
                    text="默认执行 Agent（agent_id）"
                    help="内部任务没有单独选择 Agent 时使用；留空则使用第一个 Crew 成员。" /></label
                ><select v-model="selectedTask.agent_id">
                  <option :value="null">第一个成员（first_member）</option>
                  <option
                    v-for="agent in workflow.agents.filter((item) =>
                      selectedTask.crew_agent_ids.includes(item.id),
                    )"
                    :key="agent.id"
                    :value="agent.id"
                  >
                    {{ agent.role }}
                  </option>
                </select>
              </div>
            </template>
          </section>
          <section
            v-if="selectedTask.node_type === 'crew'"
            class="inspector-section crew-task-editor"
          >
            <div class="section-title-action">
              <h3>内部任务（crew_tasks）</h3>
              <button
                class="icon-button"
                title="添加 Crew 内部任务"
                @click="addCrewTask(selectedTask)"
              >
                <Plus :size="12" />
              </button>
            </div>
            <p v-if="!selectedTask.crew_tasks?.length" class="inspector-empty">
              请添加至少一个明确的内部 Task。
            </p>
            <article
              v-for="(nested, index) in selectedTask.crew_tasks"
              :key="nested.id"
              class="crew-task-card"
            >
              <header>
                <span>{{ index + 1 }}</span
                ><input
                  v-model="nested.name"
                  aria-label="内部任务名称（name）"
                /><button
                  class="icon-button"
                  title="删除内部任务"
                  @click="removeCrewTask(selectedTask, index)"
                >
                  <Trash2 :size="12" />
                </button>
              </header>
              <div class="field">
                <label>描述（description）</label
                ><VariableTextarea v-model="nested.description" :variables="nestedVariableOptions(nested)" />
              </div>
              <div class="field">
                <label>期望输出（expected_output）</label
                ><VariableTextarea v-model="nested.expected_output" :variables="nestedVariableOptions(nested)" />
              </div>
              <div v-if="workflow.kind === 'flow' && selectedTask.crew_process !== 'hierarchical'" class="field">
                <label>执行 Agent（agent_id）</label
                ><select v-model="nested.agent_id">
                  <option v-if="workflow.kind === 'crew'" :value="null">默认执行 Agent（default）</option>
                  <option
                    v-for="agent in workflow.agents.filter((item) =>
                      selectedTask.crew_agent_ids.includes(item.id),
                    )"
                    :key="agent.id"
                    :value="agent.id"
                  >
                    {{ agent.role }}
                  </option>
                </select>
              </div>
              <div class="crew-task-deps">
                <div class="section-title-action">
                  <label
                    ><ParamLabel
                      text="依赖任务（depends_on）"
                      help="依赖会传入 CrewAI Task.context，并决定内部任务的执行顺序。" /></label
                  ><select
                    class="crew-dependency-select"
                    :value="dependencyDraft(selectedTask, nested)"
                    aria-label="选择上游内部任务"
                    @change="setCrewTaskDependency(selectedTask, nested, $event.target.value)"
                  >
                    <option value="">选择上游任务</option>
                    <option
                      v-for="dependency in availableCrewTaskDependencies(selectedTask, nested)"
                      :key="dependency.id"
                      :value="dependency.id"
                      :disabled="(nested.depends_on || []).includes(dependency.id)"
                    >
                      {{ dependency.name }}（{{ dependency.id }}）
                    </option>
                  </select><button
                    class="icon-button"
                    title="添加依赖任务"
                    @click="addCrewTaskDependency(selectedTask, nested)"
                  ><Plus :size="11" /></button>
                </div>
                <span v-for="dependency in nested.depends_on" :key="dependency"
                  >{{
                    selectedTask.crew_tasks.find(
                      (item) => item.id === dependency,
                    )?.name || dependency
                  }}<button
                    title="移除依赖"
                    @click="removeCrewTaskDependency(nested, dependency)"
                  >
                    <X :size="10" /></button
                ></span>
              </div>
              <div class="nested-output-variables">
                <label class="toggle-row"><span>结构化 JSON</span>
                  <input v-model="nested.output_mode" @change="nested.output_mode === 'json' && (nested.markdown = false); syncCrewOutputMode(selectedTask)" type="checkbox" class="toggle" true-value="json" false-value="text" />
                </label>
                <label>固定输出变量</label>
                <div
                  v-for="field in nested.output_variables || []"
                  :key="field.name"
                  class="fixed-output-row"
                >
                  <code>{{ field.name }}</code>
                  <span>{{ field.value_type }}</span>
                  <small>{{ field.description }}</small>
                </div>
              </div>
              <div class="crew-task-options">
                <label
                  ><input
                    v-model="nested.markdown"
                    :disabled="nested.output_mode === 'json'"
                    type="checkbox"
                  />Markdown（markdown）</label
                ><label
                  ><input
                    v-model="nested.async_execution"
                    type="checkbox"
                  />异步（async_execution）</label
                >
              </div>
            </article>
          </section>
          <section v-if="selectedTask.node_type !== 'router'" class="inspector-section variable-section">
            <div v-if="['task', 'agent'].includes(selectedTask.node_type)" class="output-mode-control">
              <label class="toggle-row"><span>结构化 JSON</span>
                <input v-model="selectedTask.output_mode" @change="selectedTask.output_mode === 'json' && (selectedTask.markdown = false)" type="checkbox" class="toggle" true-value="json" false-value="text" />
              </label>
              <small>开启后按期望输出生成结构化数据；关闭时直接返回正文。</small>
            </div>
            <h3>固定输出变量</h3>
            <div v-for="field in selectedTask.output_variables || []" :key="field.name" class="fixed-output-row">
              <code>{{ field.name }}</code>
              <span>{{ field.value_type }}</span>
              <small>{{ field.description }}</small>
            </div>
          </section>
          <section
            v-if="['task', 'agent', 'crew'].includes(selectedTask.node_type)"
            class="inspector-section"
          >
            <h3>
              {{
                workflow.kind === "flow"
                  ? "执行与人工审核"
                  : "任务执行设置"
              }}
            </h3>
            <label v-if="['task', 'agent'].includes(selectedTask.node_type)" class="toggle-row"
              ><span>Markdown 输出（markdown）</span
              ><input
                v-model="selectedTask.markdown"
                :disabled="selectedTask.output_mode === 'json'"
                type="checkbox"
                class="toggle" /></label
            ><label v-if="['task', 'agent'].includes(selectedTask.node_type)" class="toggle-row"
              ><span
                >异步执行（async_execution）<button
                  class="field-help"
                  type="button"
                  title="只适合互不依赖的 Crew Task；依赖链中的任务不应开启。"
                  aria-label="异步执行帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedTask.async_execution"
                type="checkbox"
                class="toggle" /></label
            ><label v-if="workflow.kind === 'flow' && ['agent', 'crew'].includes(selectedTask.node_type)" class="toggle-row"
              ><span
                >人工审批门（human_feedback）<button
                  class="field-help"
                  type="button"
                  title="Flow 在此暂停，并在预览或运行聊天页弹出审批对话框；提交结果后继续。"
                  aria-label="人工审批帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="selectedTask.human_feedback"
                type="checkbox"
                class="toggle" /></label
            ><template
              v-if="workflow.kind === 'flow' && ['agent', 'crew'].includes(selectedTask.node_type) && selectedTask.human_feedback"
              ><div class="field">
                <label>审核消息（feedback_message）</label
                ><input v-model="selectedTask.feedback_message" />
              </div>
              <div class="field">
                <label>审批结果（feedback_outcomes）</label
                ><input
                  :value="selectedTask.feedback_outcomes.join(', ')"
                  @change="
                    selectedTask.feedback_outcomes = $event.target.value
                      .split(',')
                      .map((item) => item.trim())
                      .filter(Boolean)
                  "
                />
              </div>
              <div class="field">
                <label>默认结果（feedback_default_outcome）</label
                ><select v-model="selectedTask.feedback_default_outcome">
                  <option :value="null">必须人工选择（required）</option>
                  <option
                    v-for="outcome in selectedTask.feedback_outcomes"
                    :key="outcome"
                    :value="outcome"
                  >
                    {{ outcome }}
                  </option>
                </select>
              </div></template
            >
          </section>
          <button class="button danger" @click="removeTask">
            <Trash2 :size="14" />删除节点
          </button>
        </div>
        <div v-else-if="selectedEdge" class="studio-panel-scroll">
          <section class="inspector-section connection-inspector">
            <h3>
              {{
                selectedEdge.edgeType === "dependency"
                  ? "变量依赖（dependency）"
                  : selectedEdge.edgeType === "route"
                    ? `条件分支（${routerBranchLabel(workflow.tasks.find(item => item.id === selectedEdge.source), selectedEdge.branch)}）`
                    : selectedEdge.edgeType === "router-condition"
                      ? "条件变量依赖"
                    : "Agent 关系（assignment / member）"
              }}
            </h3>
            <div class="connection-endpoint">
              <span>{{ taskName(selectedEdge.source) }}</span
              ><GitBranch :size="14" /><span>{{
                taskName(selectedEdge.target)
              }}</span>
            </div>
            <p>
              {{
                selectedEdge.edgeType === "dependency"
                  ? "该连线建立执行顺序；目标节点可以引用这条上游路径中所有节点的固定输出变量。"
              : selectedEdge.edgeType === "route"
                ? "该节点只会在条件路由命中此分支时执行；未命中的分支及其独占下游会被跳过。"
                : selectedEdge.edgeType === "router-condition"
                  ? selectedEdge.label
                : "上、下端口对应 Agent 分配或 Crew 成员关系。"
              }}
            </p>
          </section>
          <button v-if="selectedEdge.edgeType !== 'router-condition'" class="button danger" @click="removeEdge">
            <Trash2 :size="14" />删除关系
          </button>
        </div>
        <div v-else class="studio-panel-scroll">
          <section class="inspector-section workflow-inputs">
            <div class="section-title-action">
              <h3>运行输入（inputs）</h3>
              <button
                class="icon-button"
                title="添加运行输入"
                @click="addWorkflowInput"
              >
                <Plus :size="12" />
              </button>
            </div>
            <p v-if="!workflow.inputs.length" class="inspector-empty">
              此自动化运行时不需要外部输入。
            </p>
            <div
              v-for="(input, index) in workflow.inputs"
              :key="index"
              class="workflow-input-row"
            >
              <input
                v-model="input.label"
                aria-label="显示名称（label）"
              /><input
                :value="input.name"
                aria-label="变量名（name）"
                :disabled="input.name === 'message'"
                @change="renameWorkflowInput(input, $event)"
              /><select :value="input.input_type" @change="changeWorkflowInputType(input, $event.target.value)">
                <option value="text">短文本（text）</option>
                <option value="long_text">长文本（long_text）</option>
                <option value="file">文件（file）</option>
                <option value="image">图片（image）</option>
                <option value="number">数字（number）</option>
                <option value="boolean">开关（boolean）</option>
                <option value="json">JSON（json）</option></select
              ><label
                ><input
                  v-model="input.required"
                  type="checkbox"
                  :disabled="input.name === 'message'"
                />必填（required）</label
              ><button
                v-if="input.name !== 'message'"
                class="icon-button"
                title="删除运行输入"
                @click="removeWorkflowInput(input, index)"
              >
                <Trash2 :size="12" />
              </button>
            </div>
          </section>
          <section class="inspector-section interaction-settings">
            <h3><MessageCircle :size="13" />用户交互方式（interaction_mode）</h3>
            <div class="field">
              <label>一次运行如何收集信息</label>
              <select
                :value="workflow.interaction_mode"
                @change="changeInteractionMode($event.target.value)"
              >
                <option value="single_run">一次性输入全部信息</option>
                <option value="multi_turn">分步骤对话补充信息</option>
              </select>
              <small class="field-help-text">
              </small>
            </div>
            <p v-if="workflow.interaction_mode === 'multi_turn'" class="field-help-text">
              可为多个普通 Agent 或 Flow Crew 中的 Agent 开启 ask_user。执行到相应节点时暂停，用户回复后从该节点的检查点恢复；无需指定单一信息收集节点。
            </p>
          </section>
          <section
            v-if="workflow.kind === 'crew'"
            class="inspector-section crew-settings"
          >
            <CrewSettingsEditor
              :model-value="workflow"
              :agents="workflow.agents"
              :models="store.chatModels"
              title="Crew 设置"
              @process-change="changeProcess($event)"
              @manager-change="setManager($event)"
            />
          </section>
          <section v-else class="inspector-section crew-settings">
            <h3><Settings2 :size="13" />Flow 设置（Flow）</h3>
            <div class="field">
              <label
                ><ParamLabel
                  text="最大方法调用次数（max_method_calls）"
                  help="限制一次 Flow 运行中的方法调用总数，避免意外循环。" /></label
              ><input
                v-model.number="workflow.max_method_calls"
                type="number"
                min="1"
                max="10000"
              />
            </div>
          </section>
          <div class="canvas-help">
            <Plus :size="13" />
            <p>添加节点请点击画布左上角的 ＋；左右端口传递任务上下文，Flow 中 Agent 只能通过下方到任务顶部的连线指定。</p>
          </div>
        </div>
      </aside>
    </div>

    <div v-if="showRun" class="preview-scrim" @click="closePreview"></div>
    <aside v-if="showRun" class="studio-preview">
      <header class="studio-preview-head">
        <div>
          <span class="eyebrow">PREVIEW</span
          ><strong>{{ workflow.name }}</strong
          ><small>{{ workflow.kind.toUpperCase() }} · 草稿测试</small>
        </div>
        <button class="icon-button" title="关闭预览" @click="closePreview">
          <X :size="15" />
        </button>
      </header>
      <div ref="previewThread" class="studio-preview-thread" @scroll="onPreviewScroll">
        <article
          v-for="(message, index) in previewMessages"
          :key="index"
          class="preview-message"
          :class="message.role"
        >
          <span v-if="message.role !== 'user'" class="chat-avatar"
            ><Bot :size="15"
          /></span>
          <div>
            <div v-if="message.attachments?.length" class="chat-message-files">
              <span v-for="file in message.attachments" :key="file.id"
                ><FileText :size="11" />{{ file.name }}</span
              >
            </div>
            <div v-if="message.turns?.length" class="agent-turn-list preview-turn-list">
                <article v-for="turn in message.turns" :key="turn.id" class="agent-turn" :class="[turn.status, { collapsed: !turn.expanded }]">
                <button class="agent-turn-head" type="button" @click="turn.expanded = !turn.expanded">
                  <span class="agent-turn-dot" :class="turn.status"></span>
                  <span class="agent-turn-title"><strong>{{ turn.agent_role }}</strong><small>{{ turn.step_name }}</small></span>
                  <span class="agent-turn-state">{{ turn.status === 'running' ? '运行中' : turn.status === 'failed' ? '失败' : turn.status === 'waiting_input' ? '等待输入' : '已完成' }}</span>
                  <ChevronDown :size="13" />
                </button>
                <div v-if="turn.expanded" class="agent-turn-body">
                  <div v-if="turn.activity" class="agent-turn-activity"><span class="activity-pulse-dot"></span>{{ turn.activity }}</div>
                  <div v-if="turn.tools?.length" class="agent-turn-tools"><div v-for="tool in turn.tools" :key="tool.id" class="agent-turn-tool" :class="tool.status"><LoaderCircle v-if="tool.status === 'running'" class="spin" :size="11" /><Check v-else-if="tool.status === 'completed'" :size="11" /><X v-else :size="11" /><span><strong>{{ tool.name }}</strong><small>{{ tool.detail }}</small></span></div></div>
                  <RichMessage v-if="turn.output && turn.status !== 'running'" class="agent-turn-output agent-turn-rich-output" :text="turn.output" />
                  <pre v-else-if="turn.output" class="agent-turn-output">{{ turn.output }}</pre>
                  <span v-if="turn.status === 'running'" class="stream-caret"></span>
                </div>
              </article>
            </div>
            <RunApprovalCard
              v-for="approval in message.approvals || []"
              :key="`${approval.runId}-${approval.step_id}-${approval.status}`"
              :approval="approval"
              :busy="approval.busy"
              @submit="submitPreviewFeedback(message, approval, $event)"
            />
            <div v-if="message.runtimeNotice" class="run-runtime-notice">
              <span>{{ message.runtimeNotice }}</span>
              <small v-if="message.retryHistory?.length">已自动恢复 {{ message.retryHistory.length }} 次</small>
            </div>
            <div v-if="message.activity && !message.turns?.length" class="run-current-activity">
              <span class="activity-pulse-dot"></span>{{ message.activity }}
            </div>
            <div
              v-if="message.streaming && !message.text"
              class="chat-thinking"
            >
              <span></span><span></span><span></span>
            </div>
            <RichMessage v-else class="chat-copy" :text="message.text" :files="message.files || []" />
            <div v-if="message.files?.length" class="chat-delivery-files">
              <a
                v-for="file in message.files"
                :key="file.object_key || file.name"
                :href="file.url"
                :title="file.minio_path || file.name"
                @click.prevent="downloadPreviewArtifact(file)"
              ><Download :size="14" />{{ file.name }}</a>
            </div>
            <div v-if="message.error" class="chat-run-error">
              {{ message.error }}
              <button
                v-if="message.jobId && !message.streaming"
                class="button ghost chat-retry chat-error-retry"
                type="button"
                :disabled="busy || message.retrying"
                @click="retryStudioMessage(message)"
              >
                <LoaderCircle v-if="message.retrying" class="spin" :size="13" />
                <RotateCcw v-else :size="13" />
                {{ message.retrying ? "正在重试…" : "重试此轮" }}
              </button>
            </div>
            <button
              v-if="isLatestRunMessage(previewMessages, message) && !message.streaming && ['completed', 'failed', 'waiting_input'].includes(message.status)"
              class="button ghost chat-retry"
              type="button"
              :disabled="previewBusy || message.retrying"
              @click="retryPreviewMessage(message)"
            >
              <LoaderCircle v-if="message.retrying" class="spin" :size="13" />
              <RotateCcw v-else :size="13" />
              {{ message.retrying ? "正在重试…" : "重试此轮" }}
            </button>
            <button
              v-if="message.jobId && !message.streaming && (message.status === 'failed' || message.error)"
              class="button ghost chat-retry"
              type="button"
              :disabled="busy || message.retrying"
              @click="retryStudioMessage(message)"
            >
              <LoaderCircle v-if="message.retrying" class="spin" :size="13" />
              <RotateCcw v-else :size="13" />
              {{ message.retrying ? "正在重试…" : "重试此轮" }}
            </button>
            <span
              v-if="message.streaming && message.text"
              class="stream-caret"
            ></span>
            <div
              v-if="message.runId && !message.streaming"
              class="chat-message-meta"
            >
              <span>{{
                message.status === "completed" ? "已完成" : message.status
              }}</span
              ><button @click="router.push(`/runs/${message.runId}`)">
                查看 Trace <ArrowUpRight :size="11" />
              </button>
            </div>
          </div>
        </article>
      </div>
      <div class="studio-preview-compose">
        <div v-if="previewVariableInputs.length" class="preview-inline-inputs">
          <div
            v-for="input in previewVariableInputs"
            :key="input.name"
            class="preview-inline-field"
          >
            <label>{{ input.label }}<em v-if="input.required">必填</em></label
            ><textarea
              v-if="
                input.input_type === 'long_text' || input.input_type === 'json'
              "
              v-model="previewValues[input.name]"
              :placeholder="input.description || input.name"
            ></textarea
            ><input
              v-else-if="input.input_type === 'number'"
              v-model="previewValues[input.name]"
              type="number"
            /><label
              v-else-if="input.input_type === 'boolean'"
              class="chat-variable-switch"
              ><input
                v-model="previewValues[input.name]"
                type="checkbox"
              />启用</label
            ><input
              v-else
              v-model="previewValues[input.name]"
              :placeholder="input.description || input.name"
            />
          </div>
        </div>
        <div class="preview-composer-box">
          <div v-if="previewUploading" class="upload-status">
            <div class="upload-status-head">
              <span>正在上传 {{ previewPendingUploadNames.join("、") }}</span
              ><b>{{ previewUploadProgress }}%</b>
            </div>
            <div class="upload-progress-track">
              <i :style="{ width: `${previewUploadProgress}%` }"></i>
            </div>
          </div>
          <div v-if="previewSelectedFiles.length" class="preview-files">
            <template v-for="input in previewFileInputs" :key="input.name"
              ><span
                v-for="(file, index) in previewFiles[input.name]"
                :key="file.id"
                ><FileText :size="11" /><b>{{ file.name }}</b
                ><small>{{ input.label }}</small
                ><button @click="removePreviewFile(input.name, index)">
                  <X :size="11" /></button></span
            ></template>
          </div>
          <textarea
            v-model="previewMessage"
            :disabled="previewBusy || previewUploading"
            :placeholder="previewPrimaryInput?.label || '输入消息或运行要求'"
            @keydown.enter.exact.prevent="sendPreview"
          ></textarea>
          <div class="preview-composer-actions">
            <template v-if="previewFileInputs.length"
              ><input
                ref="previewFileInput"
                hidden
                type="file"
                multiple
                @change="choosePreviewFiles"
              /><button
                class="icon-button"
                :disabled="previewBusy || previewUploading"
                title="添加文件或图片"
                @click="previewFileInput?.click()"
              >
                <LoaderCircle
                  v-if="previewUploading"
                  class="spin"
                  :size="15"
                /><Paperclip v-else :size="15" /></button
              ><span>{{
                previewUploading
                  ? "上传完成后即可发送"
                  : "文件会自动绑定到合适的输入字段"
              }}</span></template
            ><span v-else></span
            ><button
              class="chat-send"
              :disabled="!previewCanSend"
              title="发送"
              @click="sendPreview"
            >
              <LoaderCircle v-if="previewBusy" class="spin" :size="15" /><Send
                v-else
                :size="15"
              />
            </button>
          </div>
        </div>
      </div>
    </aside>

    <Teleport to="body">
      <div
        v-if="proposalCapabilityPickerOpen && proposalCapabilityTarget"
        class="modal-backdrop capability-picker-backdrop"
        @click.self="closeProposalCapabilityPicker"
      >
        <section
          class="modal capability-picker-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="proposal-capability-picker-title"
        >
          <header class="modal-header">
            <div>
              <span class="eyebrow">能力配置</span>
              <h2 id="proposal-capability-picker-title">
                选择{{ proposalCapabilityTypeLabel(proposalCapabilityTarget.requirement) }}
              </h2>
            </div>
            <button
              class="icon-button"
              title="关闭"
              @click="closeProposalCapabilityPicker"
            >
              <X :size="16" />
            </button>
          </header>
          <div class="modal-body capability-picker-body">
            <p>
              为“{{ proposalCapabilityTarget.requirement.label }}”选择一个或多个实际匹配的资源。未选中的资源不会被自动加入。
            </p>
            <section>
              <header>
                <Library
                  v-if="proposalCapabilityTarget.requirement.resource_type === 'skill'"
                  :size="14"
                />
                <BookOpen
                  v-else-if="proposalCapabilityTarget.requirement.resource_type === 'knowledge'"
                  :size="14"
                />
                <Wrench v-else :size="14" />
                <strong>可选{{ proposalCapabilityTypeLabel(proposalCapabilityTarget.requirement) }}</strong>
                <span>{{ proposalCapabilityOptions.length }}</span>
              </header>
              <button
                v-for="resource in proposalCapabilityOptions"
                :key="resource.id"
                class="capability-picker-item"
                :class="{ selected: proposalCapabilitySelected(resource.id) }"
                @click="toggleProposalCapability(resource.id)"
              >
                <span>
                  <Library
                    v-if="proposalCapabilityTarget.requirement.resource_type === 'skill'"
                    :size="14"
                  />
                  <BookOpen
                    v-else-if="proposalCapabilityTarget.requirement.resource_type === 'knowledge'"
                    :size="14"
                  />
                  <Wrench v-else :size="14" />
                </span>
                <div>
                  <strong>{{ resource.name }}</strong>
                  <small>{{ resource.description || "暂无说明" }}</small>
                </div>
                <Check
                  v-if="proposalCapabilitySelected(resource.id)"
                  :size="14"
                />
                <Plus v-else :size="14" />
              </button>
              <p
                v-if="!proposalCapabilityOptions.length"
                class="capability-picker-empty"
              >
                当前工作空间没有可选资源。
                <button
                  class="text-button"
                  @click="openCapabilityManager(proposalCapabilityTarget.requirement)"
                >
                  前往创建
                </button>
              </p>
              <button
                v-if="proposalCapabilityOptions.length"
                class="button capability-create-resource"
                @click="openCapabilityManager(proposalCapabilityTarget.requirement)"
              >
                <Plus :size="13" />新建{{ proposalCapabilityTypeLabel(proposalCapabilityTarget.requirement) }}
              </button>
            </section>
          </div>
          <footer class="modal-footer">
            <button class="button" @click="closeProposalCapabilityPicker">
              取消
            </button>
            <button
              class="button primary"
              :disabled="
                proposalCapabilityTarget.requirement.required !== false &&
                !proposalCapabilitySelection.length
              "
              @click="confirmProposalCapabilityPicker"
            >
              <Check :size="13" />添加所选资源
            </button>
          </footer>
        </section>
      </div>
      <div
        v-if="capabilityPickerOpen && selectedAgent"
        class="modal-backdrop capability-picker-backdrop"
        @click.self="capabilityPickerOpen = false"
      >
        <section
          class="modal capability-picker-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="capability-picker-title"
        >
          <header class="modal-header">
            <div>
              <span class="eyebrow">AGENT CAPABILITIES</span>
              <h2 id="capability-picker-title">导入知识库、Skill 与工具</h2>
            </div>
            <button
              class="icon-button"
              title="关闭"
              @click="capabilityPickerOpen = false"
            >
              <X :size="16" />
            </button>
          </header>
          <div class="modal-body capability-picker-body">
            <p>
              选择要提供给“{{ selectedAgent.role }}”的知识来源、Skill
              或工具。只显示尚未导入的资源。
            </p>
            <section>
              <header>
                <BookOpen :size="14" /><strong>知识库</strong
                ><span>{{ availableKnowledge.length }}</span>
              </header>
              <button
                v-for="knowledge in availableKnowledge"
                :key="knowledge.id"
                class="capability-picker-item"
                @click="importCapability('knowledge_base_ids', knowledge.id)"
              >
                <span><BookOpen :size="14" /></span>
                <div>
                  <strong>{{ knowledge.name }}</strong
                  ><small>{{
                    knowledge.description || "工作空间知识库"
                  }}</small>
                </div>
                <Plus :size="14" />
              </button>
              <p
                v-if="!availableKnowledge.length"
                class="capability-picker-empty"
              >
                没有可导入的知识库。<button
                  class="text-button"
                  @click="router.push('/knowledge')"
                >
                  创建知识库
                </button>
              </p>
            </section>
            <section>
              <header>
                <Library :size="14" /><strong>Skills</strong
                ><span>{{ availableSkills.length }}</span>
              </header>
              <button
                v-for="skill in availableSkills"
                :key="skill.id"
                class="capability-picker-item"
                @click="importCapability('skills', skill.id)"
              >
                <span><Library :size="14" /></span>
                <div>
                  <strong>{{ skill.name }}</strong
                  ><small>{{ skill.description || "CrewAI Skill" }}</small>
                </div>
                <Plus :size="14" />
              </button>
              <p v-if="!availableSkills.length" class="capability-picker-empty">
                没有可导入的 Skill。
              </p>
            </section>
            <section>
              <header>
                <Wrench :size="14" /><strong>Tools</strong
                ><span>{{ availablePlugins.length }}</span>
              </header>
              <button
                v-for="plugin in availablePlugins"
                :key="plugin.id"
                class="capability-picker-item"
                @click="importCapability('plugins', plugin.id)"
              >
                <span><Wrench :size="14" /></span>
                <div>
                  <strong>{{ plugin.name }}</strong
                  ><small>{{ plugin.description || "Agent 工具" }}</small>
                </div>
                <Plus :size="14" />
              </button>
              <p
                v-if="!availablePlugins.length"
                class="capability-picker-empty"
              >
                没有可导入的工具。
              </p>
            </section>
          </div>
          <footer class="modal-footer">
            <button
              class="button primary"
              @click="capabilityPickerOpen = false"
            >
              <Check :size="13" />完成
            </button>
          </footer>
        </section>
      </div>

    </Teleport>
    <AutomationDetailsDialog
      :open="automationDetailsOpen"
      :mode="automationDetailsMode"
      :kind="automationDetailsKind"
      :name="automationDetailsMode === 'edit' ? workflow.name : ''"
      :description="automationDetailsMode === 'edit' ? workflow.description : ''"
      @cancel="cancelAutomationDetails"
      @confirm="confirmAutomationDetails"
    />
  </div>
</template>
