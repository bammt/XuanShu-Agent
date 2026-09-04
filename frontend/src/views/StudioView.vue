<script setup>
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
  MousePointer2,
  Pencil,
  Play,
  Plus,
  Route,
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
import { applyRunFrame } from "../services/runStream";
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
const prompt = ref("");
const selectedTaskId = ref("");
const selectedAgentId = ref("");
const selectedEdgeId = ref("");
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
let draftTimer = null;
let previewPollTimer = null;
let previewRunAbortController = null;
let persistedWorkflowSnapshot = null;
let pendingManualChanges = [];
const syncedManualChanges = ref([]);

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
    title: "Crew kickoff",
    description: "在 Flow 中启动包含明确 Task 的多 Agent Crew",
  },
  router: {
    label: "路由",
    title: "Router",
    description: "根据上游输出产生确定性分支标签",
  },
  code: {
    label: "代码",
    title: "Code step",
    description: "从自定义 CrewAI Python 方法解析的流程步骤",
  },
};

const creationKind = () => (route.params.kind === "flow" ? "flow" : "crew");
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
  output_log_file: "",
  manager_agent_id: null,
  manager_model_profile_id: null,
  max_rpm: null,
  max_method_calls: 100,
  model: store.defaultModel?.model || "",
  model_profile_id: store.defaultModel?.id || null,
  status: "draft",
  agents: [],
  tasks: [],
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
const selectedAgent = computed(() =>
  workflow.value.agents.find((item) => item.id === selectedAgentId.value),
);
const activeProposalIndex = computed(() => {
  if (confirmation.value?.clarification) return -1;
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
    if (dependency?.node_type === "router")
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
      type: typeInfo[task.node_type],
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
  const dependencies = workflow.value.tasks.flatMap((task) =>
    task.depends_on.map((source) => ({ source, target: task.id })),
  );
  return dependencies.map((edge) => {
    const task = workflow.value.tasks.find((item) => item.id === edge.target);
    return {
      id: `dep:${edge.source}:${edge.target}`,
      source: edge.source,
      target: edge.target,
      sourceHandle: "context-out",
      targetHandle: "context-in",
      label: task ? mappingLabel(task, edge.source) : "",
      edgeType: "dependency",
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
    name: item.name,
    description: item.description,
    kind: item.kind,
    status: item.status,
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
    .sort((a, b) => timestampValue(b.updated_at) - timestampValue(a.updated_at))
    .slice(0, 8);
});
const defaultOutputVariable = () => ({
  name: "result",
  description: "Complete task output",
  value_type: "string",
});
const defaultCrewTask = (task, index = 0) => ({
  id: Math.random().toString(36).slice(2, 10),
  name: `内部任务 ${index + 1}`,
  description: "完成 Crew 中的一项明确工作",
  expected_output: "可验证的任务结果",
  agent_id: task?.crew_agent_ids?.[0] || workflow.value.agents[0]?.id || null,
  depends_on: [],
  output_variables: [defaultOutputVariable()],
  dependency_variables: {},
  async_execution: false,
  markdown: false,
  output_file: "",
  create_directory: true,
  guardrail: "",
  guardrail_max_retries: 3,
});
function normalizeMappings(value) {
  if (!value || typeof value !== "object") return {};
  return Object.fromEntries(
    Object.entries(value).map(([dependency, rawMappings]) => {
      let mappings = rawMappings;
      if (typeof mappings === "string")
        mappings = [{ source_variable: "result", target_variable: mappings }];
      else if (!Array.isArray(mappings)) mappings = [mappings];
      return [
        dependency,
        mappings.filter(Boolean).map((item) => ({
          source_variable: item.source_variable || "result",
          target_variable: item.target_variable || "context",
        })),
      ];
    }),
  );
}
function outputOptions(dependencyId) {
  return (
    workflow.value.tasks.find((item) => item.id === dependencyId)
      ?.output_variables || [defaultOutputVariable()]
  );
}
function mappingLabel(task, dependencyId) {
  const mappings = task.dependency_variables?.[dependencyId] || [];
  if (!mappings.length) return "完整上下文";
  const first = mappings[0];
  return mappings.length === 1
    ? `${first.source_variable} → ${first.target_variable}`
    : `${mappings.length} 个变量`;
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
  if (item.remote && !item.application_id)
    router.push({ path: "/new-automation", query: { session: item.id } });
  else if (item.application_id) router.push(`/studio/${item.application_id}`);
  else router.push(`/studio/${item.id}`);
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
  const fallback = item.agents[0]?.id || null;
  item.original_request ??= item.request || "";
  item.stage_summaries ??= {};
  item.planning ??= false;
  item.planning_model_profile_id ??= null;
  item.memory ??= false;
  item.cache ??= true;
  item.output_log_file ??= "";
  item.manager_agent_id ??= null;
  item.manager_model_profile_id ??= null;
  item.max_rpm ??= null;
  item.max_method_calls ??= 100;
  item.interaction_mode =
    item.interaction_mode === "multi_turn" ? "multi_turn" : "single_run";
  item.interaction ??= {};
  item.chat_history ??= [];
  item.inputs ??= [];
  item.structure_confirmed ??= true;
  item.draft_revision ??= null;
  item.agents = item.agents.map((agent, index) => ({
    max_rpm: null,
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
    agent_id:
      item.kind === "crew" && item.process !== "hierarchical"
        ? task.agent_id || fallback
        : task.agent_id,
    crew_agent_ids:
      task.node_type === "crew" && !(task.crew_agent_ids || []).length
        ? item.agents.map((agent) => agent.id)
        : task.crew_agent_ids || [],
    output_variables: task.output_variables?.length
      ? task.output_variables
      : [defaultOutputVariable()],
    dependency_variables: normalizeMappings(task.dependency_variables),
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
    guardrail: task.guardrail || "",
    guardrail_max_retries: task.guardrail_max_retries ?? 3,
    crew_tasks: (task.crew_tasks || []).map((nested, index) => ({
      ...defaultCrewTask(task, index),
      ...nested,
      agent_id: nested.agent_id || task.crew_agent_ids?.[0] || fallback,
      depends_on: Array.isArray(nested.depends_on) ? nested.depends_on : [],
      output_variables: nested.output_variables?.length
        ? nested.output_variables
        : [defaultOutputVariable()],
      dependency_variables: normalizeMappings(nested.dependency_variables),
      create_directory: nested.create_directory ?? true,
      guardrail_max_retries: nested.guardrail_max_retries ?? 3,
    })),
  }));
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
  item.agents.forEach((agent) => { delete agent.code_execution_mode; });
  return item;
}
function hydrateWorkflow(loaded, proposal = null) {
  hydrating = true;
  workflow.value = migrate(loaded);
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
  const active = session.active_job || {};
  const pendingJob = ["queued", "planning"].includes(active.status) && active.job_id;
  if (pendingJob) {
    const answer = [...messages.value]
      .reverse()
      .find((item) => item.role === "assistant" && item.jobId === active.job_id);
    if (answer) answer.pending = true;
  }
  if (
    pendingJob
  )
    await resumeStudioJob(active.job_id, session.id);
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
      "event_driven",
    ].includes(value.recommended_process)
      ? value.recommended_process
      : value.recommended_kind === "flow"
        ? "event_driven"
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
  await store.load();
  try {
    remoteSessions.value = await api.studioSessions();
  } catch (_) {
    remoteSessions.value = [];
  }
  await loadWorkflow();
  if (route.name === "studio-new" && route.params.kind) {
    openCreateDetails(route.params.kind);
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
  () => {
    // Saving a new workflow only changes its URL. Reloading the same object here
    // would discard transient UI state such as confirmations and preview drawers.
    if (
      route.params.id &&
      String(route.params.id) === String(workflow.value.id)
    )
      return;
    if (busy.value && String(route.query.session || "") === String(workflow.value.id))
      return;
    loadWorkflow();
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
  }
}
function changeInteractionMode(mode) {
  if (mode === "multi_turn") workflow.value.interaction_mode = "multi_turn";
  else workflow.value.interaction_mode = "single_run";
  if (workflow.value.interaction_mode === "multi_turn") {
    workflow.value.inputs = workflow.value.inputs.filter(
      (input) =>
        input.name === "message" || ["file", "image"].includes(input.input_type),
    );
  }
  if (workflow.value.interaction_mode === "single_run") {
    workflow.value.agents.forEach((agent) => {
      agent.user_interaction = false;
    });
  } else if (workflow.value.kind === "crew" && workflow.value.process === "hierarchical") {
    const managerId = workflow.value.manager_agent_id || workflow.value.agents[0]?.id;
    setManager(managerId);
  } else {
    const first = workflow.value.tasks[0];
    const collectorId = first?.node_type === "crew" ? "" : first?.agent_id;
    workflow.value.agents.forEach((agent) => {
      agent.user_interaction = agent.id === collectorId;
    });
  }
}
watch(
  nodes,
  (value) => {
    graphNodes.value = value;
  },
  { immediate: true, deep: true },
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
  { immediate: true, deep: true },
);
watch(workflow, () => scheduleDraft(), { deep: true });
watch(
  messages,
  (value) => {
    if (hydrating) return;
    workflow.value.chat_history = value
      .filter(
        (item) =>
          ["user", "assistant"].includes(item.role) &&
          item.text,
      )
      .slice(-40)
      .map((item) => ({
        role: item.role,
        content: item.text,
        job_id: item.jobId || "",
        attachments: item.attachments || [],
        proposal: item.proposal || null,
        clarification: item.clarification || null,
        clarificationAnswer: item.clarificationAnswer || "",
      }));
    scheduleDraft();
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

function scrollChat() {
  nextTick(() => {
    if (assistantThread.value)
      assistantThread.value.scrollTop = assistantThread.value.scrollHeight;
  });
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
  draftDirty = false;
  saveState.value = "Saving draft...";
  const manualChanges = currentManualChanges();
  try {
    const result = await api.saveWorkflow(workflow.value, manualChanges);
    hydrating = true;
    workflow.value.id = result.id;
    workflow.value.updated_at = result.updated_at;
    workflow.value.draft_revision = result.draft_revision;
    workflow.value.draft_sync = result.draft_sync || workflow.value.draft_sync;
    markWorkflowPersisted(workflow.value);
    await nextTick();
    hydrating = false;
    saveState.value = "Draft saved";
    await store.load();
    if (!route.params.id || route.params.id !== result.id)
      router.replace(`/studio/${result.id}`);
  } catch (error) {
    draftDirty = true;
    saveState.value = "Draft pending";
    store.error = error.message;
  }
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
  if (finalResult.phase === "failed")
    throw new Error(finalResult.error || "请求未完成");
  if (answer) {
    answer.pending = false;
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
  workflow.value = migrate(value);
  workflow.value.chat_history = historySnapshot;
  workflow.value.structure_confirmed = true;
  // Generated results become the new baseline. Only edits made afterwards
  // are reported as manual canvas changes.
  markWorkflowPersisted(workflow.value);
  confirmation.value = null;
  selectedTaskId.value = value.tasks[0]?.id || "";
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  fitCanvas(220);
  draftDirty = false;
  saveState.value = "Draft saved";
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
    if (draftDirty) return;
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
    answer.text ||= `没有完成：${error.message}`;
    answer.role = "error";
    store.error = error.message;
  } finally {
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
      ? "event_driven"
      : ["sequential", "hierarchical"].includes(proposal.recommended_process)
        ? proposal.recommended_process
        : "sequential";
  proposal.process_reason =
    kind === "flow"
      ? "确认后将按事件驱动、状态与分支关系重新生成 Flow。"
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
    answer.streaming = false;
    answer.text ||= `没有完成：${error.message}`;
    answer.role = "error";
    store.error = error.message;
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
    answer.streaming = false;
    answer.text ||= `没有完成：${error.message}`;
    answer.role = "error";
    store.error = error.message;
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
    hydrating = true;
    const saved = migrate(await api.saveWorkflow(workflow.value, currentManualChanges()));
    workflow.value = saved;
    markWorkflowPersisted(workflow.value);
    await nextTick();
    hydrating = false;
    draftDirty = false;
    await store.load();
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
  if (draftTimer) {
    window.clearTimeout(draftTimer);
    draftTimer = null;
  }
  saveState.value = "Saving draft...";
  const saved = migrate(await api.saveWorkflow(workflow.value, currentManualChanges()));
  if (!/^\d+$/.test(String(saved.id || "")))
    throw new Error("应用保存后未返回有效的整数 ID");
  saved.id = Number(saved.id);
  hydrating = true;
  workflow.value = saved;
  markWorkflowPersisted(workflow.value);
  await nextTick();
  hydrating = false;
  draftDirty = false;
  saveState.value = "Draft saved";
  await store.load();
  if (String(route.params.id || "") !== String(saved.id))
    await router.replace(`/studio/${saved.id}`);
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
function scrollPreview() {
  nextTick(() => {
    if (previewThread.value)
      previewThread.value.scrollTop = previewThread.value.scrollHeight;
  });
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
  answer.status = "waiting_for_feedback";
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
      await store.load();
      scrollPreview();
      return;
    }
    if (record.status === "failed") {
      answer.role = record.output ? "assistant" : "error";
      answer.text = stripLocalArtifactReferences(record.output);
      answer.error = record.error || "执行失败";
      answer.streaming = false;
      previewBusy.value = false;
      await store.load();
      scrollPreview();
      return;
    }
    if (record.status === "waiting_for_feedback") {
      openPreviewFeedback(record.id, record.pending_feedback, answer);
      await store.load();
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
    await api.runEvents(
      id,
      (frame) => {
        terminal = applyRunFrame(answer, frame) || terminal;
        if (frame.type === "waiting_for_feedback")
          openPreviewFeedback(id, frame.pending_feedback || {}, answer);
        scrollPreview();
      },
      controller.signal,
      answer.eventCursor || 0,
    );
    if (!terminal) return pollPreview(id, answer);
    previewBusy.value = false;
    await store.load();
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
  } else {
    setManager(workflow.value.manager_agent_id || workflow.value.agents[0]?.id || addAgent());
  }
}
function setManager(agentId) {
  workflow.value.manager_agent_id = agentId || null;
  if (agentId) {
    workflow.value.tasks.forEach((task) => {
      if (task.agent_id === agentId) task.agent_id = null;
    });
    workflow.value.agents.forEach((agent) => {
      agent.allow_delegation = agent.id === agentId;
      if (workflow.value.interaction_mode === "multi_turn")
        agent.user_interaction = agent.id === agentId;
    });
  }
}
function changeEmbeddedCrewProcess(task, process) {
  task.crew_process = process;
  if (process !== "hierarchical" || !task.crew_agent_ids.length) return;
  const managerId = task.crew_agent_ids[0];
  workflow.value.agents.forEach((agent) => {
    if (!task.crew_agent_ids.includes(agent.id)) return;
    agent.allow_delegation = agent.id === managerId;
    if (workflow.value.interaction_mode === "multi_turn")
      agent.user_interaction = agent.id === managerId;
  });
  task.crew_tasks.forEach((nested) => {
    nested.agent_id = null;
  });
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
    (task.crew_tasks || []).forEach((item) => {
      if (item.agent_id === agentId)
        item.agent_id = task.crew_agent_ids[0] || null;
    });
  } else task.crew_agent_ids.push(agentId);
}
function addAgent() {
  workflow.value.structure_confirmed = true;
  const id = Math.random().toString(36).slice(2, 10);
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
    max_rpm: null,
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
  const type = workflow.value.kind === "crew" ? "task" : requestedType;
  const id = Math.random().toString(36).slice(2, 10);
  const needsAgent = type !== "router";
  const agentId = needsAgent
    ? workflow.value.agents[0]?.id || addAgent()
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
    agent_id: type === "crew" ? null : agentId,
    crew_agent_ids:
      type === "crew" ? workflow.value.agents.map((item) => item.id) : [],
    crew_tasks:
      type === "crew"
        ? [
            defaultCrewTask({
              crew_agent_ids: workflow.value.agents.map((item) => item.id),
            }),
          ]
        : [],
    depends_on: [],
    output_variables: [defaultOutputVariable()],
    dependency_variables: {},
    node_type: type,
    crew_process: "sequential",
    condition: type === "router" ? "contains:approved" : "",
    run_if: "",
    routes: {},
    async_execution: false,
    human_feedback: false,
    feedback_message: "Please review this step output",
    feedback_outcomes: ["approved", "revise"],
    feedback_default_outcome: null,
    markdown: false,
    output_file: "",
    create_directory: true,
    guardrail: "",
    guardrail_max_retries: 3,
    position: { x: 120 + count * CANVAS_COLUMN_GAP, y: CANVAS_TASK_Y },
  });
  selectedTaskId.value = id;
  selectedAgentId.value = "";
  selectedEdgeId.value = "";
  fitCanvas();
}
function addCrewTask(task) {
  if (!task || task.node_type !== "crew") return;
  task.crew_tasks ||= [];
  task.crew_tasks.push(defaultCrewTask(task, task.crew_tasks.length));
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
}
function addCrewTaskDependency(task, nested) {
  const nestedIndex = (task.crew_tasks || []).findIndex(
    (item) => item.id === nested.id,
  );
  const options = (task.crew_tasks || [])
    .slice(0, Math.max(0, nestedIndex))
    .filter((item) => !nested.depends_on.includes(item.id));
  if (!options.length) return;
  const dependency = options[options.length - 1];
  nested.depends_on.push(dependency.id);
  nested.dependency_variables ||= {};
  nested.dependency_variables[dependency.id] = [
    { source_variable: "result", target_variable: "context" },
  ];
}
function updateCrewDependencySource(nested, dependencyId, sourceVariable) {
  nested.dependency_variables ||= {};
  const mappings = nested.dependency_variables[dependencyId] || [];
  nested.dependency_variables[dependencyId] = [
    {
      source_variable: sourceVariable,
      target_variable: mappings[0]?.target_variable || "context",
    },
  ];
}
function updateCrewDependencyTarget(nested, dependencyId, targetVariable) {
  nested.dependency_variables ||= {};
  const mappings = nested.dependency_variables[dependencyId] || [];
  nested.dependency_variables[dependencyId] = [
    {
      source_variable: mappings[0]?.source_variable || "result",
      target_variable:
        targetVariable
          .trim()
          .replace(/[^A-Za-z0-9_]/g, "_")
          .replace(/^[^A-Za-z_]+/, "") || "context",
    },
  ];
}
function removeCrewTaskDependency(nested, dependencyId) {
  nested.depends_on = (nested.depends_on || []).filter(
    (id) => id !== dependencyId,
  );
  if (nested.dependency_variables)
    delete nested.dependency_variables[dependencyId];
}
function changeNodeType(type) {
  const task = selectedTask.value;
  if (!task || workflow.value.kind === "crew") return;
  task.node_type = type;
  if (type === "router") {
    task.agent_id = null;
    task.crew_agent_ids = [];
    task.condition ||= "contains:approved";
  } else if (type === "crew") {
    task.agent_id = null;
    task.crew_process ||= "sequential";
    task.crew_agent_ids = task.crew_agent_ids?.length
      ? task.crew_agent_ids
      : workflow.value.agents.map((item) => item.id);
    task.crew_tasks = task.crew_tasks?.length
      ? task.crew_tasks
      : [defaultCrewTask(task)];
  } else {
    task.agent_id ||= workflow.value.agents[0]?.id || addAgent();
    task.crew_agent_ids = [];
    task.crew_tasks = [];
  }
}
function isValidConnection(connection) {
  if (restoringEdges) return true;
  const { source, target } = connection;
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
    return targetTask.node_type === "crew"
      ? !targetTask.crew_agent_ids.includes(agentId)
      : targetTask.node_type !== "router" && targetTask.agent_id !== agentId;
  }
  if (
    !workflow.value.tasks.some((item) => item.id === source) ||
    targetTask.depends_on.includes(source)
  )
    return false;
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
    if (target.node_type === "crew") target.crew_agent_ids.push(agentId);
    else target.agent_id = agentId;
  } else {
    target.depends_on.push(connection.source);
    const sourceName =
      taskName(connection.source)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_|_$/g, "") || "context";
    const sourceVariable =
      outputOptions(connection.source)[0]?.name || "result";
    target.dependency_variables[connection.source] = [
      { source_variable: sourceVariable, target_variable: sourceName },
    ];
  }
}

