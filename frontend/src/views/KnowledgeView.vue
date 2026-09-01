<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ArrowLeft,
  BookOpen,
  FileText,
  Search,
  LoaderCircle,
  Plus,
  Trash2,
  Upload,
  X,
} from "lucide-vue-next";
import { api } from "../services/api";
import { usePlatformStore } from "../stores/platform";
import EmptyState from "../components/EmptyState.vue";
import { confirmDialog } from "../services/dialog";

const store = usePlatformStore();
const route = useRoute();
const router = useRouter();
const items = ref([]);
const detail = ref(null);
const detailLoading = ref(false);
let detailTimer = null;
const modal = ref(false);
const saving = ref(false);
const uploading = ref(false);
const uploadProgress = ref(0);
const chunkViewer = ref(null);
const chunksLoading = ref(false);
const embeddingModels = computed(() =>
  store.models.filter((item) => item.model_type === "embedding"),
);
const blank = () => ({
  name: "",
  description: "",
  embedding_model_id:
    store.defaultEmbeddingModel?.id || embeddingModels.value[0]?.id || "",
  parsing_strategy: "auto",
  chunk_size: 800,
  chunk_overlap: 120,
  files: [],
});
const form = ref(blank());

async function load() {
  items.value = await api.knowledge();
  store.knowledge = items.value;
}
async function loadDetail() {
  if (!route.params.id) return;
  if (detailTimer) window.clearTimeout(detailTimer);
  detailTimer = null;
  detailLoading.value = true;
  try {
    detail.value = await api.knowledgeItem(route.params.id);
    store.knowledge = store.knowledge.map((item) =>
      String(item.id) === String(detail.value.id) ? detail.value : item,
    );
    if (detail.value.status === "processing" || detail.value.files.some((item) => ["queued", "processing"].includes(item.status))) {
      detailTimer = window.setTimeout(loadDetail, 1200);
    }
  } catch (error) {
    store.error = error.message;
  } finally {
    detailLoading.value = false;
  }
}
onMounted(async () => {
  await store.loadModels();
  if (route.params.id) await loadDetail();
  else {
    await load();
    if (route.query.create === "1") open();
  }
});
watch(
  () => route.params.id,
  async (id) => {
    if (detailTimer) window.clearTimeout(detailTimer);
    detailTimer = null;
    detail.value = null;
    if (id) await loadDetail();
    else await load();
  },
);
onBeforeUnmount(() => {
  if (detailTimer) window.clearTimeout(detailTimer);
});
function goDetail(item) {
  router.push(`/knowledge/${item.id}`);
}
function open() {
  form.value = blank();
  uploadProgress.value = 0;
  modal.value = true;
}
async function save() {
  if (!embeddingModels.value.length) {
    store.error = "请先在模型连接中添加 Embedding 类型模型";
    return;
  }
  saving.value = true;
  try {
    form.value = await api.saveKnowledge(form.value);
    modal.value = false;
    if (!form.value.id) return;
    await router.push({
      path: `/knowledge/${form.value.id}`,
      query: route.query.returnTo ? { returnTo: route.query.returnTo } : {},
    });
    store.notify("知识库已创建，可在详情页上传并解析文件");
  } catch (error) {
    await loadDetail();
    store.error = error.message;
  } finally {
    saving.value = false;
    uploading.value = false;
  }
}
async function uploadDetailFiles(event) {
  const selected = Array.from(event.target.files || []);
  event.target.value = "";
  if (!selected.length || !detail.value) return;
  uploading.value = true;
  uploadProgress.value = 0;
  try {
    await api.uploadKnowledgeFiles(detail.value.id, selected, (value) => {
      uploadProgress.value = value;
    });
    await loadDetail();
    store.notify("文件已加入解析队列");
  } catch (error) {
    await loadDetail();
    store.error = error.message;
  } finally {
    uploading.value = false;
    uploadProgress.value = 0;
  }
}
async function remove(item) {
  if (!(await confirmDialog({
    title: "删除知识库",
    message: `删除知识库“${item.name}”及其全部向量和文件？此操作无法撤销。`,
    confirmLabel: "删除知识库",
    danger: true,
  }))) return;
  try {
    await api.deleteKnowledge(item.id);
    await load();
  } catch (error) {
    store.error = error.message;
  }
}
async function removeFile(file) {
  if (!detail.value) return;
  if (!(await confirmDialog({
    title: "删除知识文件",
    message: `删除文件“${file.name}”及其已建立的向量？此操作无法撤销。`,
    confirmLabel: "删除文件",
    danger: true,
  }))) return;
  try {
    await api.deleteKnowledgeFile(detail.value.id, file.id);
    await loadDetail();
  } catch (error) {
    store.error = error.message;
  }
}
async function viewChunks(file) {
  if (!detail.value || file.status !== "ready") return;
  chunksLoading.value = true;
  chunkViewer.value = { name: file.name, chunks: [] };
  try {
    chunkViewer.value = await api.knowledgeFileChunks(detail.value.id, file.id);
  } catch (error) {
    chunkViewer.value = null;
    store.error = error.message;
  } finally {
    chunksLoading.value = false;
  }
}
function size(value) {
  return value < 1024 * 1024
    ? `${Math.ceil(value / 1024)} KB`
    : `${(value / 1024 / 1024).toFixed(1)} MB`;
}
</script>

