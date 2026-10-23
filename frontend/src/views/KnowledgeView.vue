<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import {
  UploadCloud,
  FileText,
  RefreshCw,
  ArrowRight,
  Database,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Lock,
} from 'lucide-vue-next'
import { api, date, size } from '../api/client'
import ErrorBanner from '../components/ErrorBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ActionDialog from '../components/ActionDialog.vue'
import type { FileItem } from '../api/types'
import { useKnowledgeBases } from '../features/knowledge/useKnowledgeBases'
import { canIngest, isAdmin } from '../features/auth/store'
const bases = useKnowledgeBases()
const { activeKnowledgeBaseId } = bases
const client = useQueryClient()
const offset = ref(0),
  filter = ref(''),
  drag = ref(false),
  indexing = ref(false),
  message = ref('')
const error = ref<unknown>(null),
  input = ref<HTMLInputElement>()
const files = useQuery({
  queryKey: computed(() => ['files', activeKnowledgeBaseId.value, offset.value]),
  enabled: computed(() => !!bases.current.value),
  queryFn: () => api.files(activeKnowledgeBaseId.value, offset.value),
  refetchInterval: (query) =>
    query.state.data?.items.some((f) => ['parsing', 'indexing'].includes(f.index_status))
      ? 3000
      : 15000,
})
const visible = computed(() =>
  (files.data.value?.items || []).filter((f) =>
    f.name.toLowerCase().includes(filter.value.toLowerCase()),
  ),
)
type Upload = {
  file: File
  kbId: string
  key: string
  state: 'waiting' | 'uploading' | 'done' | 'failed'
  error?: unknown
}
const uploads = ref<Upload[]>([])
const uploading = ref(false)
const currentUploads = computed(() =>
  uploads.value.filter((item) => item.kbId === activeKnowledgeBaseId.value),
)
watch(activeKnowledgeBaseId, () => {
  offset.value = 0
  filter.value = ''
  error.value = null
  message.value = ''
})
function drop(event: DragEvent) {
  drag.value = false
  select(event.dataTransfer?.files || null)
}
function select(list: FileList | File[] | null) {
  if (!list || !bases.current.value || !canIngest.value) return
  for (const file of Array.from(list)) {
    const valid =
      /\.(txt|md|pdf|docx|xlsx)$/i.test(file.name) && file.size > 0 && file.size <= 50 * 1024 ** 2
    uploads.value.push({
      file,
      kbId: activeKnowledgeBaseId.value,
      key: crypto.randomUUID(),
      state: valid ? 'waiting' : 'failed',
      error: valid
        ? undefined
        : new Error('请使用非空的 TXT、MD、PDF、DOCX 或 XLSX，单文件不超过 50 MiB。'),
    })
  }
  if (input.value) input.value.value = ''
  void process()
}
async function process() {
  if (uploading.value) return
  uploading.value = true
  try {
    while (true) {
      const item = uploads.value.find((upload) => upload.state === 'waiting')
      if (!item) break
      item.state = 'uploading'
      item.error = undefined
      try {
        await api.upload(item.kbId, item.file, item.key)
        item.state = 'done'
        await client.invalidateQueries({ queryKey: ['files'] })
        await client.invalidateQueries({ queryKey: ['all-files'] })
        await client.invalidateQueries({ queryKey: ['knowledge-bases'] })
      } catch (e) {
        item.error = e
        item.state = 'failed'
      }
    }
  } finally {
    uploading.value = false
  }
}
function retry(item: Upload) {
  if (
    !/\.(txt|md|pdf|docx|xlsx)$/i.test(item.file.name) ||
    !item.file.size ||
    item.file.size > 50 * 1024 ** 2
  )
    return
  item.state = 'waiting'
  void process()
}
const deleting = ref<FileItem | null>(null)
const deletingBusy = ref(false),
  downloading = ref(''),
  collecting = ref(false)
