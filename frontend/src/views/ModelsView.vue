<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Eye,
  EyeOff,
  KeyRound,
  Link2,
  Plus,
  Server,
  Settings2,
  Star,
  TestTube2,
  Trash2,
  X,
} from "lucide-vue-next";
import { api } from "../services/api";
import { usePlatformStore } from "../stores/platform";
import EmptyState from "../components/EmptyState.vue";
import { confirmDialog } from "../services/dialog";

const store = usePlatformStore();
const router = useRouter();
const canEdit = computed(() => store.canEdit);
const modal = ref(false);
const showKey = ref(false);
const saving = ref(false);
const testing = ref("");
const testResults = ref({});
const formError = ref("");
const providerLabels = {
  openai: "OpenAI",
  "openai-compatible": "OpenAI Compatible",
  anthropic: "Anthropic",
  google: "Google Gemini",
  azure: "Azure OpenAI",
  ollama: "Ollama",
  custom: "Custom",
};
const blank = () => ({
  name: "",
  provider: "openai",
  model: "openai/gpt-4o-mini",
  base_url: "",
  api_key: "",
  model_type: "chat",
  temperature: null,
  max_tokens: null,
  timeout: 120,
  max_retries: 4,
  thinking_mode: "auto",
  thinking_effort: "medium",
  is_default: false,
});
const form = ref(blank());

onMounted(() => store.loadModels());

function open(item) {
  if (!canEdit.value) return;
  form.value = item
    ? {
        ...blank(),
        ...item,
        api_key: "",
        thinking_mode: item.thinking_mode || "auto",
        thinking_effort: item.thinking_effort || "medium",
      }
    : blank();
  modal.value = true;
  showKey.value = false;
  formError.value = "";
}

function validate() {
  form.value.name = form.value.name.trim();
  form.value.model = form.value.model.trim();
  form.value.base_url = form.value.base_url.trim();
  if (!form.value.name) return "请填写连接名称。";
  if (!form.value.model) return "请填写 CrewAI 使用的 Model ID。";
  if (form.value.provider === "openai-compatible" && !form.value.base_url)
    return "OpenAI Compatible 连接必须填写 Base URL。";
  return "";
}

async function save() {
  if (!canEdit.value) {
    formError.value = "当前工作空间没有编辑权限。";
    return null;
  }
  formError.value = validate();
  if (formError.value) return null;
  saving.value = true;
  try {
    const payload = {
      ...form.value,
      temperature:
        form.value.temperature === "" ? null : form.value.temperature,
      max_tokens: form.value.max_tokens === "" ? null : form.value.max_tokens,
      timeout:
        form.value.timeout === "" || form.value.timeout == null
          ? 120
          : form.value.timeout,
      max_retries:
        form.value.max_retries === "" || form.value.max_retries == null
          ? 4
          : form.value.max_retries,
      thinking_mode:
        form.value.model_type === "chat" ? form.value.thinking_mode || "auto" : "auto",
      thinking_effort:
        form.value.model_type === "chat" && form.value.thinking_mode === "enabled"
          ? form.value.thinking_effort || "medium"
          : null,
    };
    const saved = await api.saveModel(payload);
    await store.loadModels();
    if (!store.models.some((item) => item.id === saved.id))
      throw new Error("模型已提交，但重新读取时未找到该连接。");
    modal.value = false;
    store.notify(`模型连接“${saved.name}”已保存`);
    return saved;
  } catch (error) {
    formError.value = error.message;
    store.error = error.message;
    return null;
  } finally {
    saving.value = false;
  }
}

async function remove(item) {
  if (!canEdit.value) return;
  if (!(await confirmDialog({
    title: "删除模型连接",
    message: `确认删除模型连接“${item.name}”？引用该连接的智能体可能无法运行。`,
    confirmLabel: "删除连接",
    danger: true,
  }))) return;
  await api.deleteModel(item.id);
  await store.loadModels();
}

async function test(item) {
  testing.value = item.id;
  testResults.value = { ...testResults.value, [item.id]: { pending: true } };
  try {
    const result = await api.testModel(item.id);
    testResults.value = { ...testResults.value, [item.id]: result };
    if (result.ok) store.notify(`连接成功 · ${result.latency_ms} ms`);
    else store.error = result.message;
  } catch (error) {
    testResults.value = {
      ...testResults.value,
      [item.id]: { ok: false, message: error.message },
    };
    store.error = error.message;
  } finally {
    testing.value = "";
  }
}