<template>
  <template v-if="route.params.id">
    <div class="page-heading">
      <div><button class="text-button knowledge-back-button" @click="router.push('/knowledge')"><ArrowLeft :size="15" />返回知识库</button><h2>{{ detail?.name || '知识库详情' }}</h2><p>{{ detail?.description || '管理文件、解析状态和向量索引。' }}</p></div>
      <div class="heading-actions"><button v-if="route.query.returnTo" class="button" @click="router.push(String(route.query.returnTo))">返回编排</button><label class="button primary"><Upload :size="15" />上传文件<input type="file" hidden multiple accept=".pdf,.docx,.txt,.md,.csv,.json,.xml,.yaml,.yml" @change="uploadDetailFiles" /></label></div>
    </div>
    <section v-if="detail" class="knowledge-detail-grid">
      <article class="resource-card knowledge-detail-summary"><span class="eyebrow">KNOWLEDGE BASE</span><h3>{{ detail.name }}</h3><p>{{ detail.description || '工作空间知识库' }}</p><div class="resource-meta"><span class="tag">{{ detail.files.length }} 个文件</span><span class="tag">{{ detail.files.reduce((sum, file) => sum + file.chunk_count, 0) }} 个片段</span><span class="tag">{{ detail.status }}</span></div><small>解析策略：{{ detail.parsing_strategy }} · 分片 {{ detail.chunk_size }} / {{ detail.chunk_overlap }}</small></article>
      <section class="knowledge-files knowledge-detail-files"><header><strong>文件与解析状态</strong><span v-if="uploading">上传中 {{ uploadProgress }}%</span></header><article v-for="file in detail.files" :key="file.id" :class="{ 'is-viewable': file.status === 'ready' }" @click="viewChunks(file)"><FileText :size="15" /><div><strong>{{ file.name }}</strong><small>{{ size(file.size) }} · {{ file.chunk_count }} 个片段 · {{ file.status }}</small><p v-if="file.error" class="form-error">{{ file.error }}</p></div><button v-if="file.status === 'ready'" class="icon-button" title="查看分片" @click.stop="viewChunks(file)"><Search :size="13" /></button><button class="icon-button" title="删除文件" @click.stop="removeFile(file)"><Trash2 :size="13" /></button></article><EmptyState v-if="!detail.files.length" title="还没有文件" detail="上传文件后，worker 会在后台解析并建立向量索引。" /></section>
    </section>
    <div v-else-if="detailLoading" class="loading-state"><LoaderCircle class="spin" :size="20" />正在加载知识库…</div>
  </template>
  <template v-else>
  <div class="page-heading">
    <div>
      <h2>知识库</h2>
      <p>管理工作空间私有资料、解析策略和向量索引，并绑定到 CrewAI Agent。</p>
    </div>
    <button class="button primary" @click="open()">
      <Plus :size="15" />新建知识库
    </button>
  </div>
  <div
    v-if="!embeddingModels.length"
    class="model-required knowledge-model-required"
  >
    <span><BookOpen :size="18" /></span><strong>需要 Embedding 模型</strong>
    <p>知识库解析必须使用 Embedding 类型模型。请先在模型连接中添加。</p>
    <button class="button accent" @click="$router.push('/models')">
      添加 Embedding 模型
    </button>
  </div>
  <section v-if="items.length" class="resource-grid">
    <article
      v-for="item in items"
      :key="item.id"
      class="resource-card is-editable"
      @click="goDetail(item)"
    >
      <div class="resource-card-top">
        <span class="resource-icon"><BookOpen :size="17" /></span
        ><button
          class="icon-button"
          title="删除知识库"
          @click.stop="remove(item)"
        >
          <Trash2 :size="14" />
        </button>
      </div>
      <h3>{{ item.name }}</h3>
      <p>{{ item.description || "工作空间知识库" }}</p>
      <div class="resource-meta">
        <span class="tag">{{ item.files.length }} 个文件</span
        ><span class="tag"
          >{{
            item.files.reduce((sum, file) => sum + file.chunk_count, 0)
          }}
          个片段</span
        ><span class="tag">{{ item.status }}</span>
      </div>
    </article>
  </section>
  <EmptyState
    v-else
    title="还没有知识库"
    detail="创建工作空间知识库，选择 Embedding 模型并上传资料。"
    ><button class="button accent" @click="open()">
      新建知识库
    </button></EmptyState
  >

  <div
    v-if="modal"
    class="modal-backdrop"
    @click.self="!saving && (modal = false)"
  >
    <section class="modal large">
      <header class="modal-header">
        <div>
          <span class="eyebrow">WORKSPACE KNOWLEDGE</span>
          <h2>新建知识库</h2>
        </div>
        <button class="icon-button" :disabled="saving" @click="modal = false">
          <X :size="16" />
        </button>
      </header>
      <div class="modal-body">
        <div class="form-grid">
          <div class="field">
            <label>名称</label
            ><input v-model="form.name" placeholder="产品文档知识库" />
          </div>
          <div class="field">
            <label>Embedding 模型</label
            ><select
              v-model="form.embedding_model_id"
              :disabled="form.files.length > 0"
            >
              <option value="">请选择</option>
              <option
                v-for="model in embeddingModels"
                :key="model.id"
                :value="model.id"
              >
                {{ model.name }} · {{ model.model }}
              </option>
            </select>
          </div>
          <div class="field full">
            <label>说明</label
            ><textarea
              v-model="form.description"
              placeholder="说明资料范围和适用问答场景"
            ></textarea>
          </div>
          <div class="field">
            <label>解析方式</label
            ><select
              v-model="form.parsing_strategy"
              :disabled="form.files.length > 0"
            >
              <option value="auto">自动识别</option>
              <option value="plain">纯文本提取</option>
            </select>
          </div>
          <div class="field">
            <label>分片大小</label
            ><input
              v-model.number="form.chunk_size"
              type="number"
              min="200"
              max="8000"
              :disabled="form.files.length > 0"
            />
          </div>
          <div class="field">
            <label>分片重叠</label
            ><input
              v-model.number="form.chunk_overlap"
              type="number"
              min="0"
              :disabled="form.files.length > 0"
            />
          </div>
          <p v-if="form.files.length" class="field full form-hint">
            已有文件的 Embedding
            与分片参数已锁定。变更参数时请新建知识库并重新上传。
          </p>
        </div>
        <p class="form-hint">创建完成后会直接进入详情页，文件上传、删除和解析状态均在详情页管理。</p>
      </div>
      <footer class="modal-footer">
        <button class="button" :disabled="saving" @click="modal = false">
          取消</button
        ><button
          class="button primary"
          :disabled="saving || !form.name || !form.embedding_model_id"
          @click="save"
        >
          <LoaderCircle v-if="saving" class="spin" :size="14" />{{
            saving ? "正在创建…" : "创建并进入详情"
          }}
        </button>
      </footer>
    </section>
  </div>
  </template>
  <div v-if="chunkViewer" class="modal-backdrop" @click.self="chunkViewer = null">
    <section class="modal large knowledge-chunk-modal">
      <header class="modal-header"><div><span class="eyebrow">PARSED CHUNKS</span><h2>{{ chunkViewer.name }}</h2></div><button class="icon-button" title="关闭" @click="chunkViewer = null"><X :size="16" /></button></header>
      <div class="modal-body knowledge-chunk-list">
        <div v-if="chunksLoading" class="loading-state"><LoaderCircle class="spin" :size="18" />正在读取分片…</div>
        <article v-for="chunk in chunkViewer.chunks" v-else :key="chunk.index"><header><strong>分片 {{ chunk.index }}</strong><span>{{ chunk.characters }} 字符</span></header><pre>{{ chunk.content }}</pre></article>
      </div>
    </section>
  </div>
</template>
