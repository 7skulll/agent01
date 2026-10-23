<script setup lang="ts">
import Dialog from 'primevue/dialog'
const props = defineProps<{ open: boolean; title: string; busy?: boolean }>()
const emit = defineEmits<{ close: [] }>()
</script>
<template>
  <Dialog
    :visible="open"
    :header="title"
    modal
    :dismissable-mask="!busy"
    :close-on-escape="!busy"
    :closable="!busy"
    :show-close-icon="!busy"
    :block-scroll="true"
    position="center"
    class="zx-dialog"
    :pt="{ root: { 'aria-label': title } }"
    @update:visible="!busy && emit('close')"
    @hide="!busy && emit('close')"
  >
    <slot />
  </Dialog>
</template>