function addVariableMapping(task, dependencyId) {
  task.dependency_variables[dependencyId] ||= [];
  const used = new Set(
    task.dependency_variables[dependencyId].map((item) => item.source_variable),
  );
  const source =
    outputOptions(dependencyId).find((item) => !used.has(item.name))?.name ||
    outputOptions(dependencyId)[0]?.name ||
    "result";
  const base = source === "result" ? "context" : source;
  let target = base;
  let suffix = 2;
  const targetNames = new Set(
    Object.values(task.dependency_variables)
      .flat()
      .map((item) => item.target_variable),
  );
  while (targetNames.has(target)) target = `${base}_${suffix++}`;
  task.dependency_variables[dependencyId].push({
    source_variable: source,
    target_variable: target,
  });
}
function removeVariableMapping(task, dependencyId, index) {
  task.dependency_variables[dependencyId].splice(index, 1);
}
function addOutputVariable(task) {
  const used = new Set(task.output_variables.map((item) => item.name));
  let name = "field";
  let suffix = 2;
  while (used.has(name)) name = `field_${suffix++}`;
  task.output_variables.push({ name, description: "", value_type: "string" });
}
function renameOutputVariable(task, index, value) {
  const previous = task.output_variables[index].name;
  const name =
    value
      .trim()
      .replace(/[^A-Za-z0-9_]/g, "_")
      .replace(/^[^A-Za-z_]+/, "") || previous;
  if (
    task.output_variables.some(
      (item, itemIndex) => itemIndex !== index && item.name === name,
    )
  ) {
    store.error = "输出变量名不能重复";
    return;
  }
  task.output_variables[index].name = name;
  workflow.value.tasks.forEach((item) =>
    (item.dependency_variables?.[task.id] || []).forEach((mapping) => {
      if (mapping.source_variable === previous) mapping.source_variable = name;
    }),
  );
}
function renameCrewOutputVariable(task, index, value) {
  const previous = task.output_variables[index].name;
  const name =
    value
      .trim()
      .replace(/[^A-Za-z0-9_]/g, "_")
      .replace(/^[^A-Za-z_]+/, "") || previous;
  if (
    task.output_variables.some(
      (item, itemIndex) => itemIndex !== index && item.name === name,
    )
  ) {
    store.error = "内部任务输出变量名不能重复";
    return;
  }
  task.output_variables[index].name = name;
  workflow.value.tasks
    .flatMap((item) => item.crew_tasks || [])
    .forEach((item) => {
      (item.dependency_variables?.[task.id] || []).forEach((mapping) => {
        if (mapping.source_variable === previous)
          mapping.source_variable = name;
      });
    });
}
function removeOutputVariable(task, index) {
  if (task.output_variables.length === 1) {
    store.error = "任务至少需要保留一个输出变量";
    return;
  }
  const removed = task.output_variables[index].name;
  task.output_variables.splice(index, 1);
  workflow.value.tasks.forEach((item) => {
    if (item.dependency_variables?.[task.id])
      item.dependency_variables[task.id] = item.dependency_variables[
        task.id
      ].filter((mapping) => mapping.source_variable !== removed);
  });
}
function nodeClick({ node }) {
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
}
function removeEdge() {
  const edge = selectedEdge.value;
  if (!edge) return;
  const target = workflow.value.tasks.find((item) => item.id === edge.target);
  if (edge.edgeType === "dependency") {
    target.depends_on = target.depends_on.filter((id) => id !== edge.source);
    delete target.dependency_variables[edge.source];
  } else if (edge.edgeType === "member" && target.crew_agent_ids.length > 1) {
    const removedAgent = edge.source.slice(6);
    target.crew_agent_ids = target.crew_agent_ids.filter(
      (id) => id !== removedAgent,
    );
    (target.crew_tasks || []).forEach((item) => {
      if (item.agent_id === removedAgent)
        item.agent_id = target.crew_agent_ids[0] || null;
    });
  } else {
    store.error =
      "执行节点必须保留 Agent。请在右侧分配其他 Agent，或删除整个节点。";
    return;
  }
  selectedEdgeId.value = "";
}
function removeTask() {
  const task = selectedTask.value;
  if (!task) return;
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
    answer.streaming = false;
    answer.text ||= `没有完成：${error.message}`;
    answer.role = "error";
    store.error = error.message;
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
  <div class="studio-page">
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
        >
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

    <div class="studio-layout" :class="{ 'awaiting-structure': !builderReady }">
      <aside class="studio-panel">
        <div class="studio-panel-head">
          <strong>Assistant</strong><Sparkles :size="14" />
        </div>
        <div ref="assistantThread" class="assistant-thread">
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
                    <select v-model="item.input_type" :disabled="item.name === 'message' || proposalStageLocked(message.proposal, 'inputs')">
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
                    ><label v-if="['file', 'image'].includes(item.input_type)"
                      ><input
                        v-model="item.multiple"
                        type="checkbox"
                        :disabled="proposalStageLocked(message.proposal, 'inputs')"
                      />多文件</label
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
                      <GitBranch :size="15" /><strong>Flow</strong><small>状态、分支或事件驱动</small>
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
          <p>添加至少一个模型后，才能与 Studio Assistant 对话或生成编排。</p>
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
                <span class="studio-history-icon"
                  ><GitBranch
                    v-if="item.kind === 'flow'"
                    :size="15" /><UsersRound v-else :size="15"
                /></span>
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
                v-if="data.task.node_type !== 'router'"
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
              <div class="flow-node-agent">
                <span class="agent-dot"
                  ><UserRound v-if="data.agent" :size="13" /><Route
                    v-else-if="data.task.node_type === 'router'"
                    :size="13" /><UsersRound v-else :size="13"
                /></span>
                <div>
                  <b>{{
                    data.agent?.role ||
                    (data.task.node_type === "router"
                      ? "Deterministic condition"
                      : `${data.task.crew_agent_ids.length} Crew members`)
                  }}</b
                  ><small>{{
                    data.task.node_type === "router"
                      ? data.task.condition
                      : data.model
                  }}</small>
                </div>
              </div>
              <Handle
                id="context-out"
                type="source"
                :position="Position.Right"
              /></div
          ></template>
          <Panel position="top-left" class="canvas-toolbar"
            ><button class="icon-button" title="缩小" @click="zoomOut()">
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
          >
        </div>
        <div class="studio-quick-add">
          <template v-if="!builderReady"
            ><button class="button small" @click="chooseStructure('flow')">
              <GitBranch :size="12" />Flow</button
            ><button class="button small" @click="chooseStructure('crew')">
              <UsersRound :size="12" />Crew
            </button></template
          ><template v-else
            ><button
              class="button small"
              title="添加 Agent 定义"
              @click="addAgent"
            >
              <Plus :size="12" />Agent</button
            ><button
              v-if="workflow.kind === 'crew'"
              class="button small"
              title="添加 Crew Task"
              @click="addStep('task')"
            >
              <Plus :size="12" />Task</button
            ><template v-else
              ><button
                class="button small"
                title="添加 Agent call"
                @click="addStep('agent')"
              >
                <Bot :size="12" />Call</button
              ><button
                class="button small"
                title="添加 Crew kickoff"
                @click="addStep('crew')"
              >
                <UsersRound :size="12" />Crew</button
              ><button
                class="icon-button"
                title="添加 Router"
                @click="addStep('router')"
              >
                <Route :size="13" /></button></template
          ></template>
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
              ><textarea v-model="selectedAgent.goal"></textarea>
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="背景（backstory）"
                  help="提供专业背景和工作边界，帮助模型稳定地扮演角色。" /></label
              ><textarea v-model="selectedAgent.backstory"></textarea>
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
                class="toggle" /></label
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
            <div class="field">
              <label
                ><ParamLabel
                  text="Crew 执行方式（crew_process）"
                  help="Flow 本身由事件驱动；这里决定嵌入 Crew 内部是按顺序执行，还是由 manager_llm 动态分配任务。" /></label
              ><select
                :value="selectedTask.crew_process"
                @change="changeEmbeddedCrewProcess(selectedTask, $event.target.value)"
              >
                <option value="sequential">顺序协作（sequential）</option>
                <option value="hierarchical">
                  层级协作（hierarchical）
                </option></select
              ><small>层级模式使用成员列表中的第一个 Agent 作为管理 Agent。</small>
            </div>
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
                  help="agent：调用一个 Agent；crew：在 Flow 中启动一个多任务 Crew；router：只做确定性分支。" /></label
              ><select
                :value="selectedTask.node_type"
                :disabled="workflow.kind === 'crew'"
                @change="changeNodeType($event.target.value)"
              >
                <option v-if="workflow.kind === 'crew'" value="task">
                  Crew 任务（task）
                </option>
                <template v-else
                  ><option value="agent">单 Agent 调用（agent_call）</option>
                  <option value="crew">Crew 协作（crew）</option>
                  <option value="router">条件路由（router）</option>
                  <option v-if="selectedTask.node_type === 'code'" value="code">
                    代码方法（code）
                  </option></template
                >
              </select>
            </div>
            <div class="field">
              <label>名称（name）</label
              ><input
                v-model="selectedTask.name"
              />
            </div>
            <div class="field">
              <label
                ><ParamLabel
                  text="任务描述（description）"
                  help="告诉 Agent 要做什么；可以使用已声明的运行输入变量，例如 {contract_file}。" /></label
              ><textarea
                v-model="selectedTask.description"
              ></textarea>
            </div>
            <div v-if="selectedTask.node_type !== 'router'" class="field">
              <label
                ><ParamLabel
                  text="期望输出（expected_output）"
                  help="定义完成标准和输出形态，CrewAI 会用它判断任务是否完成。" /></label
              ><textarea
                v-model="selectedTask.expected_output"
              ></textarea>
            </div>
            <div v-if="selectedTask.node_type === 'router'" class="field">
              <label
                ><ParamLabel
                  text="条件（condition）"
                  help="使用 contains:关键词、equals:文本 或 always 等安全规则，不执行任意 Python。" /></label
              ><input
                v-model="selectedTask.condition"
                placeholder="contains:approved"
              />
            </div>
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
          <section
            v-if="selectedTask.depends_on.length"
            class="inspector-section variable-section"
          >
            <h3>输入变量映射（dependency_variables）</h3>
            <div
              v-for="dependency in selectedTask.depends_on"
              :key="dependency"
              class="variable-group"
            >
              <div class="variable-group-head">
                <span>{{ taskName(dependency) }}</span
                ><button
                  class="icon-button"
                  title="添加变量映射"
                  @click="addVariableMapping(selectedTask, dependency)"
                >
                  <Plus :size="12" />
                </button>
              </div>
              <div
                v-for="(mapping, index) in selectedTask.dependency_variables[
                  dependency
                ]"
                :key="index"
                class="variable-mapping-row"
              >
                <select
                  v-model="mapping.source_variable"
                  aria-label="上游输出变量"
                >
                  <option value="$raw">完整输出</option>
                  <option
                    v-for="field in outputOptions(dependency)"
                    :key="field.name"
                    :value="field.name"
                  >
                    {{ field.name }}
                  </option>
                </select>
                <span>→</span
                ><input
                  v-model="mapping.target_variable"
                  aria-label="下游输入变量"
                  placeholder="variable_name"
                />
                <button
                  class="icon-button"
                  title="删除变量映射"
                  @click="
                    removeVariableMapping(selectedTask, dependency, index)
                  "
                >
                  <Trash2 :size="12" />
                </button>
              </div>
              <p v-if="!selectedTask.dependency_variables[dependency]?.length">
                未选择字段时，仍会传递完整 Task context。
              </p>
            </div>
          </section>
          <section
            v-if="
              selectedTask.node_type !== 'router' &&
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
              ><small>也可以从 Agent 节点下方端口连接到任务顶部。</small>
            </div>
          </section>
          <section
            v-if="selectedTask.node_type === 'crew'"
            class="inspector-section"
          >
            <h3>Crew 成员（crew_agent_ids）</h3>
            <label
              v-for="agent in workflow.agents"
              :key="agent.id"
              class="toggle-row"
              ><span>{{ agent.role }}</span
              ><input
                :checked="selectedTask.crew_agent_ids.includes(agent.id)"
                type="checkbox"
                @click.prevent="toggleCrewMember(agent.id)"
            /></label>
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
                ><textarea v-model="nested.description"></textarea>
              </div>
              <div class="field">
                <label>期望输出（expected_output）</label
                ><textarea v-model="nested.expected_output"></textarea>
              </div>
              <div class="field">
                <label>执行 Agent（agent_id）</label
                ><select v-model="nested.agent_id">
                  <option :value="null">默认执行 Agent（default）</option>
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
                  ><button
                    class="icon-button"
                    title="添加内部任务依赖"
                    @click="addCrewTaskDependency(selectedTask, nested)"
                  >
                    <Plus :size="11" />
                  </button>
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
                <div class="section-title-action">
                  <label>输出变量（output_variables）</label
                  ><button
                    class="icon-button"
                    title="添加内部任务输出变量"
                    @click="addOutputVariable(nested)"
                  >
                    <Plus :size="11" />
                  </button>
                </div>
                <div
                  v-for="(field, fieldIndex) in nested.output_variables"
                  :key="fieldIndex"
                  class="output-variable-row"
                >
                  <input
                    v-model="field.name"
                    aria-label="变量名（name）"
                  /><select
                    v-model="field.value_type"
                    aria-label="变量类型（value_type）"
                  >
                    <option value="string">文本（string）</option>
                    <option value="number">数字（number）</option>
                    <option value="boolean">布尔（boolean）</option>
                    <option value="object">对象（object）</option>
                    <option value="array">数组（array）</option>
                    <option value="file">文件（file）</option></select
                  ><input
                    v-model="field.description"
                    class="variable-description"
                    aria-label="变量说明（description）"
                    placeholder="字段说明"
                  /><button
                    class="icon-button"
                    title="删除输出变量"
                    @click="removeOutputVariable(nested, fieldIndex)"
                  >
                    <Trash2 :size="11" />
                  </button>
                </div>
              </div>
              <div class="crew-task-options">
                <label
                  ><input
                    v-model="nested.markdown"
                    type="checkbox"
                  />Markdown（markdown）</label
                ><label
                  ><input
                    v-model="nested.async_execution"
                    type="checkbox"
                  />异步（async_execution）</label
                >
              </div>
              <div class="field">
                <label
                  ><ParamLabel
                    text="校验函数（guardrail）"
                    help="Python callable 路径，例如 package.module:validate；运行环境必须能导入。" /></label
                ><input
                  v-model="nested.guardrail"
                  placeholder="package.module:validate"
                />
              </div>
              <div v-if="nested.guardrail" class="field">
                <label>校验重试次数（guardrail_max_retries）</label
                ><input
                  v-model.number="nested.guardrail_max_retries"
                  type="number"
                  min="0"
                  max="20"
                />
              </div>
            </article>
          </section>
          <section
            v-if="selectedTask.node_type !== 'router'"
            class="inspector-section variable-section"
          >
            <div class="section-title-action">
              <h3>输出变量（output_variables）</h3>
              <button
                class="icon-button"
                title="添加输出变量"
                @click="addOutputVariable(selectedTask)"
              >
                <Plus :size="12" />
              </button>
            </div>
            <p>下游连线可选择这些字段。多个字段会启用 CrewAI 结构化输出。</p>
            <div
              v-for="(field, index) in selectedTask.output_variables"
              :key="index"
              class="output-variable-row"
            >
              <input
                :value="field.name"
                aria-label="输出变量名（name）"
                @change="
                  renameOutputVariable(selectedTask, index, $event.target.value)
                "
              /><select
                v-model="field.value_type"
                aria-label="输出变量类型（value_type）"
              >
                <option value="string">文本（string）</option>
                <option value="number">数字（number）</option>
                <option value="boolean">布尔（boolean）</option>
                <option value="object">对象（object）</option>
                <option value="array">数组（array）</option>
                <option value="file">文件（file）</option></select
              ><input
                v-model="field.description"
                class="variable-description"
                aria-label="输出变量说明（description）"
                placeholder="字段说明（description）"
              /><button
                class="icon-button"
                title="删除输出变量"
                @click="removeOutputVariable(selectedTask, index)"
              >
                <Trash2 :size="12" />
              </button>
            </div>
          </section>
          <section
            v-if="selectedTask.node_type !== 'router'"
            class="inspector-section"
          >
            <h3>
              {{
                workflow.kind === "flow"
                  ? "输出与审核（output_review）"
                  : "输出设置（output）"
              }}
            </h3>
            <label class="toggle-row"
              ><span>Markdown 输出（markdown）</span
              ><input
                v-model="selectedTask.markdown"
                type="checkbox"
                class="toggle" /></label
            ><label class="toggle-row"
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
            ><label v-if="workflow.kind === 'flow'" class="toggle-row"
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
              v-if="workflow.kind === 'flow' && selectedTask.human_feedback"
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
            <div class="field">
              <label
                ><ParamLabel
                  text="校验函数（guardrail）"
                  help="Python callable 路径，例如 package.module:validate。" /></label
              ><input
                v-model="selectedTask.guardrail"
                placeholder="package.module:validate"
              />
            </div>
            <div v-if="selectedTask.guardrail" class="field">
              <label>校验重试次数（guardrail_max_retries）</label
              ><input
                v-model.number="selectedTask.guardrail_max_retries"
                type="number"
                min="0"
                max="20"
              />
            </div>
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
                  : "Agent 关系（assignment / member）"
              }}
            </h3>
            <div class="connection-endpoint">
              <span>{{ taskName(selectedEdge.source) }}</span
              ><GitBranch :size="14" /><span>{{
                taskName(selectedEdge.target)
              }}</span>
            </div>
            <template v-if="selectedEdge.edgeType === 'dependency'"
              ><div
                class="variable-mapping-row"
                v-for="(mapping, index) in workflow.tasks.find(
                  (item) => item.id === selectedEdge.target,
                ).dependency_variables[selectedEdge.source]"
                :key="index"
              >
                <select
                  v-model="mapping.source_variable"
                  aria-label="上游输出变量（source_variable）"
                >
                  <option value="$raw">完整输出（$raw）</option>
                  <option
                    v-for="field in outputOptions(selectedEdge.source)"
                    :key="field.name"
                    :value="field.name"
                  >
                    {{ field.name }}
                  </option></select
                ><span>→</span
                ><input
                  v-model="mapping.target_variable"
                  aria-label="下游变量（target_variable）"
                /><button
                  class="icon-button"
                  title="删除变量映射"
                  @click="
                    removeVariableMapping(
                      workflow.tasks.find(
                        (item) => item.id === selectedEdge.target,
                      ),
                      selectedEdge.source,
                      index,
                    )
                  "
                >
                  <Trash2 :size="12" />
                </button>
              </div>
              <button
                class="button small"
                @click="
                  addVariableMapping(
                    workflow.tasks.find(
                      (item) => item.id === selectedEdge.target,
                    ),
                    selectedEdge.source,
                  )
                "
              >
                <Plus :size="12" />添加字段映射
              </button></template
            >
            <p>
              {{
                selectedEdge.edgeType === "dependency"
                  ? "上游输出字段会在运行时提取，并绑定到下游 {变量名}。"
                  : "上、下端口对应 Agent 分配或 Crew 成员关系。"
              }}
            </p>
          </section>
          <button class="button danger" @click="removeEdge">
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
                v-model="input.name"
                aria-label="变量名（name）"
                :disabled="input.name === 'message'"
              /><select v-model="input.input_type" :disabled="input.name === 'message'">
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
                @click="workflow.inputs.splice(index, 1)"
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
          </section>
          <section
            v-if="workflow.kind === 'crew'"
            class="inspector-section crew-settings"
          >
            <h3><Settings2 :size="13" />Crew 设置（Crew）</h3>
            <div class="field">
              <label
                ><ParamLabel
                  text="执行模式（process）"
                  help="sequential 按依赖顺序执行；hierarchical 由管理 Agent 分配任务。" /></label
              ><select
                :value="workflow.process"
                @change="changeProcess($event.target.value)"
              >
                <option value="sequential">顺序（sequential）</option>
                <option value="hierarchical">层级（hierarchical）</option>
              </select>
            </div>
            <template v-if="workflow.process === 'hierarchical'"
              ><div class="field">
                <label>管理 Agent（manager_agent）</label
                ><select
                  :value="workflow.manager_agent_id || ''"
                  @change="setManager($event.target.value)"
                >
                  <option value="">CrewAI 自动管理（manager_llm）</option>
                  <option
                    v-for="agent in workflow.agents"
                    :key="agent.id"
                    :value="agent.id"
                  >
                    {{ agent.role }}
                  </option>
                </select>
              </div>
              <div v-if="!workflow.manager_agent_id" class="field">
                <label>管理模型（manager_llm）</label
                ><select v-model="workflow.manager_model_profile_id">
                  <option :value="null">工作流默认（workflow_default）</option>
                  <option
                    v-for="model in store.chatModels"
                    :key="model.id"
                    :value="model.id"
                  >
                    {{ model.name }}
                  </option>
                </select>
              </div></template
            ><label class="toggle-row"
              ><span
                >执行规划（planning）<button
                  class="field-help"
                  type="button"
                  title="Crew 级任务规划，适合需要动态拆解的多步骤目标。单步规则任务请关闭，避免额外规划和重复反思。"
                  aria-label="执行规划帮助"
                >
                  <CircleHelp :size="12" /></button></span
              ><input
                v-model="workflow.planning"
                type="checkbox"
                class="toggle"
            /></label>
            <div v-if="workflow.planning" class="field">
              <label>规划模型（planning_llm）</label
              ><select v-model="workflow.planning_model_profile_id">
                <option :value="null">工作流默认（workflow_default）</option>
                <option
                  v-for="model in store.chatModels"
                  :key="model.id"
                  :value="model.id"
                >
                  {{ model.name }}
                </option>
              </select>
            </div>
            <label class="toggle-row"
              ><span>记忆（memory）</span
              ><input
                v-model="workflow.memory"
                type="checkbox"
                class="toggle" /></label
            ><label class="toggle-row"
              ><span>工具缓存（cache）</span
              ><input v-model="workflow.cache" type="checkbox" class="toggle"
            /></label>
            <div class="field">
              <label>每分钟最大请求数（max_rpm）</label
              ><input
                v-model.number="workflow.max_rpm"
                type="number"
                min="1"
                placeholder="不限制"
              />
            </div>
            <div class="field">
              <label>执行日志文件（output_log_file）</label
              ><input
                v-model="workflow.output_log_file"
                placeholder="logs/crew.json"
              />
            </div>
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
          <div class="resource-group">
            <div class="resource-group-title">
              <span>Agent 定义（agents）</span
              ><span>{{ workflow.agents.length }}</span>
            </div>
            <button
              v-for="agent in workflow.agents"
              :key="agent.id"
              class="resource-item palette-item"
              @click="selectedAgentId = agent.id"
            >
              <span><Bot :size="13" /></span>
              <div>
                <strong>{{ agent.role }}</strong
                ><small>Agent（role, goal, tools, skills）</small>
              </div></button
            ><button
              class="button small"
              @click="addAgent"
            >
              <Plus :size="13" />新增 Agent
            </button>
          </div>
          <div class="resource-group">
            <div class="resource-group-title">
              <span>{{
                workflow.kind === "crew" ? "Crew 任务" : "Flow 执行节点"
              }}</span
              ><Plus :size="12" />
            </div>
            <button
              v-if="workflow.kind === 'crew'"
              class="resource-item palette-item"
              @click="addStep('task')"
            >
              <span><ListTodo :size="13" /></span>
              <div>
                <strong>Crew 任务（task）</strong
                ><small>Task(description, agent, context)</small>
              </div></button
            ><template
              ><button
                class="resource-item palette-item"
                title="只需要一个角色完成该步骤时使用，直接调用 Agent.kickoff；不必为了单步工作创建 Crew。"
                @click="addStep('agent')"
              >
                <span><Bot :size="13" /></span>
                <div>
                  <strong>单 Agent 调用（agent_call）</strong
                  ><small>Agent.kickoff(...)</small>
                </div>
                <CircleHelp :size="12" /></button
              ><button
                class="resource-item palette-item"
                title="需要多个 Agent 按明确任务协作时使用；节点内部可配置 Crew Task。"
                @click="addStep('crew')"
              >
                <span><UsersRound :size="13" /></span>
                <div>
                  <strong>Crew 协作（crew）</strong
                  ><small>Crew(tasks=[...]).kickoff(...)</small>
                </div>
                <CircleHelp :size="12" /></button
              ><button
                class="resource-item palette-item"
                @click="addStep('router')"
              >
                <span><Route :size="13" /></span>
                <div>
                  <strong>条件路由（router）</strong
                  ><small>Flow router outcome</small>
                </div>
              </button></template
            >
          </div>
          <div class="canvas-help">
            <MousePointer2 :size="13" />
            <p>左右端口传递任务上下文；Agent 从下方连接到任务上方。</p>
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
      <div ref="previewThread" class="studio-preview-thread">
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
            <div v-if="message.steps?.length" class="run-activity">
              <header>
                <GitBranch :size="12" /><strong>执行进度</strong
                ><span
                  >{{
                    message.steps.filter((item) => item.status === "completed")
                      .length
                  }}/{{ message.steps.length }}</span
                >
              </header>
              <ol>
                <li
                  v-for="step in message.steps"
                  :key="step.step_id"
                  :class="[step.status, { expanded: step.expanded }]"
                >
                  <i></i>
                  <div>
                    <div class="run-step-heading">
                      <strong>{{ step.agent_role }}</strong
                      ><button
                        v-if="step.output"
                        class="run-step-toggle"
                        type="button"
                        :title="
                          step.expanded ? '收起智能体输出' : '展开智能体输出'
                        "
                        :aria-label="
                          step.expanded ? '收起智能体输出' : '展开智能体输出'
                        "
                        :aria-expanded="step.expanded"
                        @click="step.expanded = !step.expanded"
                      >
                        <ChevronDown :size="13" />
                      </button>
                    </div>
                    <small
                      >{{ step.step_name
                      }}<template v-if="step.tool_name">
                        · {{ step.tool_name }}</template
                      ></small
                    >
                    <p v-if="step.preview" :class="{ expanded: step.expanded }">
                      {{ step.expanded ? step.output : step.preview }}
                    </p>
                  </div>
                </li>
              </ol>
            </div>
            <RunApprovalCard
              v-for="approval in message.approvals || []"
              :key="`${approval.runId}-${approval.step_id}-${approval.status}`"
              :approval="approval"
              :busy="approval.busy"
              @submit="submitPreviewFeedback(message, approval, $event)"
            />
            <div v-if="message.runtimeNotice" class="run-runtime-notice">
              {{ message.runtimeNotice }}
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
            </div>
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
