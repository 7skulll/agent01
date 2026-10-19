<script setup lang="ts">
import { AlertCircle } from 'lucide-vue-next'
import { ApiError } from '../api/client'
defineProps<{ error?: unknown }>()
</script>
<template>
  <div v-if="error" class="error-banner" role="alert">
    <AlertCircle :size="18" />
    <div>
      {{ error instanceof Error ? error.message : String(error)
      }}<small v-if="error instanceof ApiError"
        >{{ error.code
        }}<template v-if="error.requestId"> · 请求 ID：{{ error.requestId }}</template></small
      ><slot />
    </div>
  </div>
</template>