const actionError = ref<unknown>(null)
function confirmDelete(file: FileItem) {
  deleting.value = file
  actionError.value = null
}
async function removeFile() {
  const file = deleting.value
  if (!file || deletingBusy.value) return
  deletingBusy.value = true
  actionError.value = null
  try {
    const result = await api.deleteFile(file.kb_id, file.file_id)
    if (activeKnowledgeBaseId.value === file.kb_id) {
      if (files.data.value?.items.length === 1 && offset.value) offset.value -= 20
      message.value = result.cleanup_pending
        ? '文件已删除，原文件清理将在后续重试。'
        : '文件及索引已删除，历史引用保留。'
    }
    deleting.value = null
    await Promise.all([
      client.invalidateQueries({ queryKey: ['files', file.kb_id] }),
      client.invalidateQueries({ queryKey: ['all-files', file.kb_id] }),
      client.invalidateQueries({ queryKey: ['knowledge-bases'] }),
    ])
  } catch (e) {
    actionError.value = e
  } finally {
    deletingBusy.value = false
  }
}
async function download(file: FileItem) {
  downloading.value = file.file_id
  error.value = null
  try {
    await api.downloadFile(file.kb_id, file.file_id, file.name)
  } catch (e) {
    if (activeKnowledgeBaseId.value === file.kb_id) error.value = e
  } finally {
    downloading.value = ''
  }
}
async function collect() {
  if (collecting.value) return
  collecting.value = true
  error.value = null
  try {
    const result = await api.collectOrphans()
    message.value = `已检查全部知识库，新增清理 ${result.queued} 个过期孤儿文件；待重试 ${result.cleanup_pending} 个。`
  } catch (e) {
    error.value = e
  } finally {
    collecting.value = false
  }
}
async function index() {
  const kbId = activeKnowledgeBaseId.value
  if (!bases.current.value || !canIngest.value) return
  indexing.value = true
  error.value = null
  message.value = ''
  try {
    const result = await api.index(kbId)
    if (activeKnowledgeBaseId.value !== kbId) return
    message.value = result.count
      ? `已安排 ${result.count} 个文件，状态将自动更新。`
      : '当前没有需要构建索引的文件。'
    await client.invalidateQueries({ queryKey: ['files'] })
  } catch (e) {
    error.value = e
  } finally {
    indexing.value = false
  }
}
</script>
<template>
  <div class="page knowledge-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">KNOWLEDGE LIBRARY</span>
        <h1>知识库文件<span class="heading-dot">.</span></h1>
        <p>
          {{
            bases.current.value
              ? `正在管理「${bases.current.value.name}」的文件，文件和检索结果按库隔离。`
              : '请点击右上角 + 创建知识库，再上传文档。'
          }}
        </p>
      </div>
      <Button
        v-if="canIngest"
        icon="pi pi-upload"
        label="上传文件"
        aria-label="上传文件"
        class="page-heading-action"
        :disabled="!bases.current.value"
        @click="input?.click()"
      />
    </div>
    <input
      ref="input"
      type="file"
      class="sr-only"
      multiple
      accept=".txt,.md,.pdf,.docx,.xlsx"
      aria-label="选择上传文件"
      @change="select(($event.target as HTMLInputElement).files)"
    />
    <div class="knowledge-stats">
      <div>
        <span class="stat-icon blue"><Database :size="20" /></span>
        <div>
          <small>知识库文件</small
          ><strong>{{ files.data.value?.total ?? '—' }} <span>份文档</span></strong>
        </div>
      </div>
      <div>
        <span class="stat-icon green"><CheckCheck :size="20" /></span>
        <div>
          <small>当前页全文就绪</small
          ><strong
            >{{ files.data.value?.items.filter((f) => f.keyword_ready).length ?? '—' }}
            <span>份文档</span></strong
          >
        </div>
      </div>
      <div class="stat-help">
        <span class="dot online"></span>独立索引，灵活检索<small>全文检索不依赖向量化完成</small>
      </div>
    </div>
    <button
      v-if="canIngest"
      class="upload-zone"
      :disabled="!bases.current.value"
      :class="{ dragging: drag }"
      @click="input?.click()"
      @dragover.prevent="drag = true"
      @dragleave.prevent="drag = false"
      @drop.prevent="drop"
    >
      <span class="upload-icon"><UploadCloud :size="26" /></span
      ><span class="upload-copy"
        ><strong>把知识放进来，让答案生长。</strong>
        <span>拖放文件到这里，或点击上传 · 单文件最大 50 MiB</span>
        <small>TXT / MD / DOCX / XLSX / PDF（需要包含文本层）</small></span
      >
      <span class="upload-cta">选择文档 <ArrowRight :size="16" /></span>
    </button>
    <div v-else class="card read-only-note">
      <span class="stat-icon blue"><Lock :size="20" /></span>
      <div>
        <strong>当前账号为普通用户</strong>
        <p>可以查看资料、检索和问答；上传文件、构建索引与管理知识库请联系员工或管理员账号。</p>
      </div>
    </div>
    <div v-if="currentUploads.length" class="upload-list card">
      <div class="section-heading">
        <strong>上传队列</strong
        ><button
          v-if="!uploading"
          class="text-button"
          @click="uploads = uploads.filter((u) => u.state !== 'done')"
        >
          清除已完成
        </button>
      </div>
      <div v-for="item in currentUploads" :key="item.key" class="upload-row">
        <FileText :size="16" /><span>{{ item.file.name }}</span
        ><small>{{
          {
            waiting: '等待上传',
            uploading: '上传中…',
            done: '已上传 · 待索引',
            failed: '上传失败',
          }[item.state]
        }}</small
        ><button
          v-if="item.state === 'failed'"
          class="text-button"
          :disabled="
            !/\.(txt|md|pdf|docx|xlsx)$/i.test(item.file.name) ||
            !item.file.size ||
            item.file.size > 50 * 1024 ** 2
          "
          @click="retry(item)"
        >
          重试</button
        ><ErrorBanner :error="item.error" />
      </div>
    </div>
    <ErrorBanner :error="error || files.error.value" />
    <div v-if="message" class="notice" role="status">{{ message }}</div>
    <section class="card files-card">
      <div class="section-heading">
        <div class="section-title">
          <h2>全部文件</h2>
          <span class="count">{{ files.data.value?.total ?? 0 }}</span>
        </div>
        <div class="toolbar">
          <button
            v-if="isAdmin"
            class="button"
            :disabled="collecting"
            @click="collect"
            title="清理全部知识库中超过 24 小时且无记录引用的上传残留"
          >
            {{ collecting ? '清理中…' : '清理上传残留' }}
          </button>
          <input
            v-model="filter"
            class="search-input"
            placeholder="按文件名筛选本页"
            aria-label="筛选本页文件"
          /><button
            v-if="canIngest"
            class="button"
            :disabled="indexing || !bases.current.value"
            @click="index"
          >
            <RefreshCw :size="15" :class="{ spin: indexing }" />{{
              indexing ? '安排中…' : '构建 / 重试索引'
            }}
          </button>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>文件名称</th>
              <th>大小</th>
              <th>切片</th>
              <th>全文检索</th>
              <th>解析 / 向量状态</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="file in visible" :key="file.file_id">
              <td>
                <div class="file-name">
                  <span class="file-icon"><FileText :size="19" /></span>
                  <div>
                    <strong>{{ file.name }}</strong
                    ><small>{{ file.extension.replace('.', '').toUpperCase() }} 文档</small
                    ><span v-if="file.index_error || file.keyword_error" class="file-error">{{
                      file.index_error || file.keyword_error
                    }}</span>
                  </div>
                </div>
              </td>
              <td>{{ size(file.size) }}</td>
              <td>{{ file.chunk_count }}</td>
              <td>
                <span
                  class="badge"
                  :class="
                    file.keyword_ready ? 'completed' : file.keyword_error ? 'failed' : 'muted'
                  "
                  ><span class="dot"></span
                  >{{
                    file.keyword_ready ? '可用' : file.keyword_error ? '构建失败' : '待构建'
                  }}</span
                >
              </td>
              <td><StatusBadge :status="file.index_status" /></td>
              <td class="nowrap">{{ date(file.updated_at) }}</td>
              <td>
                <div class="file-actions">
                  <button
                    class="text-button"
                    :aria-label="`下载 ${file.name}`"
                    :disabled="!!downloading"
                    @click="download(file)"
                  >
                    下载
                  </button>
                  <button
                    v-if="canIngest"
                    class="text-button danger-text"
                    :aria-label="`删除文件 ${file.name}`"
                    :disabled="deletingBusy"
                    @click="confirmDelete(file)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!visible.length" class="empty compact">
        <FileText :size="30" />
        <h3>
          {{
            !bases.current.value
              ? '请先创建或选择知识库'
              : files.isPending.value
                ? '正在读取文件…'
                : filter
                  ? '本页没有匹配的文件'
                  : '知识从第一份文档开始'
          }}
        </h3>
        <p>上传文档后，点击构建索引，即可用于检索和问答。</p>
      </div>
      <div class="pagination">
        <span>共 {{ files.data.value?.total ?? 0 }} 个文件 · 每页 20 条</span>
        <div>
          <button class="icon-button" aria-label="上一页" :disabled="!offset" @click="offset -= 20">
            <ChevronLeft :size="18" /></button
          ><span>{{ Math.floor(offset / 20) + 1 }}</span
          ><button
            class="icon-button"
            aria-label="下一页"
            :disabled="offset + 20 >= (files.data.value?.total ?? 0)"
            @click="offset += 20"
          >
            <ChevronRight :size="18" />
          </button>
        </div>
      </div>
    </section>
    <ActionDialog :open="!!deleting" title="删除文件" :busy="deletingBusy" @close="deleting = null">
      <p>确认删除「{{ deleting?.name }}」及其全部切片和索引？此操作无法撤销。</p>
      <p class="muted">历史回答与引用快照保留。有索引或未结束问答时，请先完成或取消。</p>
      <ErrorBanner :error="actionError" />
      <div class="kb-modal-actions">
        <button class="button" :disabled="deletingBusy" @click="deleting = null">取消</button>
        <button class="button danger" :disabled="deletingBusy" @click="removeFile">
          {{ deletingBusy ? '删除中…' : '确认删除文件' }}
        </button>
      </div>
    </ActionDialog>
    <div class="bottom-tip">
      <span>上传只是开始，让知识参与思考。</span
      ><RouterLink to="/retrieval">验证检索效果 <ArrowRight :size="15" /></RouterLink>
    </div>
  </div>
</template>
<style scoped>
.read-only-note {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 20px 24px;
}
.read-only-note strong {
  display: block;
  margin-bottom: 4px;
}
.read-only-note p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}
</style>
