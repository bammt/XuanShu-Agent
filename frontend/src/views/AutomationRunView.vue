<script setup>
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ArrowLeft,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  Code2,
  Download,
  ExternalLink,
  FileText,
  GitBranch,
  Image,
  LoaderCircle,
  Paperclip,
  Plus,
  Send,
  SlidersHorizontal,
  SquarePen,
  Trash2,
  Upload,
  X,
  XCircle,
} from "lucide-vue-next";
import { api } from "../services/api";
import { applyRunFrame } from "../services/runStream";
import { stripLocalArtifactReferences } from "../services/messageFormatting";
import { usePlatformStore } from "../stores/platform";
import { confirmDialog } from "../services/dialog";
import RunApprovalCard from "../components/RunApprovalCard.vue";
import RichMessage from "../components/RichMessage.vue";
import { formatBeijingDateTime } from "../services/dateFormatting";

const route = useRoute();
const router = useRouter();
const store = usePlatformStore();
const values = reactive({});
const files = reactive({});
const messages = ref([]);
const currentMessage = ref("");
const busy = ref(false);
const uploading = ref(false);
const uploadProgress = ref(0);
const pendingUploadNames = ref([]);
const uploadTarget = ref("");
const loading = ref(true);
const variablesOpen = ref(false);
const chatThread = ref(null);
const composerFileInput = ref(null);
const conversationId = ref("");
const conversations = ref([]);
let pollTimer = null;
let runAbortController = null;
let conversationLoading = false;

// Runtime pages never use the editable store graph. Load the last published
// snapshot directly so draft edits cannot change a live conversation.
const workflow = ref(null);
const primaryInput = computed(
  () =>
    workflow.value?.inputs.find((item) =>
      ["text", "long_text"].includes(item.input_type),
    ) || null,
);
const fileInputs = computed(
  () =>
    workflow.value?.inputs.filter((item) =>
      ["file", "image"].includes(item.input_type),
    ) || [],
);
const composerFileField = computed(() => fileInputs.value[0] || null);
const variableInputs = computed(() =>
  (workflow.value?.inputs || []).filter(
    (item) => item.name !== primaryInput.value?.name,
  ),
);
const inlineVariableInputs = computed(() =>
  variableInputs.value.filter(
    (item) => !["file", "image"].includes(item.input_type),
  ),
);
const selectedFiles = computed(() => Object.values(files).flat());
const hasVariableValue = computed(() =>
  variableInputs.value.some((input) => {
    if (["file", "image"].includes(input.input_type))
      return Boolean(files[input.name]?.length);
    if (input.input_type === "boolean") return values[input.name] === true;
    return (
      values[input.name] !== "" &&
      values[input.name] !== null &&
      values[input.name] !== undefined
    );
  }),
);
const canSend = computed(
  () =>
    !busy.value &&
    !uploading.value &&
    Boolean(
      currentMessage.value.trim() ||
      selectedFiles.value.length ||
      hasVariableValue.value ||
      !workflow.value?.inputs.length,
    ),
);