function applySuggestion(item, result) {
  open(item);
  form.value.base_url = result.suggested_base_url;
  store.notify("已应用检测到的兼容端点，请保存后重新测试");
}

async function saveAndTest() {
  const item = await save();
  if (item) await test(item);
}
</script>

<template>
  <div class="page-heading">
    <div>
      <h2>模型连接</h2>
      <p>管理对话模型与知识库解析所需的 Embedding 模型。</p>
    </div>
    <div class="heading-actions">
      <button v-if="canEdit" class="button" @click="router.push('/model-default')">
        <Settings2 :size="14" />默认模型设置
      </button>
      <button class="button primary" :disabled="!canEdit" @click="open()">
        <Plus :size="15" />添加模型
      </button>
    </div>
  </div>

  <section v-if="store.models.length" class="model-list panel">
    <div class="model-list-head">
      <span>连接</span><span>Provider / Model</span
      ><span>Endpoint / Credential</span><span>状态</span><span></span>
    </div>
    <article v-for="item in store.models" :key="item.id" class="model-list-row">
      <span class="provider-icon"><Cpu :size="17" /></span>
      <div class="model-list-name">
        <strong>{{ item.name }}</strong
        ><small
          >{{ item.model_type === "embedding" ? "Embedding" : "对话" }} ·
          {{ providerLabels[item.provider] }} · {{ item.model }}</small
        >
      </div>
      <div class="model-list-endpoint">
        <span
          ><Link2 :size="12" />{{
            item.base_url || "Provider default endpoint"
          }}</span
        ><span
          ><KeyRound :size="12" />{{
            item.has_api_key ? item.key_hint : "No API key"
          }}</span
        >
      </div>
      <div class="model-list-status">
        <span v-if="item.is_default" class="status-badge published"
          ><Star :size="10" />DEFAULT</span
        ><span
          v-if="testResults[item.id] && !testResults[item.id].pending"
          class="connection-inline"
          :class="testResults[item.id].ok ? 'ok' : 'failed'"
          ><CheckCircle2
            v-if="testResults[item.id].ok"
            :size="12"
          /><AlertTriangle v-else :size="12" />{{
            testResults[item.id].ok
              ? `${testResults[item.id].latency_ms} ms`
              : "失败"
          }}</span
        >
      </div>
      <div class="model-list-actions">
        <button class="button small" :disabled="!canEdit" @click="open(item)">编辑</button
        ><button
          class="button small"
          :disabled="testing === item.id"
          @click="test(item)"
        >
          <TestTube2 :size="13" />{{
            testing === item.id ? "Testing…" : "测试"
          }}</button
        ><button class="icon-button" :disabled="!canEdit" title="删除" @click="remove(item)">
          <Trash2 :size="14" />
        </button>
      </div>
      <div
        v-if="
          testResults[item.id] &&
          !testResults[item.id].pending &&
          !testResults[item.id].ok
        "
        class="model-list-error"
      >
        <span>{{ testResults[item.id].message }}</span
        ><button
          v-if="testResults[item.id].suggested_base_url"
          class="button small"
          :disabled="!canEdit"
          @click="applySuggestion(item, testResults[item.id])"
        >
          应用建议端点
        </button>
      </div>
    </article>
  </section>
  <EmptyState
    v-else
    title="尚未配置模型"
    detail="必须至少添加一个模型连接，才能使用自然语言开发和运行工作流。"
    ><button class="button accent" :disabled="!canEdit" @click="open()">
      添加模型连接
    </button></EmptyState
  >

  <div v-if="modal" class="modal-backdrop" @click.self="modal = false">
    <section class="modal large">
      <header class="modal-header">
        <div>
          <span class="eyebrow">LLM CONNECTION</span>
          <h2>{{ form.id ? "编辑模型连接" : "添加模型连接" }}</h2>
        </div>
        <button class="icon-button" @click="modal = false">
          <X :size="16" />
        </button>
      </header>
      <div class="modal-body">
        <div class="form-grid">
          <div class="field">
            <label>连接名称 *</label
            ><input
              v-model="form.name"
              placeholder="Production gateway"
              @input="formError = ''"
            />
          </div>
          <div class="field">
            <label>模型类型</label
            ><select
              v-model="form.model_type"
            >
              <option value="chat">对话模型（chat）</option>
              <option value="embedding">
                Embedding 模型（embedding）
              </option></select
            ><small>Embedding 模型仅用于知识库向量化和检索。</small>
          </div>
          <div class="field">
            <label>Provider</label
            ><select
              v-model="form.provider"
              @change="formError = ''"
            >
              <option
                v-for="(label, key) in providerLabels"
                :key="key"
                :value="key"
              >
                {{ label }}
              </option>
            </select>
          </div>
          <div class="field full">
            <label>Model ID *</label
            ><input
              v-model="form.model"
              placeholder="openai/gpt-4o-mini"
              @input="formError = ''"
            /><small
              >使用 CrewAI provider/model-id 格式；OpenAI-compatible
              网关也可以填写网关支持的模型 ID。</small
            >
          </div>
          <div class="field full">
            <label>Base URL</label>
            <div class="input-with-icons">
              <Server :size="14" /><input
                v-model="form.base_url"
                placeholder="https://api.example.com/v1"
                @input="formError = ''"
              />
            </div>
            <small
              >OpenAI Compatible 必填；其他 Provider 留空时使用默认地址。</small
            >
          </div>
          <div class="field full">
            <label>API Key</label>
            <div class="input-with-icons">
              <KeyRound :size="14" /><input
                v-model="form.api_key"
                :type="showKey ? 'text' : 'password'"
                :placeholder="form.has_api_key ? '留空以保留现有 Key' : 'sk-…'"
              /><button
                class="icon-button ghost-field-button"
                type="button"
                title="显示或隐藏 Key"
                @click="showKey = !showKey"
              >
                <EyeOff v-if="showKey" :size="14" /><Eye v-else :size="14" />
              </button>
            </div>
            <small
              >Key 加密写入
              PostgreSQL；读取接口只返回脱敏提示，不返回原值。</small
            >
          </div>
          <div class="field">
            <label>Temperature</label
            ><input
              v-model.number="form.temperature"
              type="number"
              min="0"
              max="2"
              step="0.1"
              placeholder="Provider default"
            />
          </div>
          <div class="field">
            <label>Max tokens</label
            ><input
              v-model.number="form.max_tokens"
              type="number"
              min="1"
              placeholder="Provider default"
            />
          </div>
          <div v-if="form.model_type === 'chat'" class="field full">
            <label>思考模式</label>
            <div class="segmented">
              <button
                type="button"
                :class="{ active: form.thinking_mode === 'auto' }"
                @click="form.thinking_mode = 'auto'"
              >跟随模型</button>
              <button
                type="button"
                :class="{ active: form.thinking_mode === 'enabled' }"
                @click="form.thinking_mode = 'enabled'"
              >开启</button>
              <button
                type="button"
                :class="{ active: form.thinking_mode === 'disabled' }"
                @click="form.thinking_mode = 'disabled'"
              >关闭</button>
            </div>
            <small>明确开启或关闭时传递供应商官方参数；兼容网关同时传递 extra_body。选择跟随模型则不覆盖供应商默认值。</small>
          </div>
          <div
            v-if="form.model_type === 'chat' && form.thinking_mode === 'enabled'"
            class="field"
          >
            <label>思考强度</label>
            <select v-model="form.thinking_effort">
              <option value="minimal">极低（minimal）</option>
              <option value="low">低（low）</option>
              <option value="medium">中（medium）</option>
              <option value="high">高（high）</option>
              <option value="max">最高（max）</option>
            </select>
          </div>
          <div class="field">
            <label>请求超时秒数（timeout）</label
            ><input
              v-model.number="form.timeout"
              type="number"
              min="1"
              max="1800"
            /><small>单次模型请求的最长等待时间。</small>
          </div>
          <div class="field">
            <label>网络重试次数（max_retries）</label
            ><input
              v-model.number="form.max_retries"
              type="number"
              min="0"
              max="10"
            /><small
              >连接失败、限流或网关 5xx 时由 CrewAI
              模型层指数退避重试，不会重新执行已完成任务。</small
            >
          </div>
        </div>
        <div v-if="formError" class="form-error">{{ formError }}</div>
      </div>
      <footer class="modal-footer">
        <button class="button" :disabled="saving" @click="modal = false">
          取消</button
        ><button class="button" :disabled="saving" @click="saveAndTest">
          <TestTube2 :size="14" />保存并测试</button
        ><button class="button primary" :disabled="saving" @click="save">
          {{ saving ? "保存中…" : "保存连接" }}
        </button>
      </footer>
    </section>
  </div>
</template>
