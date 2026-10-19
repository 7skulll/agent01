<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  ArrowUp,
  ArrowUpRight,
  Plus,
  MessageSquare,
  Sparkles,
  Copy,
  Pause,
  Play,
  Square,
  FileText,
  Check,
  Pencil,
  Trash2,
  BookOpen,
  ScanSearch,
  Layers,
  PanelLeft,
} from 'lucide-vue-next'
import { api, date } from '../api/client'
import {
  modeLabels,
  activeStatuses,
  openStatuses,
  type Mode,
  type Run,
  type Source,
} from '../api/types'
import { useRunStream } from '../features/chat/useRunStream'
import ErrorBanner from '../components/ErrorBanner.vue'
import MarkdownAnswer from '../components/MarkdownAnswer.vue'
import SourcePanel from '../components/SourcePanel.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ActionDialog from '../components/ActionDialog.vue'
import SelectMenu from '../components/SelectMenu.vue'
import type { Conversation } from '../api/types'
import { useKnowledgeBases } from '../features/knowledge/useKnowledgeBases'
const bases = useKnowledgeBases()
const { activeKnowledgeBaseId } = bases
const route = useRoute(),
  router = useRouter(),
  client = useQueryClient()
const cid = computed(() => String(route.params.conversationId || ''))
const prompt = ref(''),
  mode = ref<Mode>('hybrid'),
  knowledge = ref(true),
  sending = ref(false),
  controlling = ref(false)
const error = ref<unknown>(null),
  selected = ref<Source | null>(null),
  copied = ref('')
const conversations = useInfiniteQuery({
  queryKey: ['conversations'],
  initialPageParam: 0,
  queryFn: ({ pageParam }) => api.conversations(pageParam),
  getNextPageParam: (last, pages) =>
    last.length === 50 ? pages.reduce((count, page) => count + page.length, 0) : undefined,
  refetchInterval: 15000,
})
const turns = useInfiniteQuery({
  queryKey: computed(() => ['turns', cid.value]),
  enabled: computed(() => !!cid.value),
  initialPageParam: 0,
  queryFn: ({ queryKey, pageParam }) => api.turns(String(queryKey[1]), pageParam),
  getNextPageParam: (last) =>
    last.items.length && last.offset + last.items.length < last.total
      ? last.offset + last.items.length
      : undefined,
  refetchInterval: (query) =>
    query.state.data?.pages.some((page) => page.items.some((r) => openStatuses.has(r.status)))
      ? 5000
      : false,
})
const scoped = ref(false),
  fileIds = ref<string[]>([])
const files = useQuery({
  queryKey: computed(() => ['all-files', activeKnowledgeBaseId.value]),
  enabled: computed(() => knowledge.value && !!bases.current.value),
  queryFn: () => api.allFiles(activeKnowledgeBaseId.value),
})
watch(activeKnowledgeBaseId, () => {
  fileIds.value = []
  scoped.value = false
})
const editing = ref<Conversation | null>(null),
  deleting = ref<Conversation | null>(null)
const title = ref(''),
  managing = ref(false),
  manageError = ref<unknown>(null),
  notice = ref('')