function assistantWelcome() {
  return {
    role: "assistant",
    text:
      workflow.value?.description ||
      "你好，请告诉我你希望完成什么，也可以附加文件或设置运行变量。",
  };
}
function initializeValues() {
  for (const input of workflow.value?.inputs || []) {
    values[input.name] = input.input_type === "boolean" ? false : "";
    files[input.name] = [];
  }
}
function restoreConversation(conversation) {
  conversationId.value = conversation.id;
  const runs = conversation.runs || [];
  messages.value = [assistantWelcome()];
  let activeRun = null;
  for (const item of runs) {
    if (item.user_message)
      messages.value.push({
        role: "user",
        text: item.user_message,
        attachments: attachmentNames(item),
      });
    if (item.status === "completed")
      messages.value.push({
        role: "assistant",
        text: stripLocalArtifactReferences(item.output),
        runId: item.id,
        status: item.status,
        routerOnly: item.metrics?.runtime_type === "conversation_router",
        files: item.files || [],
      });
    else if (item.status === "failed")
      messages.value.push({
        role: item.output ? "assistant" : "error",
        text: stripLocalArtifactReferences(item.output),
        error: item.error || "执行失败",
        runId: item.id,
        status: item.status,
      });
    else if (item.status === "waiting_for_feedback") {
      const answer = reactive({
        role: "assistant",
        text: "",
        runId: item.id,
        status: item.status,
        streaming: false,
        steps: [],
      });
      messages.value.push(answer);
      openReview(item.id, item.pending_feedback, answer);
    } else if (item.status === "waiting_input") {
      if (
        ["file", "image"].includes(item.waiting_input?.input_type) ||
        item.waiting_input?.accepts_files ||
        item.waiting_input?.file_input_names?.length
      )
        variablesOpen.value = true;
      messages.value.push({
        role: "assistant",
        text: stripLocalArtifactReferences(item.output) || item.waiting_input?.question || "请补充必要信息。",
        runId: item.id,
        status: item.status,
        waitingInput: item.waiting_input,
        streaming: false,
      });
    } else if (["queued", "running"].includes(item.status)) {
      const answer = reactive({
        role: "assistant",
        text: "",
        runId: item.id,
        status: item.status,
        streaming: true,
        steps: [],
      });
      messages.value.push(answer);
      activeRun = { id: item.id, answer };
    }
  }
  scrollChat();
  if (activeRun) {
    busy.value = true;
    streamRun(activeRun.id, activeRun.answer);
  }
}
async function refreshConversations() {
  conversations.value = await api.conversations(workflow.value.id);
}
async function loadConversation() {
  if (conversationLoading) return;
  conversationLoading = true;
  let conversation = null;
  try {
    const requested = String(route.query.conversation || "");
    if (requested) {
      try {
        conversation = await api.conversation(workflow.value.id, requested);
      } catch (_) {
        conversation = null;
      }
    }
    if (!conversation) {
      await refreshConversations();
      conversation = conversations.value[0]
        ? await api.conversation(workflow.value.id, conversations.value[0].id)
        : await api.createConversation(workflow.value.id);
    }
    restoreConversation(conversation);
    await refreshConversations();
    if (String(route.query.conversation || "") !== String(conversation.id))
      await router.replace({
        path: route.path,
        query: { ...route.query, conversation: conversation.id },
      });
  } finally {
    conversationLoading = false;
  }
}
onMounted(async () => {
  try {
    workflow.value = await api.runtimeWorkflow(route.params.id);
    if (!workflow.value || workflow.value.status !== "published") return;
    initializeValues();
    await loadConversation();
    variablesOpen.value = false;
  } catch (error) {
    store.error = error.message;
  } finally {
    loading.value = false;
  }
});
onBeforeUnmount(() => {
  if (pollTimer) window.clearTimeout(pollTimer);
  runAbortController?.abort();
});
watch(
  () => route.query.conversation,
  async (id, previous) => {
    if (!id || id === previous || id === conversationId.value || loading.value)
      return;
    if (pollTimer) window.clearTimeout(pollTimer);
    runAbortController?.abort();
    await loadConversation();
  },
);

