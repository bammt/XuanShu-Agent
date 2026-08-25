<script setup>
import { Check, CirclePause, LoaderCircle } from "lucide-vue-next";

defineProps({
  approval: { type: Object, required: true },
  busy: { type: Boolean, default: false },
});
defineEmits(["submit"]);

function outcomeLabel(value) {
  return (
    {
      approved: "通过并继续",
      revise: "退回修改",
      rejected: "拒绝",
      needs_revision: "要求修改",
    }[value] || value
  );
}
</script>

<template>
  <section class="run-approval-card" :class="approval.status || 'pending'">
    <header>
      <span><CirclePause :size="15" /></span>
      <div>
        <small>人工审批</small>
        <strong>{{ approval.step_name || "请审核当前结果" }}</strong>
      </div>
      <em>{{ approval.status === "submitted" ? "已提交" : "等待处理" }}</em>
    </header>
    <p>{{ approval.message || "请审核当前步骤输出，并选择下一步。" }}</p>
    <pre v-if="approval.output">{{ approval.output }}</pre>
    <template v-if="approval.status !== 'submitted'">
      <label>
        <span>审核意见（可选）</span>
        <textarea
          v-model="approval.feedback"
          placeholder="说明通过原因，或给出需要修改的内容"
        ></textarea>
      </label>
      <footer>
        <button
          v-for="outcome in approval.outcomes || []"
          :key="outcome"
          class="button"
          :class="{ primary: outcome === 'approved' }"
          :disabled="busy"
          @click="$emit('submit', outcome)"
        >
          <LoaderCircle v-if="busy" class="spin" :size="13" />
          <Check v-else :size="13" />{{ outcomeLabel(outcome) }}
        </button>
      </footer>
    </template>
    <div v-else class="run-approval-result">
      <Check :size="13" />{{ outcomeLabel(approval.outcome) }}
      <span v-if="approval.feedback">{{ approval.feedback }}</span>
    </div>
  </section>
</template>
