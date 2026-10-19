<script setup lang="ts">
import { ref } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { Plus, Trash2 } from 'lucide-vue-next'
import { api } from '../api/client'
import type { KnowledgeBase } from '../api/types'
import { useKnowledgeBases } from '../features/knowledge/useKnowledgeBases'
import ErrorBanner from './ErrorBanner.vue'

const client = useQueryClient()
const bases = useKnowledgeBases()
const { activeKnowledgeBaseId } = bases
const creating = ref(false),
  name = ref(''),
  busy = ref(false),
  error = ref<unknown>(null)
const deleting = ref<KnowledgeBase | null>(null)
const notice = ref('')
function openCreate() {
  error.value = null
  name.value = ''
  creating.value = true
}
function openDelete() {
  error.value = null
  deleting.value = bases.current.value || null
}
async function create() {
  if (!name.value.trim() || busy.value) return
  busy.value = true
  error.value = null
  try {
    const item = await api.createKnowledgeBase(name.value.trim())
    client.setQueryData<KnowledgeBase[]>(['knowledge-bases'], (old) => [...(old || []), item])
    activeKnowledgeBaseId.value = item.kb_id
    creating.value = false
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
async function remove() {
  if (!deleting.value || busy.value) return
  busy.value = true
  error.value = null
  const id = deleting.value.kb_id
  try {
    const result = await api.deleteKnowledgeBase(id)
    client.setQueryData<KnowledgeBase[]>(['knowledge-bases'], (old) =>
      (old || []).filter((item) => item.kb_id !== id),
    )
    client.removeQueries({ queryKey: ['files', id] })
    client.removeQueries({ queryKey: ['all-files', id] })
    notice.value = result.cleanup_pending
      ? '知识库已删除，部分原文件将在后续清理时重试。'
      : '知识库及其文件和索引已删除。'
    deleting.value = null
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="kb-picker">
    <select
      v-model="activeKnowledgeBaseId"
      aria-label="当前知识库"
      :disabled="bases.isPending.value || busy"
    >
      <option v-if="!bases.data.value?.length" value="">
        {{ bases.isPending.value ? '加载知识库…' : '暂无知识库' }}
      </option>
      <option v-for="item in bases.data.value" :key="item.kb_id" :value="item.kb_id">
        {{ item.name }}
      </option>
    </select>
    <button class="icon-button" aria-label="新建知识库" title="新建知识库" @click="openCreate">
      <Plus :size="17" />
    </button>
    <button
      class="icon-button"
      aria-label="删除当前知识库"
      title="删除当前知识库"
      :disabled="!bases.current.value || busy"
      @click="openDelete"
    >
      <Trash2 :size="16" />
    </button>
    <ErrorBanner :error="bases.error.value" />
    <Teleport to="body">
      <div v-if="creating || deleting" class="kb-modal-backdrop">
        <section
          class="kb-modal card"
          role="dialog"
          aria-modal="true"
          :aria-label="creating ? '新建知识库' : '删除知识库'"
        >
          <h2>{{ creating ? '新建知识库' : '删除知识库' }}</h2>
          <form v-if="creating" @submit.prevent="create">
            <label class="field"
              >知识库名称<input
                v-model="name"
                required
                maxlength="100"
                placeholder="例如：产品资料、项目文档、人事制度"
                :disabled="busy"
            /></label>
            <ErrorBanner :error="error" />
            <div class="kb-modal-actions">
              <button type="button" class="button" :disabled="busy" @click="creating = false">
                取消</button
              ><button class="button primary" :disabled="busy || !name.trim()">
                {{ busy ? '创建中…' : '创建知识库' }}
              </button>
            </div>
          </form>
          <template v-else-if="deleting">
            <p>
              确认删除「{{ deleting.name }}」？该库及其全部文件、切片和索引将被永久删除，无法撤销。
            </p>
            <p class="muted">历史问答会保留。若有索引或未结束的问答，请先完成或取消任务。</p>
            <ErrorBanner :error="error" />
            <div class="kb-modal-actions">
              <button class="button" :disabled="busy" @click="deleting = null">取消</button
              ><button class="button danger" :disabled="busy" @click="remove">
                {{ busy ? '删除中…' : '确认删除知识库' }}
              </button>
            </div>
          </template>
        </section>
      </div>
    </Teleport>
    <div v-if="notice" class="kb-toast notice" role="status">
      {{ notice }} <button class="text-button" @click="notice = ''">关闭</button>
    </div>
  </div>
</template>
