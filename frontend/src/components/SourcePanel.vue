<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { X, FileText, Quote } from 'lucide-vue-next'
import type { Source } from '../api/types'
const props = defineProps<{ source: Source | null }>()
const panel = ref<HTMLElement>()
let previous: HTMLElement | null = null
watch(
  () => props.source,
  async (value) => {
    if (value) {
      previous = document.activeElement as HTMLElement
      await nextTick()
      panel.value?.focus()
    } else previous?.focus()
  },
)
defineEmits<{ close: [] }>()
</script>
<template>
  <div v-if="source" class="source-backdrop" @click.self="$emit('close')">
    <aside
      ref="panel"
      class="source-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-title"
      tabindex="-1"
      @keydown.esc="$emit('close')"
      @keydown.tab.prevent="panel?.querySelector<HTMLButtonElement>('button')?.focus()"
    >
      <div class="panel-header">
        <span class="eyebrow"><Quote :size="16" /> 回答来源 · {{ source.citation_id }}</span
        ><button class="icon-button" aria-label="关闭来源" autofocus @click="$emit('close')">
          <X :size="20" />
        </button>
      </div>
      <h2 id="source-title">{{ source.name }}</h2>
      <div class="source-location">
        <FileText :size="14" />{{ source.heading || '正文片段'
        }}<span v-if="source.page"> · 第 {{ source.page }} 页</span>
      </div>
      <span v-if="source.truncated" class="badge">按问题与上下文预算抽取原文</span>
      <pre class="source-content">{{ source.content }}</pre>
      <p v-if="source.evidence_chunks?.length" class="muted small">
        本段包含 {{ source.evidence_chunks.length }} 个原文片段，按原文顺序呈现。
        <template v-if="source.evidence_chunks.some((chunk) => chunk.role === 'neighbor')"
          >已包含相邻补充内容。</template
        >
      </p>
      <template v-else-if="source.supporting_chunks?.length"
        ><h3>相邻补充片段</h3>
        <pre v-for="(chunk, i) in source.supporting_chunks" :key="i" class="source-content">{{
          chunk.content
        }}</pre>
      </template>
      <p class="muted small">
        来源展示本轮实际引用的资料片段。引用编号有效不代表事实已通过独立核验。
      </p>
    </aside>
  </div>
</template>
