<script setup lang="ts">
import { ref, watch } from 'vue'
const props = defineProps<{ open: boolean; title: string; busy?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement>()
watch(
  [() => props.open, dialog],
  () => {
    if (!dialog.value) return
    if (props.open && !dialog.value.open) dialog.value.showModal()
    if (!props.open && dialog.value.open) dialog.value.close()
  },
  { flush: 'post' },
)
</script>
<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="management-dialog card"
      :aria-label="title"
      @cancel.prevent="!busy && emit('close')"
    >
      <h2>{{ title }}</h2>
      <slot />
    </dialog>
  </Teleport>
</template>
