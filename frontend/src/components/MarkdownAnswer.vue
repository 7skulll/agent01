<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import type { Source, AnswerSegment } from '../api/types'
const props = defineProps<{ text: string; sources?: Source[]; segments?: AnswerSegment[] }>()
const emit = defineEmits<{ source: [source: Source] }>()
const md = new MarkdownIt({ html: false, linkify: false, breaks: true })
const original = md.renderer.rules.text!
md.renderer.rules.text = (tokens, idx, options, env, self) => {
  const escaped = original(tokens, idx, options, env, self)
  if (env.inLink) return escaped
  return escaped.replace(/\[(\d+)\]/g, (whole, id) =>
    env.ids.has(Number(id))
      ? `<button class="citation" data-citation="${Number(id)}" aria-label="查看来源 ${Number(id)}">${Number(id)}</button>`
      : whole,
  )
}
// Do not place buttons inside links; code_inline/fence have separate renderers.
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  env.inLink = true
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return self.renderToken(tokens, idx, options)
}
md.renderer.rules.link_close = (tokens, idx, options, env, self) => {
  env.inLink = false
  return self.renderToken(tokens, idx, options)
}
// Keep document-supplied image URLs from producing unexpected network requests.
md.renderer.rules.image = (tokens, idx) => md.utils.escapeHtml(tokens[idx].content || '[图片]')
const html = computed(() => {
  const ids = new Set(props.sources?.map((s) => s.citation_id))
  if (props.segments?.length) {
    return props.segments
      .map((segment) => {
        // 新协议的正文数字不推断为引用；按钮只来自服务端校验后的结构化关联。
        const body = md.render(segment.text, { ids: new Set(), inLink: false })
        const references = segment.source_ids
          .filter((id) => ids.has(id))
          .map(
            (id) =>
              `<button class="citation" data-citation="${Number(id)}" aria-label="查看来源 ${Number(id)}">${Number(id)}</button>`,
          )
          .join('')
        return body.endsWith('</p>\n')
          ? body.replace(/<\/p>\n$/, `${references}</p>\n`)
          : body + `<p>${references}</p>`
      })
      .join('\n')
  }
  return md.render(props.text, {
    ids: props.segments === undefined ? ids : new Set(),
    inLink: false,
  })
})
function click(event: MouseEvent) {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-citation]')
  const source = props.sources?.find((s) => s.citation_id === Number(button?.dataset.citation))
  if (button && source) emit('source', source)
}
</script>
<template><div class="markdown" @click="click" v-html="html"></div></template>
