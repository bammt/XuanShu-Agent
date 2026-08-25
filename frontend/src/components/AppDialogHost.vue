<script setup>
import { nextTick, ref, watch } from "vue";
import { AlertTriangle, X } from "lucide-vue-next";
import { closeDialog, dialogState } from "../services/dialog";

const input = ref(null);
const closeButton = ref(null);

watch(
  () => dialogState.open,
  async (open) => {
    if (open && dialogState.kind === "prompt") {
      await nextTick();
      input.value?.focus();
    } else if (open) {
      await nextTick();
      closeButton.value?.focus();
    }
  },
);

function submitDialog() {
  if (
    dialogState.kind === "prompt" &&
    dialogState.inputValue.length < dialogState.inputMinLength
  ) return;
  closeDialog(true);
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="dialogState.open"
      class="modal-backdrop app-dialog-backdrop"
      @click.self="closeDialog(false)"
      @keydown.esc="closeDialog(false)"
    >
      <section
        class="modal app-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="app-dialog-title"
      >
        <header class="modal-header">
          <div class="app-dialog-title">
            <span :class="['app-dialog-icon', { danger: dialogState.danger }]"
              ><AlertTriangle :size="16"
            /></span>
            <h2 id="app-dialog-title">{{ dialogState.title }}</h2>
          </div>
          <button
            ref="closeButton"
            class="icon-button"
            type="button"
            title="关闭"
            aria-label="关闭"
            @click="closeDialog(false)"
          >
            <X :size="16" />
          </button>
        </header>
        <div class="modal-body app-dialog-body">
          <p>{{ dialogState.message }}</p>
          <label v-if="dialogState.kind === 'prompt'" class="field">
            <span>{{ dialogState.inputLabel }}</span>
            <input
              ref="input"
              v-model="dialogState.inputValue"
              :type="dialogState.inputType"
              :placeholder="dialogState.placeholder"
              @keyup.enter="submitDialog"
            />
            <small v-if="dialogState.inputMinLength">
              至少 {{ dialogState.inputMinLength }} 位
            </small>
          </label>
        </div>
        <footer class="modal-footer">
          <button class="button" type="button" @click="closeDialog(false)">
            {{ dialogState.cancelLabel }}
          </button>
          <button
            :class="['button', dialogState.danger ? 'danger' : 'primary']"
            type="button"
            :disabled="
              dialogState.kind === 'prompt' &&
              dialogState.inputValue.length < dialogState.inputMinLength
            "
            @click="submitDialog"
          >
            {{ dialogState.confirmLabel }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
