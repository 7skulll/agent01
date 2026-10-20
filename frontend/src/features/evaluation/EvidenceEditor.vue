<script setup lang="ts">
import type { EvidenceGroup } from './types'
const groups = defineModel<EvidenceGroup[]>({ required: true })
const props = defineProps<{ files: { file_id: string; name: string; content_hash: string }[] }>()
function add(group?: EvidenceGroup) {
  const f = props.files[0]
  if (!f) return
  const anchor = { file_id: f.file_id, content_hash: f.content_hash, quote: '', page: null }
  if (group) group.alternatives.push(anchor)
  else groups.value.push({ fact: '', alternatives: [anchor] })
}
</script>
<template>
  <details class="eval-anchors">
    <summary>必要原文证据（{{ groups.length }} 组）</summary>
    <p class="small muted">
      每个事实一组，组内证据可替代。复制原文短句；保存文件内容哈希与可选页码，重切片后按文本重新映射。暂不支持跨切片长句匹配。
    </p>
    <fieldset v-for="(group, g) in groups" :key="g">
      <legend>必要事实 {{ g + 1 }}</legend>
      <input
        v-model="group.fact"
        :aria-label="`必要事实 ${g + 1}`"
        placeholder="这个事实必须有证据支持"
        maxlength="500"
        required
      />
      <div v-for="(anchor, a) in group.alternatives" :key="a" class="field">
        <label
          >证据文件<select
            v-model="anchor.file_id"
            :aria-label="`事实 ${g + 1} 证据 ${a + 1} 文件`"
            @change="
              anchor.content_hash =
                files.find((f) => f.file_id === anchor.file_id)?.content_hash || ''
            "
          >
            <option v-for="f in files" :key="f.file_id" :value="f.file_id">{{ f.name }}</option>
          </select></label
        >
        <textarea
          v-model="anchor.quote"
          :aria-label="`事实 ${g + 1} 证据 ${a + 1} 原文`"
          placeholder="粘贴原文，至少 4 字；请先在相关文件中勾选此文件"
          minlength="4"
          maxlength="2000"
          required
          rows="2"
        />
        <label
          >页码（可选）<input
            v-model.number="anchor.page"
            type="number"
            min="1"
            :aria-label="`事实 ${g + 1} 证据 ${a + 1} 页码`"
            @change="anchor.page = anchor.page || null"
        /></label>
        <small class="muted">内容哈希 {{ anchor.content_hash.slice(0, 12) }}…</small>
        <button
          v-if="group.alternatives.length > 1"
          type="button"
          class="text-button"
          @click="group.alternatives.splice(a, 1)"
        >
          移除替代证据
        </button>
      </div>
      <button
        type="button"
        class="text-button"
        :disabled="group.alternatives.length >= 5 || !files.length"
        @click="add(group)"
      >
        添加替代证据
      </button>
      <button type="button" class="text-button" @click="groups.splice(g, 1)">移除此事实</button>
    </fieldset>
    <button
      type="button"
      class="button"
      :disabled="groups.length >= 10 || !files.length"
      @click="add()"
    >
      添加必要事实
    </button>
  </details>
</template>
