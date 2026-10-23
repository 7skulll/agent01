<script setup lang="ts">
import Tag from 'primevue/tag'
import { statusLabels } from '../api/types'
defineProps<{ status: string }>()
const severityByStatus: Record<string, 'success' | 'info' | 'warn' | 'danger' | 'secondary'> = {
  indexed: 'success',
  parsing: 'info',
  indexing: 'info',
  recorded: 'warn',
  not_indexed: 'secondary',
  failed: 'danger',
}
</script>
<template>
  <Tag
    :severity="severityByStatus[status] || 'secondary'"
    :value="statusLabels[status] || status"
  />
</template>