function openRename(item: Conversation) {
  editing.value = item
  title.value = item.title || `对话 ${item.thread_id.slice(0, 6)}`
  manageError.value = null
}
function openDelete(item: Conversation) {
  deleting.value = item
  manageError.value = null
}
async function rename() {
  if (!editing.value || managing.value || !title.value.trim()) return
  managing.value = true
  manageError.value = null
  try {
    await api.renameConversation(editing.value.thread_id, title.value.trim())
    editing.value = null
    await client.invalidateQueries({ queryKey: ['conversations'] })
  } catch (e) {
    manageError.value = e
  } finally {
    managing.value = false
  }
}
async function removeConversation() {
  const item = deleting.value
  if (!item || managing.value) return
  managing.value = true
  manageError.value = null
  try {
    const result = await api.deleteConversation(item.thread_id)
    if (cid.value === item.thread_id) {
      stream.stop()
      pending = undefined
      await router.push('/chat')
    }
    client.removeQueries({ queryKey: ['turns', item.thread_id] })
    deleting.value = null
    notice.value = result.cleanup_pending
      ? '会话已删除，检查点清理将在后续重试。'
      : '会话、问答历史与检查点已删除。'
    await client.invalidateQueries({ queryKey: ['conversations'] })
  } catch (e) {
    manageError.value = e
  } finally {
    managing.value = false
  }
}
const scroll = ref<HTMLElement>()
const follow = ref(true)
const creating = ref(false)
function onScroll() {
  const el = scroll.value
  if (el) follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 100
}
const conversationItems = computed(() =>
  (conversations.data.value?.pages.flat() || []).filter(
    (item, index, list) => list.findIndex((c) => c.thread_id === item.thread_id) === index,
  ),
)
const showHistory = ref(false)
const historyCollapsed = ref(localStorage.getItem('rag-history-collapsed') === 'true')
const historyMedia = window.matchMedia('(max-width: 520px)')
const compactHistory = ref(historyMedia.matches)
const historyVisible = computed(() =>
  compactHistory.value ? showHistory.value : !historyCollapsed.value,
)
function updateHistoryLayout() {
  compactHistory.value = historyMedia.matches
  showHistory.value = false
}
function toggleHistory() {
  if (compactHistory.value) showHistory.value = !showHistory.value
  else historyCollapsed.value = !historyCollapsed.value
}
watch(historyCollapsed, (collapsed) =>
  localStorage.setItem('rag-history-collapsed', String(collapsed)),
)
onMounted(() => historyMedia.addEventListener('change', updateHistoryLayout))
onBeforeUnmount(() => historyMedia.removeEventListener('change', updateHistoryLayout))
const retrievalOptions: { value: Mode; label: string; description: string }[] = [
  { value: 'hybrid', label: '混合检索', description: '结合语义与关键词，适合大多数问题' },
  { value: 'keyword', label: '全文检索', description: '按关键词查找名称、编号与具体条款' },
  { value: 'vector', label: '向量检索', description: '按语义查找相关内容与相近表达' },
]
const allTurns = computed(() =>
  (turns.data.value?.pages.flatMap((page) => page.items) || [])
    .filter((r, i, arr) => arr.findIndex((t) => t.run_id === r.run_id) === i)
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.run_id.localeCompare(b.run_id)),
)
const active = computed(() => allTurns.value.find((r) => openStatuses.has(r.status)))
const streamId = computed(() =>
  active.value && activeStatuses.has(active.value.status) ? active.value.run_id : '',
)
async function sync() {
  await Promise.all([
    client.invalidateQueries({ queryKey: ['turns', cid.value] }),
    client.invalidateQueries({ queryKey: ['conversations'] }),
  ])
}
const stream = useRunStream(sync)
watch(
  () => [allTurns.value.length, stream.state.value.seq],
  async () => {
    if (follow.value) {
      await nextTick()
      if (scroll.value) scroll.value.scrollTop = scroll.value.scrollHeight
    }
  },
)
watch(
  [cid, streamId],
  ([, id]) => {
    if (id) void stream.start(id)
    else stream.stop()
  },
  { flush: 'post' },
)
watch(cid, () => {
  selected.value = null
  error.value = null
  stream.stop()
  follow.value = true
})
watch(
  () => route.query,
  (q) => {
    if (typeof q.question === 'string') prompt.value = q.question
    if (typeof q.mode === 'string' && Object.hasOwn(modeLabels, q.mode)) mode.value = q.mode as Mode
  },
  { immediate: true },
)
let pending:
  | {
      id: string
      text: string
      key: string
      mode: Mode
      kbId: string | null
      fileIds: string[] | null
    }
  | undefined
