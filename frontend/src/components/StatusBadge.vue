<script setup lang="ts">
import { statusLabels } from '../api/types'
defineProps<{ status: string }>()
</script>
<template>
  <span class="badge" :class="status"
    ><span class="dot"></span>{{ statusLabels[status] || status }}</span
  >
</template>