function attachmentNames(record) {
  const names = [];
  for (const [field, ids] of Object.entries(record.attachments || {})) {
    const configured = record.inputs?.[field];
    const values = Array.isArray(configured) ? configured : [];
    ids.forEach((id, index) =>
      names.push({ id, name: values[index] || field }),
    );
  }
  return names;
}
function scrollChat() {
  nextTick(() => {
    if (chatThread.value)
      chatThread.value.scrollTop = chatThread.value.scrollHeight;
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
async function newConversation() {
  if (pollTimer) window.clearTimeout(pollTimer);
  runAbortController?.abort();
  runAbortController = null;
  busy.value = false;
  currentMessage.value = "";
  try {
    const conversation = await api.createConversation(workflow.value.id);
    restoreConversation(conversation);
    await refreshConversations();
    await router.replace({
      path: route.path,
      query: { ...route.query, conversation: conversation.id },
    });
    initializeValues();
  } catch (error) {
    store.error = error.message;
  }
}
async function chooseConversation(id) {
  if (!id || id === conversationId.value || busy.value) return;
  await router.replace({
    path: route.path,
    query: { ...route.query, conversation: id },
  });
}
async function deleteConversation(item) {
  if (busy.value) return;
  const confirmed = await confirmDialog({
    title: "删除对话",
    message: `确认删除对话“${item.title || "新对话"}”？此操作会删除该会话的历史运行。`,
    confirmLabel: "删除",
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api.deleteConversation(workflow.value.id, item.id);
    await refreshConversations();
    if (item.id !== conversationId.value) return;
    const next = conversations.value[0]
      ? await api.conversation(workflow.value.id, conversations.value[0].id)
      : await api.createConversation(workflow.value.id);
    restoreConversation(next);
    await refreshConversations();
    await router.replace({
      path: route.path,
      query: { ...route.query, conversation: next.id },
    });
  } catch (error) {
    store.error = error.message;
  }
}
async function chooseFiles(input, event) {
  const selected = Array.from(event.target.files || []);
  event.target.value = "";
  if (!selected.length || uploading.value) return;
  const startedAt = Date.now();
  uploading.value = true;
  uploadProgress.value = 0;
  uploadTarget.value = input.name;
  pendingUploadNames.value = selected.map((file) => file.name);
  try {
    const uploaded = await api.uploadStudioAttachments(selected, (value) => {
      uploadProgress.value = value;
    });
    files[input.name] = input.multiple
      ? [...files[input.name], ...uploaded]
      : uploaded.slice(0, 1);
  } catch (error) {
    store.error = error.message;
  } finally {
    await keepUploadStatusVisible(startedAt);
    uploading.value = false;
    uploadProgress.value = 0;
    pendingUploadNames.value = [];
    uploadTarget.value = "";
  }
}
async function chooseComposerFiles(event) {
  const selected = Array.from(event.target.files || []);
  event.target.value = "";
  if (!selected.length || !fileInputs.value.length || uploading.value) return;
  const startedAt = Date.now();
  uploading.value = true;
  uploadProgress.value = 0;
  uploadTarget.value = "composer";
  pendingUploadNames.value = selected.map((file) => file.name);
  try {
    const uploaded = await api.uploadStudioAttachments(selected, (value) => {
      uploadProgress.value = value;
    });
    for (const file of uploaded) {
      const matching = fileInputs.value.filter(
        (input) =>
          input.input_type !== "image" ||
          file.content_type?.startsWith("image/"),
      );
      const target =
        matching.find(
          (input) => input.multiple || !files[input.name]?.length,
        ) ||
        matching[0] ||
        fileInputs.value[0];
      files[target.name] ||= [];
      files[target.name] = target.multiple
        ? [...files[target.name], file]
        : [file];
    }
  } catch (error) {
    store.error = error.message;
  } finally {
    await keepUploadStatusVisible(startedAt);
    uploading.value = false;
    uploadProgress.value = 0;
    pendingUploadNames.value = [];
    uploadTarget.value = "";
  }
}
function removeFile(inputName, index) {
  files[inputName].splice(index, 1);
}
function parsedInputs() {
  const result = {};
  for (const input of workflow.value.inputs || []) {
    // File values travel only through ``attachments``. Keeping browser file
    // names out of ``inputs`` prevents a later ask_user merge from treating
    // an attachment label as a conversational answer.
    if (["file", "image"].includes(input.input_type)) continue;
    if (input.name === primaryInput.value?.name)
      result[input.name] = currentMessage.value.trim();
    else if (input.input_type === "json" && values[input.name]) {
      try {
        result[input.name] = JSON.parse(values[input.name]);
      } catch (_) {
        throw new Error(`${input.label} 不是有效的 JSON`);
      }
    } else if (input.input_type === "number" && values[input.name] !== "")
      result[input.name] = Number(values[input.name]);
    else result[input.name] = values[input.name];
  }
  return result;
}
function validateInputs(inputs, attachments) {
  if (workflow.value.interaction_mode === "multi_turn") return;
  const missing = [];
  for (const input of workflow.value.inputs || []) {
    if (!input.required) continue;
    const supplied = ["file", "image"].includes(input.input_type)
      ? Boolean(attachments[input.name]?.length)
      : inputs[input.name] !== "" &&
        inputs[input.name] !== null &&
        inputs[input.name] !== undefined;
    if (!supplied) missing.push(input.label);
  }
  if (missing.length) throw new Error(`请先填写：${missing.join("、")}`);
}
async function poll(id, answer) {
  try {
    const record = await api.run(id);
    answer.status = record.status;
    answer.runId = record.id;
    if (record.status === "completed") {
      answer.text = stripLocalArtifactReferences(record.output) || "执行已完成。";
      answer.files = record.files || [];
      answer.streaming = false;
      busy.value = false;
      answer.routerOnly =
        record.metrics?.runtime_type === "conversation_router";
      await store.load();
      scrollChat();
      return;
    }
    if (record.status === "failed") {
      answer.role = record.output ? "assistant" : "error";
      answer.text = stripLocalArtifactReferences(record.output);
      answer.error = record.error || "执行失败";
      answer.streaming = false;
      busy.value = false;
      await store.load();
      scrollChat();
      return;
    }
    if (record.status === "waiting_for_feedback") {
      openReview(record.id, record.pending_feedback, answer);
      await store.load();
      return;
    }
    if (record.status === "waiting_input") {
      if (
        ["file", "image"].includes(record.waiting_input?.input_type) ||
        record.waiting_input?.accepts_files ||
        record.waiting_input?.file_input_names?.length
      )
        variablesOpen.value = true;
      answer.text = stripLocalArtifactReferences(record.output) || record.waiting_input?.question || "请补充必要信息。";
      answer.waitingInput = record.waiting_input;
      answer.streaming = false;
      answer.status = "waiting_input";
      busy.value = false;
      await store.load();
      scrollChat();
      return;
    }
    pollTimer = window.setTimeout(() => poll(id, answer), 900);
  } catch (error) {
    answer.role = "error";
    answer.text = error.message;
    answer.streaming = false;
    busy.value = false;
    store.error = error.message;
    scrollChat();
  }
}
async function downloadArtifact(file) {
  try {
    await api.downloadRunFile(file);
  } catch (error) {
    store.error = error.message;
  }
}
function openReview(runId, pending, answer) {
  if (!answer) return;
  answer.approvals ||= [];
  const existing = answer.approvals.find(
    (item) =>
      item.runId === runId &&
      item.step_id === pending?.step_id &&
      item.status === "pending",
  );
  if (!existing)
    answer.approvals.push({
      ...(pending || {}),
      runId,
      feedback: "",
      status: "pending",
      busy: false,
    });
  answer.text = "";
  answer.status = "waiting_for_feedback";
  answer.streaming = false;
  busy.value = false;
  scrollChat();
}
async function submitReview(answer, approval, outcome) {
  if (!approval?.runId || approval.busy || approval.status !== "pending") return;
  approval.busy = true;
  try {
    await api.submitRunFeedback(approval.runId, outcome, approval.feedback);
    approval.status = "submitted";
    approval.outcome = outcome;
    answer.text = "";
    answer.status = "queued";
    answer.streaming = true;
    busy.value = true;
    await streamRun(approval.runId, answer);
  } catch (error) {
    store.error = error.message;
  } finally {
    approval.busy = false;
  }
}
async function streamRun(id, answer) {
  const controller = new AbortController();
  runAbortController?.abort();
  runAbortController = controller;
  let terminal = "";
  try {
    await api.runEvents(
      id,
      (frame) => {
        terminal = applyRunFrame(answer, frame) || terminal;
        if (frame.type === "waiting_for_feedback")
          openReview(id, frame.pending_feedback || {}, answer);
        if (
          frame.type === "run.waiting_input" &&
          (["file", "image"].includes(frame.waiting_input?.input_type) ||
            frame.waiting_input?.accepts_files ||
            frame.waiting_input?.file_input_names?.length)
        )
          variablesOpen.value = true;
        scrollChat();
      },
      controller.signal,
      answer.eventCursor || 0,
    );
    if (!terminal) return poll(id, answer);
    busy.value = false;
    await store.load();
    await refreshConversations();
    scrollChat();
  } catch (error) {
    if (error.name !== "AbortError") await poll(id, answer);
  } finally {
    if (runAbortController === controller) runAbortController = null;
  }
}
async function sendMessage() {
  if (!canSend.value) return;
  try {
    const inputs = parsedInputs();
    const attachments = Object.fromEntries(
      Object.entries(files)
        .filter(([, items]) => items.length)
        .map(([key, items]) => [key, items.map((item) => item.id)]),
    );
    validateInputs(inputs, attachments);
    const text =
      currentMessage.value.trim() ||
      (selectedFiles.value.length ? "请处理我上传的内容。" : "运行智能体。");
    const submittedMessage = currentMessage.value.trim();
    const shownFiles = selectedFiles.value.map((item) => ({
      id: item.id,
      name: item.name,
    }));
    messages.value.push({ role: "user", text, attachments: shownFiles });
    const answer = reactive({
      role: "assistant",
      text: "",
      streaming: true,
      status: "queued",
      runId: "",
      steps: [],
    });
    messages.value.push(answer);
    busy.value = true;
    currentMessage.value = "";
    scrollChat();
    const record = await api.runWorkflow(
      workflow.value.id,
      inputs,
      attachments,
      {
        conversation_id: conversationId.value,
        // Keep the placeholder visible in the local transcript, but do not
        // send it as a user value while ask_user is waiting for another field.
        message: submittedMessage,
      },
    );
    Object.keys(files).forEach((key) => {
      files[key] = [];
    });
    answer.runId = record.id;
    await streamRun(record.id, answer);
  } catch (error) {
    busy.value = false;
    const answer = messages.value.at(-1);
    if (answer?.streaming) {
      answer.role = "error";
      answer.error = error.message;
      answer.status = "failed";
      answer.streaming = false;
    }
    store.error = error.message;
  }
}
</script>

<template>
  <div v-if="loading" class="run-app-loading">
    <LoaderCircle class="spin" :size="22" /><span>正在打开智能体应用...</span>
  </div>
  <div
    v-else-if="workflow && workflow.status === 'published'"
    class="chat-app-page has-history"
    :class="{ 'variables-open': variablesOpen }"
  >
    <header class="chat-app-header">
      <div class="chat-app-leading">
        <button
          class="chat-app-back"
          title="返回智能体列表"
          aria-label="返回智能体列表"
          @click="router.push('/automations')"
        >
          <ArrowLeft :size="18" />
        </button>
        <span class="chat-app-icon"><Bot :size="19" /></span>
        <div class="chat-app-title">
          <strong>{{ workflow.name }}</strong
          ><small>{{ workflow.kind.toUpperCase() }} · 已发布</small>
        </div>
      </div>
      <div class="chat-app-actions">
        <div class="chat-app-tools">
          <button
            title="编辑智能体"
            aria-label="编辑智能体"
            @click="router.push(`/studio/${workflow.id}`)"
          >
            <SquarePen :size="16" />
          </button>
          <button
            class="chat-app-api"
            title="API 接入"
            @click="router.push(`/automations/${workflow.id}/develop`)"
          >
            <Code2 :size="16" /><span>API</span>
          </button>
          <button
            title="运行变量"
            aria-label="运行变量"
            :class="{ active: variablesOpen }"
            @click="variablesOpen = !variablesOpen"
          >
            <SlidersHorizontal :size="16" />
          </button>
        </div>
        <button class="button primary chat-new-conversation" @click="newConversation">
          <SquarePen :size="15" />新对话
        </button>
      </div>
    </header>

    <main class="chat-app-main">
      <aside class="chat-history">
        <header>
          <strong>历史对话</strong>
          <button class="icon-button" title="新建对话" aria-label="新建对话" @click="newConversation">
            <SquarePen :size="14" />
          </button>
        </header>
        <div v-if="conversations.length" class="chat-history-list">
          <article
            v-for="item in conversations"
            :key="item.id"
            class="chat-history-item"
            :class="{ active: item.id === conversationId }"
            @click="chooseConversation(item.id)"
          >
            <div>
              <strong>{{ item.title || "新对话" }}</strong>
              <small>{{ formatBeijingDateTime(item.updated_at) }}</small>
            </div>
            <button
              class="chat-history-delete"
              title="删除对话"
              aria-label="删除对话"
              @click.stop="deleteConversation(item)"
            ><Trash2 :size="13" /></button>
          </article>
        </div>
        <p v-else class="chat-history-empty">暂无历史对话</p>
      </aside>
      <section class="chat-surface">
        <div ref="chatThread" class="chat-thread">
          <div class="chat-thread-inner">
            <article
              v-for="(message, index) in messages"
              :key="index"
              class="chat-message"
              :class="[
                message.role,
                { welcome: index === 0 && message.role === 'assistant' },
              ]"
            >
              <span v-if="message.role !== 'user'" class="chat-avatar"
                ><Bot :size="16"
              /></span>
              <div class="chat-message-body">
                <div
                  v-if="message.attachments?.length"
                  class="chat-message-files"
                >
                  <span v-for="file in message.attachments" :key="file.id"
                    ><FileText :size="12" />{{ file.name }}</span
                  >
                </div>
                <div v-if="message.steps?.length" class="run-activity">
                  <header>
                    <GitBranch :size="13" /><strong>执行进度</strong
                    ><span
                      >{{
                        message.steps.filter(
                          (item) => item.status === "completed",
                        ).length
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
                              step.expanded
                                ? '收起智能体输出'
                                : '展开智能体输出'
                            "
                            :aria-label="
                              step.expanded
                                ? '收起智能体输出'
                                : '展开智能体输出'
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
                        <p
                          v-if="step.preview"
                          :class="{ expanded: step.expanded }"
                        >
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
                  @submit="submitReview(message, approval, $event)"
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
                    @click.prevent="downloadArtifact(file)"
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
                    message.routerOnly
                      ? "已回复"
                      : message.status === "completed"
                        ? "已完成"
                        : message.status
                  }}</span
                  ><button
                    v-if="!message.routerOnly"
                    @click="router.push(`/runs/${conversationId}`)"
                  >
                    查看 Trace <ExternalLink :size="11" />
                  </button>
                </div>
              </div>
            </article>
          </div>
        </div>

        <div class="chat-composer-wrap">
          <div v-if="inlineVariableInputs.length" class="chat-inline-variables">
            <div
              v-for="input in inlineVariableInputs"
              :key="input.name"
              class="chat-inline-field"
            >
              <label
                >{{ input.label }}<em v-if="input.required">必填</em></label
              >
              <textarea
                v-if="
                  input.input_type === 'long_text' ||
                  input.input_type === 'json'
                "
                v-model="values[input.name]"
                :placeholder="input.description || input.name"
              ></textarea>
              <input
                v-else-if="input.input_type === 'number'"
                v-model="values[input.name]"
                type="number"
              />
              <label
                v-else-if="input.input_type === 'boolean'"
                class="chat-variable-switch"
                ><input
                  v-model="values[input.name]"
                  type="checkbox"
                />启用</label
              >
              <input
                v-else
                v-model="values[input.name]"
                :placeholder="input.description || input.name"
              />
            </div>
          </div>
          <div class="chat-composer">
            <div
              v-if="uploading && uploadTarget === 'composer'"
              class="upload-status"
            >
              <div class="upload-status-head">
                <span>正在上传 {{ pendingUploadNames.join("、") }}</span
                ><b>{{ uploadProgress }}%</b>
              </div>
              <div class="upload-progress-track">
                <i :style="{ width: `${uploadProgress}%` }"></i>
              </div>
            </div>
            <div v-if="selectedFiles.length" class="chat-composer-files">
              <template v-for="input in fileInputs" :key="input.name"
                ><span v-for="(file, index) in files[input.name]" :key="file.id"
                  ><FileText :size="12" />{{ file.name
                  }}<small>{{ input.label }}</small
                  ><button title="移除" @click="removeFile(input.name, index)">
                    <X :size="11" /></button></span
              ></template>
            </div>
            <textarea
              v-model="currentMessage"
              :disabled="busy || uploading"
              :placeholder="primaryInput?.label || '输入消息或运行要求'"
              rows="1"
              @keydown.enter.exact.prevent="sendMessage"
            ></textarea>
            <div class="chat-composer-actions">
              <template v-if="composerFileField"
                ><input
                  ref="composerFileInput"
                  hidden
                  type="file"
                  multiple
                  :accept="
                    fileInputs.every((item) => item.input_type === 'image')
                      ? 'image/*'
                      : '*/*'
                  "
                  @change="chooseComposerFiles" /><button
                  class="icon-button"
                  :disabled="busy || uploading"
                  title="添加文件或图片"
                  @click="composerFileInput?.click()"
                >
                  <LoaderCircle
                    v-if="uploading && uploadTarget === 'composer'"
                    class="spin"
                    :size="16"
                  /><Paperclip v-else :size="16" /></button
              ></template>
              <button
                v-if="variableInputs.length"
                class="chat-variable-trigger"
                @click="variablesOpen = true"
              >
                <SlidersHorizontal :size="13" />查看全部字段
              </button>
              <span></span>
              <button
                class="chat-send"
                :disabled="!canSend"
                title="发送"
                @click="sendMessage"
              >
                <LoaderCircle v-if="busy" class="spin" :size="16" /><Send
                  v-else
                  :size="16"
                />
              </button>
            </div>
          </div>
          <small>AI 生成内容可能有误，请核对重要信息。</small>
        </div>
      </section>

      <aside class="chat-variables">
        <header>
          <div>
            <span class="eyebrow">RUN VARIABLES</span
            ><strong>本次对话变量</strong>
          </div>
          <button
            class="icon-button"
            title="关闭"
            @click="variablesOpen = false"
          >
            <X :size="15" />
          </button>
        </header>
        <p>
          这些值会随每条消息一起发送。文件和图片可在这里分别绑定到对应变量。
        </p>
        <div v-if="!variableInputs.length" class="chat-variable-empty">
          <CheckCircle2 :size="18" /><span>除消息外没有其他运行变量。</span>
        </div>
        <div
          v-for="input in variableInputs"
          :key="input.name"
          class="chat-variable-field"
        >
          <label>{{ input.label }}<em v-if="input.required">必填</em></label
          ><small>{{ input.description || input.name }}</small>
          <textarea
            v-if="
              input.input_type === 'long_text' || input.input_type === 'json'
            "
            v-model="values[input.name]"
            :placeholder="input.input_type === 'json' ? '{}' : '输入内容'"
          ></textarea>
          <input
            v-else-if="input.input_type === 'number'"
            v-model="values[input.name]"
            type="number"
          />
          <label
            v-else-if="input.input_type === 'boolean'"
            class="chat-variable-switch"
            ><input v-model="values[input.name]" type="checkbox" />启用</label
          >
          <template v-else-if="['file', 'image'].includes(input.input_type)">
            <label class="chat-variable-upload" :class="{ disabled: uploading }"
              ><LoaderCircle
                v-if="uploading && uploadTarget === input.name"
                class="spin"
                :size="16" /><Image
                v-else-if="input.input_type === 'image'"
                :size="16" /><Upload v-else :size="16" /><span>{{
                uploading && uploadTarget === input.name
                  ? "正在上传"
                  : `选择${input.input_type === "image" ? "图片" : "文件"}`
              }}</span
              ><input
                hidden
                type="file"
                :disabled="uploading"
                :multiple="input.multiple"
                :accept="input.input_type === 'image' ? 'image/*' : '*/*'"
                @change="chooseFiles(input, $event)"
            /></label>
            <div
              v-if="uploading && uploadTarget === input.name"
              class="upload-status"
            >
              <div class="upload-status-head">
                <span>{{ pendingUploadNames.join("、") }}</span
                ><b>{{ uploadProgress }}%</b>
              </div>
              <div class="upload-progress-track">
                <i :style="{ width: `${uploadProgress}%` }"></i>
              </div>
            </div>
            <div class="chat-variable-files">
              <span v-for="(file, index) in files[input.name]" :key="file.id"
                ><FileText :size="11" />{{ file.name
                }}<button @click="removeFile(input.name, index)">
                  <X :size="11" /></button
              ></span>
            </div>
          </template>
          <input v-else v-model="values[input.name]" type="text" />
        </div>
      </aside>
    </main>
  </div>
  <div v-if="!workflow" class="run-app-missing">
    <XCircle :size="22" />
    <h2>智能体不存在或尚未发布</h2>
    <button class="button" @click="router.push('/automations')">
      返回目录
    </button>
  </div>
</template>