async function create() {
  if (sending.value || creating.value) return
  creating.value = true
  error.value = null
  try {
    const item = await api.createConversation()
    await router.push(`/chat/${item.thread_id}`)
    await client.invalidateQueries({ queryKey: ['conversations'] })
  } catch (e) {
    error.value = e
  } finally {
    creating.value = false
  }
}
async function send() {
  const text = prompt.value.trim()
  if (!text || sending.value || creating.value || active.value) return
  if (knowledge.value && !bases.current.value) {
    error.value = new Error('请先创建或选择知识库。')
    return
  }
  const kbId = knowledge.value ? activeKnowledgeBaseId.value : null
  const requestMode = mode.value
  const requestFiles = kbId && scoped.value ? [...new Set(fileIds.value)].sort() : null
  if (requestFiles && requestFiles.length > 100) {
    error.value = new Error('最多选择 100 个文件。')
    return
  }
  sending.value = true
  error.value = null
  try {
    let id = cid.value
    if (!id) {
      const conversation = await api.createConversation()
      id = conversation.thread_id
      await router.push(`/chat/${id}`)
    }
    if (
      !pending ||
      pending.id !== id ||
      pending.text !== text ||
      pending.mode !== requestMode ||
      pending.kbId !== kbId ||
      JSON.stringify(pending.fileIds) !== JSON.stringify(requestFiles)
    )
      pending = {
        id,
        text,
        key: crypto.randomUUID(),
        mode: requestMode,
        kbId,
        fileIds: requestFiles,
      }
    await api.createRun(id, text, pending.key, pending.mode, pending.kbId, pending.fileIds)
    if (cid.value === id) {
      prompt.value = ''
      follow.value = true
    }
    pending = undefined
    await sync()
  } catch (e) {
    error.value = e
  } finally {
    sending.value = false
  }
}
async function control(run: Run, action: 'pause' | 'resume' | 'cancel') {
  controlling.value = true
  error.value = null
  try {
    await api.control(run.run_id, action)
    await sync()
    if (action === 'resume' && streamId.value === run.run_id) void stream.start(run.run_id, true)
  } catch (e) {
    error.value = e
  } finally {
    controlling.value = false
  }
}
async function more() {
  try {
    const ownId = cid.value
    if (turns.isFetching.value) return
    const before = scroll.value?.scrollHeight || 0
    follow.value = false
    await turns.fetchNextPage({ throwOnError: true })
    if (ownId !== cid.value) return
    await nextTick()
    if (scroll.value) scroll.value.scrollTop += scroll.value.scrollHeight - before
  } catch (e) {
    error.value = e
  }
}
async function moreConversations() {
  if (conversations.isFetching.value) return
  try {
    await conversations.fetchNextPage({ throwOnError: true })
  } catch (e) {
    error.value = e
  }
}
async function copy(run: Run) {
  try {
    await navigator.clipboard.writeText(run.answer || '')
    copied.value = run.run_id
    setTimeout(() => (copied.value = ''), 1500)
  } catch {
    error.value = new Error('复制失败，请手动选择回答内容复制。')
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    void send()
  }
}
const suggestions = [
  { title: '梳理知识', text: '总结知识库中的核心内容', icon: Layers },
  { title: '了解制度', text: '员工年假应该如何申请？', icon: BookOpen },
  { title: '查找依据', text: '帮我找到相关的制度依据', icon: ScanSearch },
]
</script>
<template>
  <div class="chat-workspace" @keydown.esc="showHistory = false">
    <button
      v-if="compactHistory && showHistory"
      class="history-backdrop"
      aria-label="关闭会话记录"
      @click="showHistory = false"
    ></button>
    <aside
      id="conversation-history"
      v-show="historyVisible"
      class="conversation-sidebar"
      :class="{ 'mobile-conversations': showHistory }"
    >
      <div class="conversation-heading">
        <strong>会话记录</strong
        ><button
          class="icon-button"
          aria-label="新建会话"
          :disabled="sending || creating"
          @click="create"
        >
          <Plus :size="18" />
        </button>
      </div>
      <button class="button new-chat" :disabled="sending || creating" @click="create">
        <Plus :size="16" /> 新建对话
      </button>
      <div class="conversation-list">
        <p class="list-label">最近会话</p>
        <div v-for="item in conversationItems" :key="item.thread_id" class="conversation-row">
          <RouterLink
            :to="`/chat/${item.thread_id}`"
            class="conversation-item"
            @click="showHistory = false"
            :class="{ active: cid === item.thread_id }"
          >
            <MessageSquare :size="16" /><span
              >{{ item.title || `对话 ${item.thread_id.slice(0, 6)}`
              }}<small>{{ date(item.created_at) }}</small></span
            >
            <span v-if="item.active_run_id" class="dot online"></span>
          </RouterLink>
          <div class="conversation-actions">
            <button
              class="icon-button"
              :aria-label="`重命名会话 ${item.title || item.thread_id.slice(0, 6)}`"
              :disabled="managing"
              @click="openRename(item)"
            >
              <Pencil :size="13" />
            </button>
            <button
              class="icon-button"
              :aria-label="`删除会话 ${item.title || item.thread_id.slice(0, 6)}`"
              :disabled="managing || !!item.active_run_id || sending"
              @click="openDelete(item)"
            >
              <Trash2 :size="13" />
            </button>
          </div>
        </div>
        <p v-if="!conversationItems.length" class="small muted conversation-empty">
          你的对话会保存在这里
        </p>
        <button
          v-if="conversations.hasNextPage.value"
          class="text-button"
          :disabled="conversations.isFetching.value"
          @click="moreConversations"
        >
          加载更多会话
        </button>
      </div>
      <RouterLink class="knowledge-shortcut" to="/knowledge"
        ><FileText :size="16" /><span>为回答添加更多知识</span><ArrowUpRight :size="15"
      /></RouterLink>
    </aside>
    <section class="chat-main">
      <div class="chat-topline">
        <button
          class="icon-button history-toggle"
          :aria-label="historyVisible ? '隐藏对话记录' : '显示对话记录'"
          :title="historyVisible ? '隐藏对话记录' : '显示对话记录'"
          :aria-expanded="historyVisible"
          aria-controls="conversation-history"
          @click="toggleHistory"
        >
          <PanelLeft :size="18" /></button
        ><span><span class="dot online"></span>{{ knowledge ? '知识库问答' : '普通对话' }}</span
        ><span>基于文档 · 附带引用</span>
      </div>
      <div ref="scroll" class="chat-scroll" @scroll="onScroll">
        <div v-if="!allTurns.length" class="chat-welcome">
          <div class="welcome-mark"><Sparkles :size="29" /></div>
          <span class="eyebrow">YOUR KNOWLEDGE, IN FOCUS</span>
          <h1>让每一次提问，<br /><span>都有据可循。</span></h1>
          <p>从你的文档出发，让复杂的信息，成为清晰的答案。</p>
          <div class="suggestions">
            <button
              v-for="suggestion in suggestions"
              :key="suggestion.title"
              @click="prompt = suggestion.text"
            >
              <span class="suggestion-icon"><component :is="suggestion.icon" :size="18" /></span>
              <strong>{{ suggestion.title }}</strong
              ><span class="suggestion-text">{{ suggestion.text }}</span>
              <ArrowUpRight :size="15" />
            </button>
          </div>
          <RouterLink class="welcome-link" to="/knowledge"
            >还没有资料？先上传第一份文档 <ArrowUpRight :size="14"
          /></RouterLink>
        </div>
        <div v-else class="messages">
          <button
            v-if="turns.hasNextPage.value"
            class="text-button load-history"
            :disabled="turns.isFetching.value"
            @click="more"
          >
            加载更早的问答
          </button>
          <article v-for="run in allTurns" :key="run.run_id" class="turn">
            <div class="human-message">
              <span class="message-avatar human">我</span>
              <div>
                <span class="message-author"
                  >你 <small>{{ date(run.created_at) }}</small></span
                >
                <p>{{ run.message }}</p>
              </div>
            </div>
            <div class="assistant-message">
              <span class="message-avatar assistant"><Sparkles :size="17" /></span>
              <div class="assistant-body">
                <div class="answer-heading">
                  <span class="message-author"
                    >知序
                    <span class="subtle-chip">{{
                      run.knowledge_base_id ? modeLabels[run.retrieval_mode] : '普通对话'
                    }}</span></span
                  ><StatusBadge :status="run.status" />
                </div>
                <template v-if="run.status === 'completed'"
                  ><span v-if="run.answer_status === 'insufficient_evidence'" class="badge muted"
                    >资料不足</span
                  ><MarkdownAnswer
                    :text="run.answer || ''"
                    :sources="run.sources"
                    :segments="
                      run.answer_protocol === 'rag-answer-v2' ? run.answer_segments : undefined
                    "
                    @source="selected = $event"
                  />
                  <div v-if="run.sources.length" class="answer-sources">
                    <button
                      v-for="source in run.sources"
                      :key="source.citation_id"
                      @click="selected = source"
                    >
                      <span>{{ source.citation_id }}</span
                      ><FileText :size="13" />{{ source.name }}
                    </button>
                  </div>
                  <button class="text-button copy-button" @click="copy(run)">
                    <Check v-if="copied === run.run_id" :size="14" /><Copy v-else :size="14" />{{
                      copied === run.run_id ? '已复制' : '复制回答'
                    }}
                  </button></template
                ><template v-else
                  ><div
                    v-if="
                      stream.state.value.runId === run.run_id &&
                      stream.state.value.drafts[stream.state.value.messageId]
                    "
                    class="draft"
                  >
                    <span class="draft-label">生成草稿 · 尚未完成校验</span
                    ><MarkdownAnswer
                      :text="stream.state.value.drafts[stream.state.value.messageId] || ''"
                    />
                  </div>
                  <p class="run-phase">
                    <span v-if="activeStatuses.has(run.status)" class="loader small-loader"></span
                    >{{
                      run.pause_requested && activeStatuses.has(run.status)
                        ? '正在请求暂停，等待当前节点完成…'
                        : stream.state.value.runId === run.run_id && activeStatuses.has(run.status)
                          ? stream.state.value.phase
                          : run.status === 'recovering'
                            ? '服务中断，等待你手动恢复。'
                            : run.status === 'paused'
                              ? '任务已暂停，可以继续执行。'
                              : run.status === 'cancelled'
                                ? '已取消，本轮没有发布正式回答。'
                                : run.error?.message || '等待执行…'
                    }}
                  </p>
                  <div class="run-controls">
                    <template v-if="activeStatuses.has(run.status)"
                      ><button
                        class="button small-button"
                        :disabled="controlling || run.pause_requested"
                        @click="control(run, 'pause')"
                      >
                        <Pause :size="13" />暂停</button
                      ><button
                        class="button small-button"
                        :disabled="controlling"
                        @click="control(run, 'cancel')"
                      >
                        <Square :size="12" />取消
                      </button></template
                    ><template
                      v-if="
                        run.status === 'paused' ||
                        run.status === 'recovering' ||
                        (run.status === 'failed' && run.error?.retryable)
                      "
                      ><button
                        class="button small-button"
                        :disabled="controlling || (!!active && active.run_id !== run.run_id)"
                        @click="control(run, 'resume')"
                      >
                        <Play :size="13" />恢复执行</button
                      ><button
                        v-if="run.status !== 'failed'"
                        class="button small-button"
                        :disabled="controlling"
                        @click="control(run, 'cancel')"
                      >
                        取消任务
                      </button></template
                    >
                  </div></template
                >
                <details
                  v-if="
                    stream.state.value.runId === run.run_id &&
                    Object.keys(stream.state.value.details).length
                  "
                  class="execution-details"
                >
                  <summary>执行详情</summary>
                  <pre>{{ JSON.stringify(stream.state.value.details, null, 2) }}</pre>
                </details>
                <p v-if="run.status === 'failed'" class="small muted">
                  {{ run.error?.code
                  }}<template v-if="run.error?.request_id">
                    · 请求 ID：{{ run.error.request_id }}</template
                  >
                </p>
              </div>
            </div>
          </article>
        </div>
      </div>
      <div class="composer-wrap">
        <div v-if="notice" class="notice" role="status">
          {{ notice }} <button class="text-button" @click="notice = ''">关闭</button>
        </div>
        <ErrorBanner :error="error || turns.error.value || conversations.error.value" />
        <div v-if="stream.warning.value && active" class="notice warning">
          {{ stream.warning.value }}
          <button class="text-button" @click="stream.start(active.run_id)">重新连接</button>
        </div>
        <details v-if="knowledge" class="chat-file-scope">
          <summary>问答范围：{{ scoped ? `指定文件（${fileIds.length}）` : '全部文件' }}</summary>
          <label><input v-model="scoped" type="checkbox" /> 仅在指定文件中查找</label>
          <div v-if="scoped" class="file-options">
            <ErrorBanner :error="files.error.value" />
            <label v-for="file in files.data.value" :key="file.file_id"
              ><input v-model="fileIds" type="checkbox" :value="file.file_id" />{{
                file.name
              }}</label
            >
            <p class="small muted">
              最多 100 个文件。{{
                !fileIds.length
                  ? '未选文件时范围为空，不会自动搜索全库。'
                  : '所选范围用于下一次提问。'
              }}
            </p>
          </div>
        </details>
        <form class="composer" @submit.prevent="send">
          <textarea
            v-model="prompt"
            rows="2"
            maxlength="4000"
            aria-label="输入问题"
            :placeholder="active ? '当前会话有未完成的任务，请先完成或取消' : '向你的知识库提问…'"
            :disabled="sending"
            @keydown="keydown"
          ></textarea>
          <div class="composer-footer">
            <div class="composer-options">
              <label class="knowledge-toggle"
                ><input v-model="knowledge" type="checkbox" /> 使用知识库{{
                  bases.current.value ? `：${bases.current.value.name}` : '（请先创建）'
                }}</label
              ><SelectMenu
                v-model="mode"
                :options="retrievalOptions"
                label="问答检索方式"
                :disabled="!knowledge"
              />
            </div>
            <div class="send-options">
              <span>{{ prompt.length }}/4000</span
              ><button
                class="send-button"
                type="submit"
                aria-label="发送问题"
                :disabled="
                  !prompt.trim() || sending || !!active || (knowledge && !bases.current.value)
                "
              >
                <span v-if="sending" class="loader small-loader"></span
                ><ArrowUp v-else :size="21" />
              </button>
            </div>
          </div>
        </form>
        <p class="composer-hint">
          Enter 发送 · Shift + Enter 换行 <span>回答基于现有资料，请结合来源判断。</span>
        </p>
      </div>
    </section>
    <ActionDialog :open="!!editing" title="重命名会话" :busy="managing" @close="editing = null">
      <form @submit.prevent="rename">
        <label class="field"
          >会话名称<input v-model="title" required maxlength="100" :disabled="managing"
        /></label>
        <ErrorBanner :error="manageError" />
        <div class="kb-modal-actions">
          <button class="button" type="button" :disabled="managing" @click="editing = null">
            取消
          </button>
          <button class="button primary" :disabled="managing || !title.trim()">
            {{ managing ? '保存中…' : '保存名称' }}
          </button>
        </div>
      </form>
    </ActionDialog>
    <ActionDialog :open="!!deleting" title="删除会话" :busy="managing" @close="deleting = null">
      <p>
        确认删除「{{
          deleting?.title || `对话 ${deleting?.thread_id.slice(0, 6)}`
        }}」及其全部问答历史？此操作无法撤销。
      </p>
      <p class="muted">知识库文件保留。有未结束任务时，请先完成或取消。</p>
      <ErrorBanner :error="manageError" />
      <div class="kb-modal-actions">
        <button class="button" :disabled="managing" @click="deleting = null">取消</button>
        <button class="button danger" :disabled="managing" @click="removeConversation">
          {{ managing ? '删除中…' : '确认删除会话' }}
        </button>
      </div>
    </ActionDialog>
    <SourcePanel :source="selected" @close="selected = null" />
  </div>
</template>
