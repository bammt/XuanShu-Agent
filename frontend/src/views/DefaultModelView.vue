<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import {
  Check,
  Cpu,
  Database,
  MessageSquareText,
  Plus,
  Save,
  Star,
} from "lucide-vue-next";
import { api } from "../services/api";
import { usePlatformStore } from "../stores/platform";

const store = usePlatformStore();
const router = useRouter();
const selectedChat = ref("");
const selectedEmbedding = ref("");
const saving = ref("");
const chatModels = computed(() => store.chatModels);
const embeddingModels = computed(() => store.embeddingModels);
const currentChat = computed(() => store.defaultModel);
const currentEmbedding = computed(() => store.defaultEmbeddingModel);
const providerLabels = {
  openai: "OpenAI",
  "openai-compatible": "OpenAI Compatible",
  anthropic: "Anthropic",
  google: "Google Gemini",
  azure: "Azure OpenAI",
  ollama: "Ollama",
  custom: "Custom",
};

function selectCurrent() {
  selectedChat.value = currentChat.value?.id || chatModels.value[0]?.id || "";
  selectedEmbedding.value =
    currentEmbedding.value?.id || embeddingModels.value[0]?.id || "";
}

onMounted(async () => {
  await store.loadModels();
  selectCurrent();
});
watch(
  () =>
    store.models
      .map((item) => `${item.id}:${item.model_type}:${item.is_default}`)
      .join("|"),
  selectCurrent,
);

async function saveDefault(modelType) {
  const modelId =
    modelType === "embedding" ? selectedEmbedding.value : selectedChat.value;
  if (!modelId || saving.value) return;
  saving.value = modelType;
  try {
    const saved = await api.setDefaultModel(modelId, modelType);
    await store.loadModels();
    store.notify(
      `${modelType === "embedding" ? "默认 Embedding 模型" : "默认对话模型"}已切换为“${saved.name}”`,
    );
  } catch (error) {
    store.error = error.message;
  } finally {
    saving.value = "";
  }
}
</script>

<template>
  <div class="page-heading default-model-heading">
    <div>
      <h2>默认模型</h2>
      <p>分别指定智能体对话与知识库向量化所使用的默认连接。</p>
    </div>
    <button class="button" @click="router.push('/models')">
      <Plus :size="14" />管理模型连接
    </button>
  </div>

  <div class="default-model-layout">
    <section class="panel default-model-panel">
      <header class="panel-header default-model-section-head">
        <span class="default-model-purpose chat"
          ><MessageSquareText :size="18"
        /></span>
        <div>
          <h3>默认对话模型</h3>
          <p>用于自然语言编排，以及未单独指定模型的 Agent。</p>
        </div>
        <span v-if="currentChat" class="status-badge published"
          ><Star :size="10" />已设置</span
        >
      </header>
      <div v-if="chatModels.length" class="default-model-list">
        <label
          v-for="item in chatModels"
          :key="item.id"
          class="default-model-option"
          :class="{ selected: selectedChat === item.id }"
        >
          <input
            v-model="selectedChat"
            type="radio"
            name="default-chat-model"
            :value="item.id"
          />
          <span class="provider-icon"><Cpu :size="17" /></span>
          <span class="default-model-copy"
            ><strong>{{ item.name }}</strong
            ><small>{{ providerLabels[item.provider] }} · {{ item.model }}</small
            ><small>{{ item.base_url || "Provider default endpoint" }}</small></span
          >
          <span class="default-model-check"
            ><Check v-if="selectedChat === item.id" :size="15"
          /></span>
        </label>
      </div>
      <div v-else class="default-model-empty">
        <MessageSquareText :size="20" />
        <div>
          <strong>还没有对话模型</strong>
          <p>添加 Chat 类型连接后，才能运行编排与智能体。</p>
        </div>
        <button class="button small" @click="router.push('/models')">
          添加连接
        </button>
      </div>
      <footer v-if="chatModels.length" class="default-model-footer">
        <span>只影响没有显式指定模型的编排和 Agent。</span>
        <button
          class="button primary"
          :disabled="saving || selectedChat === currentChat?.id"
          @click="saveDefault('chat')"
        >
          <Save :size="14" />{{
            saving === "chat" ? "保存中..." : "保存对话模型"
          }}
        </button>
      </footer>
    </section>

    <section class="panel default-model-panel">
      <header class="panel-header default-model-section-head">
        <span class="default-model-purpose embedding"
          ><Database :size="18"
        /></span>
        <div>
          <h3>默认 Embedding 模型</h3>
          <p>用于新建知识库时的文本向量化与语义检索。</p>
        </div>
        <span v-if="currentEmbedding" class="status-badge published"
          ><Star :size="10" />已设置</span
        >
      </header>
      <div v-if="embeddingModels.length" class="default-model-list">
        <label
          v-for="item in embeddingModels"
          :key="item.id"
          class="default-model-option"
          :class="{ selected: selectedEmbedding === item.id }"
        >
          <input
            v-model="selectedEmbedding"
            type="radio"
            name="default-embedding-model"
            :value="item.id"
          />
          <span class="provider-icon"><Cpu :size="17" /></span>
          <span class="default-model-copy"
            ><strong>{{ item.name }}</strong
            ><small>{{ providerLabels[item.provider] }} · {{ item.model }}</small
            ><small>{{ item.base_url || "Provider default endpoint" }}</small></span
          >
          <span class="default-model-check"
            ><Check v-if="selectedEmbedding === item.id" :size="15"
          /></span>
        </label>
      </div>
      <div v-else class="default-model-empty">
        <Database :size="20" />
        <div>
          <strong>还没有 Embedding 模型</strong>
          <p>添加 Embedding 类型连接后，知识库才能建立向量索引。</p>
        </div>
        <button class="button small" @click="router.push('/models')">
          添加连接
        </button>
      </div>
      <footer v-if="embeddingModels.length" class="default-model-footer">
        <span>新建知识库会优先选择此连接，已有知识库保持原配置。</span>
        <button
          class="button primary"
          :disabled="saving || selectedEmbedding === currentEmbedding?.id"
          @click="saveDefault('embedding')"
        >
          <Save :size="14" />{{
            saving === "embedding" ? "保存中..." : "保存 Embedding 模型"
          }}
        </button>
      </footer>
    </section>
  </div>
</template>
