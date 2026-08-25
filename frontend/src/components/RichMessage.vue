<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { authenticatedObjectUrl } from "../services/api";
import { renderRichMessage } from "../services/messageFormatting";

const props = defineProps({
  text: { type: String, default: "" },
  files: { type: Array, default: () => [] },
  authenticated: { type: Boolean, default: true },
});
const imageUrls = ref({});
const ownedUrls = new Set();
const html = computed(() => renderRichMessage(props.text));
const imageFiles = computed(() => props.files.filter((file) =>
  String(file.content_type || "").startsWith("image/") ||
  /\.(png|jpe?g|gif|webp|bmp|avif)$/i.test(String(file.name || "")),
));

async function loadImages() {
  for (const file of imageFiles.value) {
    const key = file.object_key || file.url || file.name;
    if (!key || imageUrls.value[key]) continue;
    try {
      const url = props.authenticated ? await authenticatedObjectUrl(file) : file.url;
      if (url) {
        imageUrls.value = { ...imageUrls.value, [key]: url };
        if (props.authenticated) ownedUrls.add(url);
      }
    } catch (_) {
      // A failed preview still leaves the normal download link available.
    }
  }
}
onMounted(loadImages);
watch(() => props.files, loadImages, { deep: true });
onBeforeUnmount(() => ownedUrls.forEach((url) => window.URL.revokeObjectURL(url)));
</script>

<template>
  <div class="rich-message">
    <div v-if="text" class="rich-message-copy" v-html="html"></div>
    <div v-if="imageFiles.length" class="rich-message-images">
      <a v-for="file in imageFiles" :key="file.object_key || file.url || file.name" :href="imageUrls[file.object_key || file.url || file.name] || undefined" target="_blank" rel="noopener noreferrer">
        <img v-if="imageUrls[file.object_key || file.url || file.name]" :src="imageUrls[file.object_key || file.url || file.name]" :alt="file.name" loading="lazy" />
      </a>
    </div>
  </div>
</template>
