<script setup>
import { computed, nextTick, ref, watch } from "vue";
import { GitBranch, UsersRound, X } from "lucide-vue-next";

const props = defineProps({
  open: { type: Boolean, default: false },
  mode: { type: String, default: "create" },
  kind: { type: String, default: "crew" },
  name: { type: String, default: "" },
  description: { type: String, default: "" },
});
const emit = defineEmits(["cancel", "confirm"]);

const nameInput = ref(null);
const draftName = ref("");
const draftDescription = ref("");
const kindLabel = computed(() => (props.kind === "flow" ? "Flow" : "Crew"));
const canSubmit = computed(
  () => Boolean(draftName.value.trim()) && Boolean(draftDescription.value.trim()),
);

watch(
  () => props.open,
  async (open) => {
    if (!open) return;
    draftName.value = props.name || "";
    draftDescription.value = props.description || "";
    await nextTick();
    nameInput.value?.focus();
  },
  { immediate: true },
);

function submit() {
  if (!canSubmit.value) return;
  emit("confirm", {
    name: draftName.value.trim(),
    description: draftDescription.value.trim(),
  });
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="modal-backdrop automation-details-backdrop"
      @click.self="emit('cancel')"
      @keydown.esc.prevent="emit('cancel')"
    >
      <form
        class="modal automation-details-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="automation-details-title"
        @submit.prevent="submit"
      >
        <header class="modal-header">
          <div class="automation-details-heading">
            <span class="automation-details-icon">
              <GitBranch v-if="kind === 'flow'" :size="17" />
              <UsersRound v-else :size="17" />
            </span>
            <div>
              <small>{{ kindLabel }}</small>
              <h2 id="automation-details-title">
                {{ mode === "edit" ? "编辑智能体信息" : `创建 ${kindLabel}` }}
              </h2>
            </div>
          </div>
          <button
            class="icon-button"
            type="button"
            title="关闭"
            aria-label="关闭"
            @click="emit('cancel')"
          >
            <X :size="16" />
          </button>
        </header>
        <div class="modal-body automation-details-body">
          <p>名称和介绍会显示在智能体列表及运行入口中。</p>
          <label class="field">
            <span>名称</span>
            <input
              ref="nameInput"
              v-model="draftName"
              maxlength="160"
              placeholder="例如：运维知识问答"
              autocomplete="off"
            />
          </label>
          <label class="field">
            <span>介绍</span>
            <textarea
              v-model="draftDescription"
              maxlength="1000"
              placeholder="说明这个智能体解决什么问题，以及适合谁使用"
            ></textarea>
          </label>
          <small v-if="!canSubmit" class="automation-details-hint">
            名称和介绍均为必填项
          </small>
        </div>
        <footer class="modal-footer">
          <button class="button" type="button" @click="emit('cancel')">取消</button>
          <button class="button primary" type="submit" :disabled="!canSubmit">
            {{ mode === "edit" ? "保存修改" : "进入编排" }}
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<style scoped>
.automation-details-backdrop {
  z-index: 88;
  background: rgba(22, 28, 24, 0.46);
  backdrop-filter: blur(2px);
}
.automation-details-dialog {
  width: min(520px, calc(100vw - 32px));
}
.automation-details-heading {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}
.automation-details-heading > div {
  min-width: 0;
}
.automation-details-heading small {
  display: block;
  margin-bottom: 2px;
  color: var(--subtle);
  font-size: 9px;
  font-weight: 750;
  text-transform: uppercase;
}
.automation-details-heading h2 {
  margin: 0;
}
.automation-details-icon {
  width: 32px;
  height: 32px;
  display: grid;
  flex: 0 0 32px;
  place-items: center;
  border-radius: 6px;
  color: #53685a;
  background: #edf2ed;
}
.automation-details-body {
  display: grid;
  gap: 15px;
}
.automation-details-body > p {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.65;
}
.automation-details-body .field > span {
  color: #4f5752;
  font-size: 10px;
  font-weight: 750;
}
.automation-details-body textarea {
  min-height: 110px;
}
.automation-details-hint {
  color: #9c4d42;
  font-size: 10px;
}
</style>
