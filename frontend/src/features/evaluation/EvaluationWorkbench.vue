<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { request, date, api } from '../../api/client'
import { modeLabels, type Mode, type Source } from '../../api/types'
import {
  metricLabels,
  evaluationStatus,
  type EvalCase,
  type EvalRecord,
  type Scores,
  type CaseSet,
  type HistoryPage,
  type ExperimentOptions,
  type AnswerReview,
} from './types'
import {
  blankCase as blank,
  normalizeCases,
  parseCases,
  actualTokens as tokenCount,
  caseSetSignature,
} from './cases'
import { download } from './download'
import { percent as formatPercent } from './format'
import { useReviewDraft } from './useReviewDraft'
import { activeKnowledgeBaseId } from '../knowledge/useKnowledgeBases'
import { canIngest } from '../auth/store'
import MetricChart from './MetricChart.vue'
import AnswerQualityChart from './AnswerQualityChart.vue'
import CaseSetLibrary from './CaseSetLibrary.vue'
import LayerDiagnosis from './LayerDiagnosis.vue'
import QualityOverview from './QualityOverview.vue'
import EvidenceEditor from './EvidenceEditor.vue'
import ExperimentComparison from './ExperimentComparison.vue'
import ErrorBanner from '../../components/ErrorBanner.vue'
import MarkdownAnswer from '../../components/MarkdownAnswer.vue'
import SourcePanel from '../../components/SourcePanel.vue'
const props = defineProps<{ kbId: string }>()
const client = useQueryClient()
const mode = ref<Mode>('hybrid'),
  kind = ref('rag'),
  k = ref(5),
  scoped = ref(false),
  scope = ref<string[]>([])
const cases = ref<EvalCase[]>([blank()]),
  busy = ref(false),
  error = ref<unknown>(null),
  selectedIndex = ref(0),
  source = ref<Source | null>(null)
const id = ref(''),
  importing = ref<HTMLInputElement>()
const options = ref<ExperimentOptions>({ rerank: 'inherit', context_tokens: null, judge: 'ragas' })
const judgeChoiceExplicit = ref(false)
const comparisonEpoch = ref(0)
const historyOffset = ref(0),
  showArchived = ref(false),
  tag = ref('')
const setSplit = ref<'development' | 'acceptance'>('development'),
  setVerified = ref(false),
  setFrozen = ref(false)
const retryPlan = ref<{
    retry_indices: number[]
    unknown_indices: number[]
    reuse_count: number
  }>(),
  confirmUnknown = ref(false)
let retryKey: { parent: string; confirm: boolean; key: string } | undefined
watch(kind, (value) => {
  if (value !== 'rag') {
    options.value.context_tokens = null
    options.value.judge = 'none'
  } else if (!judgeChoiceExplicit.value && capabilities.data.value?.ragas_available) {
    options.value.judge = 'ragas'
  }
})
watch(showArchived, () => {
  historyOffset.value = 0
})
let pending: { signature: string; key: string } | undefined
const files = useQuery({
  queryKey: computed(() => ['all-files', props.kbId]),
  enabled: computed(() => !!props.kbId),
  queryFn: () => api.allFiles(props.kbId),
})
const capabilities = useQuery({
  queryKey: ['evaluation-capabilities'],
  queryFn: () =>
    request<{
      max_model_calls_per_case: number
      default_context_tokens: number
      rerank_available: boolean
      ragas_available: boolean
    }>('/evaluations/capabilities'),
})
watch(
  () => capabilities.data.value?.ragas_available,
  (available) => {
    if (available === false && options.value.judge === 'ragas') options.value.judge = 'none'
    if (available === true && kind.value === 'rag' && !judgeChoiceExplicit.value)
      options.value.judge = 'ragas'
  },
)
watch(setVerified, (value) => {
  if (!value) setFrozen.value = false
})
const corpus = useQuery({
  queryKey: computed(() => ['evaluation-corpus', props.kbId]),
  queryFn: async () => {
    const rows = await request<{ file_id: string; name: string; content_hash: string }[]>(
      `/evaluations/corpus?knowledge_base_id=${encodeURIComponent(props.kbId)}`,
    )
    return Array.isArray(rows) ? rows : []
  },
  enabled: computed(() => !!props.kbId),
})
const caseSets = useQuery({
  queryKey: ['case-sets'],
  queryFn: async () => {
    const rows = await request<CaseSet[]>('/evaluations/case-sets?include_archived=true')
    return Array.isArray(rows) ? rows : []
  },
})
const previewSet = ref<CaseSet | null>(null)
const previewLoading = ref(false)
const showArchivedSets = ref(false)
const pendingCaseSet = ref<{ id: string; test: boolean } | null>(null)
const quickSetId = ref('')
const quickSets = computed(() =>
  (caseSets.data.value || []).filter((set) => set.kb_id === props.kbId && !set.archived),
)
async function archiveCaseSet(set: CaseSet) {
  if (!canIngest.value || busy.value || active.value) return
  busy.value = true
  error.value = null
  try {
    await request(`/evaluations/case-sets/${set.case_set_id}/archive`, {
      method: 'PATCH',
      body: JSON.stringify({ archived: !set.archived }),
    })
    if (previewSet.value?.case_set_id === set.case_set_id) previewSet.value = null
    await client.invalidateQueries({ queryKey: ['case-sets'] })
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
watch(
  quickSets,
  (sets) => {
    if (!sets.some((set) => set.case_set_id === quickSetId.value)) {
      quickSetId.value = sets[0]?.case_set_id || ''
    }
  },
  { immediate: true },
)
async function quickRun() {
  const saved = quickSets.value.find((set) => set.case_set_id === quickSetId.value)
  if (saved) await testCaseSet(saved)
}
const setId = ref(''),
  setName = ref(''),
  setTags = ref(''),
  loadedCases = ref('')
const draftSignature = computed(() =>
  caseSetSignature(cases.value, {
    name: setName.value,
    tags: setTags.value.split(/[,，]/),
    split: setSplit.value,
    review_status: setVerified.value ? 'verified' : 'draft',
    frozen: setFrozen.value,
  }),
)
const boundSet = computed(() =>
  setId.value && loadedCases.value === draftSignature.value ? setId.value : null,
)
function loadSavedSet(saved: CaseSet) {
  setId.value = saved.case_set_id
  cases.value = normalizeCases(saved.cases)
  setName.value = saved.name
  setTags.value = saved.tags.join(', ')
  setSplit.value = saved.split || 'development'
  setVerified.value = saved.review_status === 'verified'
  setFrozen.value = saved.frozen || false
  loadedCases.value = caseSetSignature(cases.value, saved)
}
const reviewFields = [
  {
    key: 'fact_coverage',
    label: '必要事实覆盖',
    options: { pending: '待审核', full: '完整', partial: '部分', none: '未覆盖' },
  },
  {
    key: 'unsupported_claims',
    label: '存在无依据事实',
    options: { pending: '待审核', yes: '有', no: '无' },
  },
  {
    key: 'conditions',
    label: '适用条件与例外',
    options: { pending: '待审核', pass: '正确完整', fail: '遗漏或错误', na: '不适用' },
  },
  {
    key: 'answer_relevancy',
    label: '答案相关性',
    options: {
      pending: '待审核',
      relevant: '直接回应',
      partial: '部分相关',
      irrelevant: '答非所问',
    },
  },
  {
    key: 'citation_support',
    label: '引用语义支持',
    options: {
      pending: '待审核',
      full: '完整支持',
      partial: '部分支持',
      none: '不支持',
      na: '不适用',
    },
  },
  {
    key: 'refusal',
    label: '拒答判断',
    options: { pending: '待审核', correct: '正确', incorrect: '错误', na: '不适用' },
  },
] as const
const causeLabels: Record<string, string> = {
  annotation_gap: '标注失效或未映射',
  parsing: '解析',
  retrieval: '召回',
  rerank: '重排',
  assembly: '组装',
  generation: '生成',
  unknown: '待确认',
  retrieval_miss: '召回未包含必要原文',
  rerank_loss: '重排阶段丢失',
  selection_loss: '最终候选截断',
  assembly_loss: '上下文组装丢失',
}
const stages: Record<string, string> = {
  parsed: '已解析原文',
  retrieved: '初排召回',
  reranked: '重排后',
  selected: '最终候选',
  context: '进入上下文',
}
function resetLabels(c: EvalCase) {
  c.relevant_file_ids = []
  c.negative_file_ids = []
  c.evidence_groups = []
}
async function saveSet() {
  if (!canIngest.value || busy.value || active.value) return
  busy.value = true
  error.value = null
  const kb = props.kbId
  try {
    const saved = await request<CaseSet>('/evaluations/case-sets', {
      method: 'POST',
      body: JSON.stringify({
        knowledge_base_id: kb,
        name: setName.value,
        tags: setTags.value
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
        cases: cases.value,
        split: setSplit.value,
        review_status: setVerified.value ? 'verified' : 'draft',
        frozen: setFrozen.value,
      }),
    })
    if (kb === props.kbId) {
      loadSavedSet(saved)
    }
    await client.invalidateQueries({ queryKey: ['case-sets'] })
  } catch (e) {
    if (kb === props.kbId) error.value = e
  } finally {
    busy.value = false
  }
}
async function fetchCaseSet(set: CaseSet) {
  return set.cases?.length ? set : request<CaseSet>(`/evaluations/case-sets/${set.case_set_id}`)
}
async function previewCaseSet(set: CaseSet) {
  previewSet.value = set
  if (set.cases?.length) return
  previewLoading.value = true
  try {
    const saved = await fetchCaseSet(set)
    if (previewSet.value?.case_set_id === set.case_set_id) previewSet.value = saved
  } catch (e) {
    error.value = e
  } finally {
    previewLoading.value = false
  }
}
async function runLoadedCaseSet(test: boolean) {
  if (!canIngest.value || !test || busy.value || active.value || !boundSet.value || !props.kbId)
    return
  if (
    kind.value === 'rag' &&
    options.value.judge === 'ragas' &&
    !capabilities.data.value?.ragas_available
  ) {
    error.value = new Error('RAGAS 暂不可用，请检查自动评分服务后再运行。')
    return
  }
  if (
    kind.value === 'rag' &&
    !window.confirm(
      `将运行 ${cases.value.length} 道完整 RAG${options.value.judge === 'ragas' ? '及 RAGAS 自动评分' : ''}，可能产生模型调用费用。继续？`,
    )
  )
    return
  await nextTick()
  await start()
}
async function openPendingCaseSet(pending: { id: string; test: boolean }) {
  if (busy.value || active.value) return
  busy.value = true
  error.value = null
  try {
    const saved = await request<CaseSet>(`/evaluations/case-sets/${pending.id}`)
    if (saved.kb_id && saved.kb_id !== props.kbId) return
    loadSavedSet(saved)
    previewSet.value = saved
  } catch (e) {
    error.value = e
    return
  } finally {
    busy.value = false
  }
  await runLoadedCaseSet(pending.test)
}
async function loadCaseSet(set: CaseSet): Promise<boolean> {
  if (busy.value || active.value) return false
  if (set.kb_id && set.kb_id !== props.kbId) {
    pendingCaseSet.value = { id: set.case_set_id, test: false }
    activeKnowledgeBaseId.value = set.kb_id
    return false
  }
  busy.value = true
  error.value = null
  const kb = props.kbId
  try {
    const saved = await fetchCaseSet(set)
    if (kb === props.kbId) {
      loadSavedSet(saved)
      previewSet.value = saved
      return true
    }
    return false
  } catch (e) {
    if (kb === props.kbId) error.value = e
    return false
  } finally {
    busy.value = false
  }
}
async function testCaseSet(set: CaseSet) {
  if (!canIngest.value || busy.value || active.value) return
  if (set.kb_id && set.kb_id !== props.kbId) {
    pendingCaseSet.value = { id: set.case_set_id, test: true }
    activeKnowledgeBaseId.value = set.kb_id
    return
  }
  if (await loadCaseSet(set)) await runLoadedCaseSet(true)
}
async function importCaseSet(file: File) {
  if (!canIngest.value || busy.value || active.value) return
  const kb = props.kbId
  busy.value = true
  error.value = null
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error('测评集文件不能超过 8 MiB。')
    const raw: unknown = JSON.parse(await file.text())
    const cases = parseCases(raw)
    const meta =
      raw && !Array.isArray(raw) && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
    const fallbackName = file.name.replace(/\.json$/i, '').slice(0, 100) || '导入测评集'
    const name = typeof meta.name === 'string' && meta.name.trim() ? meta.name.trim() : fallbackName
    const tags = Array.isArray(meta.tags)
      ? meta.tags
          .filter((tag): tag is string => typeof tag === 'string' && !!tag.trim())
          .map((tag) => tag.trim())
          .slice(0, 10)
      : ['导入']
    const split = meta.split === 'acceptance' ? 'acceptance' : 'development'
    const review_status = meta.review_status === 'verified' ? 'verified' : 'draft'
    const frozen = meta.frozen === true
    const saved = await request<CaseSet>('/evaluations/case-sets', {
      method: 'POST',
      body: JSON.stringify({
        knowledge_base_id: kb,
        name,
        tags,
        cases,
        split,
        review_status,
        frozen,
      }),
    })
    if (kb === props.kbId) {
      previewSet.value = saved
      loadSavedSet(saved)
      await client.invalidateQueries({ queryKey: ['case-sets'] })
    }
  } catch (e) {
    if (kb === props.kbId) error.value = e
  } finally {
    busy.value = false
  }
}
async function exportCaseSet(set: CaseSet) {
  try {
    const saved = await fetchCaseSet(set)
    const safe = saved.name.replace(/[\\/:*?"<>|]/g, '-').slice(0, 80)
    download(`case-set-${safe}-v${saved.version}.json`, JSON.stringify(saved, null, 2))
  } catch (e) {
    error.value = e
  }
}
function refreshCaseSets() {
  void client.invalidateQueries({ queryKey: ['case-sets'] })
}
const history = useQuery({
  queryKey: computed(() => ['evaluations', props.kbId, showArchived.value, historyOffset.value]),
  enabled: computed(() => !!props.kbId),
  queryFn: ({ queryKey }) =>
    request<HistoryPage>(
      `/evaluations/history?knowledge_base_id=${encodeURIComponent(String(queryKey[1]))}&archived=${queryKey[2]}&limit=20&offset=${queryKey[3]}`,
    ),
  refetchInterval: 5000,
})
watch(history.data, (page) => {
  if (
    page &&
    page.offset === historyOffset.value &&
    historyOffset.value >= page.total &&
    historyOffset.value > 0
  ) {
    historyOffset.value = page.total ? Math.floor((page.total - 1) / 20) * 20 : 0
    void client.invalidateQueries({
      queryKey: ['evaluations', props.kbId, showArchived.value, historyOffset.value],
    })
  }
})
const ownHistory = computed(() =>
  (history.data.value?.items || []).filter((r) => r.kb_id === props.kbId),
)
const record = useQuery({
  queryKey: computed(() => ['evaluation', id.value, tag.value]),
  enabled: computed(() => !!id.value),
  queryFn: ({ queryKey }) =>
    request<EvalRecord>(
      `/evaluations/${queryKey[1]}?tag=${encodeURIComponent(String(queryKey[2]))}`,
    ),
  refetchInterval: (q) => (q.state.data?.status === 'running' ? 1000 : false),
})
const current = computed(() =>
  record.data.value?.request.knowledge_base_id === props.kbId ? record.data.value : undefined,
)
const comparisonHistory = computed(() => {
  const r = current.value
  return r && !ownHistory.value.some((item) => item.evaluation_id === r.evaluation_id)
    ? [
        ...ownHistory.value,
        {
          evaluation_id: r.evaluation_id,
          status: r.status,
          created_at: r.created_at,
          kind: r.request.kind,
          kb_id: props.kbId,
        },
      ]
    : ownHistory.value
})
const active = computed(() => current.value?.status === 'running')
const availableTags = computed(() => [
  ...new Set(current.value?.request.cases.flatMap((c) => c.tags || []) || []),
])
const visibleResults = computed(() =>
  (current.value?.results || [])
    .map((result, index) => ({ result, index }))
    .filter(({ result }) => !tag.value || result.case.tags?.includes(tag.value)),
)
watch(visibleResults, (rows) => {
  if (!rows.some((r) => r.index === selectedIndex.value)) selectedIndex.value = rows[0]?.index ?? 0
})
const selected = computed(() =>
  visibleResults.value.some((r) => r.index === selectedIndex.value)
    ? current.value?.results[selectedIndex.value]
    : undefined,
)
const reviewKey = computed(() => `${id.value}:${selectedIndex.value}`)
const { draft: dimensions, saved: reviewSaved } = useReviewDraft(reviewKey, selected)
const retrievalLabels: (keyof Scores)[] = ['precision', 'recall', 'mrr']
const retrievalScore = (
  result: { status: string; case: EvalCase; metrics?: Scores | null },
  key: keyof Scores,
) => {
  if (result.case.expected !== 'answerable' || result.case.judgment_scope === 'partial')
    return '不参与'
  if (result.status === 'failed')
    return result.metrics ? `${percent(result.metrics[key])}（任务失败）` : '失败（无检索结果，计零）'
  if (result.status !== 'completed') return '—'
  return percent(result.metrics?.[key])
}
const answerMark = (value: string | undefined, positive: string) => {
  if (!value || value === 'pending') return '待审'
  if (value === 'na') return '不适用'
  if (value === positive) return '✓'
  if (value === 'partial') return '部分'
  return '✗'
}
// Provenance is shown next to the form: an AI proxy review or a carried review is not a
// human sign-off, and the difference must stay visible after the review is saved.
const reviewOrigin = (review?: AnswerReview) => {
  if (!review?.review_method) return ''
  const who = [review.reviewer, review.reviewed_at].filter(Boolean).join(' · ')
  if (review.review_method === 'human') return `人工审核${who ? ` · ${who}` : ''}`
  if (review.review_method === 'ai_proxy')
    return `AI 代审${who ? ` · ${who}` : ''} · 非人工签收，导出前请复核`
  const source = review.carried_from ? `（源自 ${review.carried_from}）` : ''
  return `沿用同一案例集既往判定${source} · 回答文本与来源运行完全一致${who ? ` · 原审核 ${who}` : ''}`
}
watch(
  () => props.kbId,
  () => {
    id.value = localStorage.getItem(`rag-evaluation:${props.kbId}`) || ''
    cases.value = [blank()]
    setId.value = ''
    setName.value = ''
    setTags.value = ''
    loadedCases.value = ''
    scope.value = []
    scoped.value = false
    selectedIndex.value = 0
    source.value = null
    error.value = null
    pending = undefined
    tag.value = ''
    historyOffset.value = 0
    showArchived.value = false
    kind.value = 'rag'
    judgeChoiceExplicit.value = false
    options.value = {
      rerank: 'inherit',
      context_tokens: null,
      judge: capabilities.data.value?.ragas_available === false ? 'none' : 'ragas',
    }
    setSplit.value = 'development'
    setVerified.value = false
    setFrozen.value = false
    previewSet.value = null
    previewLoading.value = false
    const deferred = pendingCaseSet.value
    pendingCaseSet.value = null
    if (deferred) void openPendingCaseSet(deferred)
  },
  { immediate: true },
)
watch(id, (value) => {
  if (value) localStorage.setItem(`rag-evaluation:${props.kbId}`, value)
  else localStorage.removeItem(`rag-evaluation:${props.kbId}`)
  tag.value = ''
  retryPlan.value = undefined
  confirmUnknown.value = false
  retryKey = undefined
  selectedIndex.value = 0
})
watch(ownHistory, (rows) => {
  if (!id.value) {
    const running = rows.find((r) => r.status === 'running')
    if (running) id.value = running.evaluation_id
  }
})
const status = (value: string) => evaluationStatus[value] || value
const percent = (value: number | null | undefined) => formatPercent(value, '未标注')
function fileName(fileId: string) {
  return (
    current.value?.snapshot.files.find((f) => f.file_id === fileId)?.name ||
    files.data.value?.find((f) => f.file_id === fileId)?.name ||
    fileId
  )
}
function loadConfiguration() {
  const r = current.value
  if (!r || busy.value || active.value) return
  kind.value = r.request.kind
  mode.value = r.request.mode
  options.value = { rerank: 'inherit', context_tokens: null, judge: 'none', ...r.request.options }
  judgeChoiceExplicit.value = true
  k.value = r.request.k
  scoped.value = r.request.file_ids !== null
  scope.value = [...(r.request.file_ids || [])]
  cases.value = normalizeCases(r.request.cases)
  const saved = r.snapshot.case_set
  if (r.request.case_set_id && saved)
    loadSavedSet({
      ...saved,
      case_set_id: r.request.case_set_id,
      tags: saved.tags || [],
      cases: r.request.cases,
    })
  else {
    setId.value = ''
    loadedCases.value = ''
  }
  pending = undefined
}
function acceptRecord(response: EvalRecord) {
  client.setQueryData(['evaluation', response.evaluation_id, ''], response)
  void client.invalidateQueries({ queryKey: ['evaluation', response.evaluation_id] })
}
async function manageRecord(action: 'archive' | 'delete') {
  if (!canIngest.value || busy.value || active.value || !current.value) return
  const target = current.value,
    kb = props.kbId
  if (
    action === 'delete' &&
    !window.confirm('删除此评测及其专用会话？删除后无法从历史恢复。复用结果保存在关联实验中。')
  )
    return
  busy.value = true
  error.value = null
  try {
    if (action === 'delete')
      await request(`/evaluations/${target.evaluation_id}`, { method: 'DELETE' })
    else
      await request(`/evaluations/${target.evaluation_id}/archive`, {
        method: 'PATCH',
        body: JSON.stringify({ archived: !target.archived }),
      })
    client.removeQueries({ queryKey: ['evaluation', target.evaluation_id] })
    if (kb === props.kbId) comparisonEpoch.value++
    if (kb === props.kbId && id.value === target.evaluation_id) id.value = ''
    await client.invalidateQueries({ queryKey: ['evaluations'] })
  } catch (e) {
    if (kb === props.kbId) error.value = e
  } finally {
    busy.value = false
  }
}
async function prepareRetry() {
  if (!id.value || active.value || busy.value) return
  const target = id.value
  try {
    const plan = await request<typeof retryPlan.value>(`/evaluations/${target}/retry-plan`)
    if (id.value === target) {
      retryPlan.value = plan
      confirmUnknown.value = false
    }
  } catch (e) {
    if (id.value === target) error.value = e
  }
}
async function retry() {
  if (!canIngest.value || !id.value || !retryPlan.value || busy.value || active.value) return
  const target = id.value,
    kb = props.kbId
  if (!retryKey || retryKey.parent !== target || retryKey.confirm !== confirmUnknown.value)
    retryKey = { parent: target, confirm: confirmUnknown.value, key: crypto.randomUUID() }
  busy.value = true
  error.value = null
  try {
    const response = await request<EvalRecord>(`/evaluations/${target}/retry`, {
      method: 'POST',
      body: JSON.stringify({
        idempotency_key: retryKey.key,
        confirm_unknown_calls: confirmUnknown.value,
      }),
    })
    acceptRecord(response)
    if (props.kbId === kb && id.value === target) id.value = response.evaluation_id
    await client.invalidateQueries({ queryKey: ['evaluations'] })
  } catch (e) {
    if (kb === props.kbId) error.value = e
  } finally {
    busy.value = false
  }
}
async function exportReport() {
  const target = id.value
  try {
    const report = await request<{ markdown: string }>(
      `/evaluations/${target}/report?tag=${encodeURIComponent(tag.value)}`,
    )
    download(`rag-acceptance-${target}.md`, report.markdown, 'text/markdown;charset=utf-8')
  } catch (e) {
    if (id.value === target) error.value = e
  }
}
async function start() {
  if (!canIngest.value || busy.value || active.value) return
  const kbId = props.kbId
  busy.value = true
  error.value = null
  const payload = {
    knowledge_base_id: kbId,
    kind: kind.value,
    mode: mode.value,
    k: k.value,
    file_ids: scoped.value ? [...scope.value] : null,
    cases: cases.value,
    case_set_id: boundSet.value,
    options: options.value,
  }
  const signature = JSON.stringify(payload)
  if (!pending || pending.signature !== signature) pending = { signature, key: crypto.randomUUID() }
  try {
    const response = await request<EvalRecord>('/evaluations', {
      method: 'POST',
      body: JSON.stringify({ ...payload, idempotency_key: pending.key }),
    })
    acceptRecord(response)
    if (props.kbId === kbId) {
      id.value = response.evaluation_id
      selectedIndex.value = 0
    }
    pending = undefined
    await client.invalidateQueries({ queryKey: ['evaluations'] })
  } catch (e) {
    if (props.kbId === kbId) error.value = e
  } finally {
    busy.value = false
  }
}
async function stop() {
  if (!canIngest.value || !id.value || busy.value) return
  const target = id.value
  busy.value = true
  try {
    const response = await request<EvalRecord>(`/evaluations/${target}/stop`, { method: 'POST' })
    acceptRecord(response)
  } catch (e) {
    if (id.value === target) error.value = e
  } finally {
    busy.value = false
  }
}
async function review() {
  if (!canIngest.value || !selected.value || !id.value || active.value || busy.value) return
  const target = id.value,
    index = selectedIndex.value,
    kb = props.kbId
  const submitted = JSON.parse(JSON.stringify(dimensions.value))
  busy.value = true
  try {
    const response = await request<EvalRecord>(`/evaluations/${target}/reviews/${index}`, {
      method: 'PATCH',
      body: JSON.stringify(submitted),
    })
    reviewSaved(`${target}:${index}`, submitted, response.results[index]!.review)
    acceptRecord(response)
  } catch (e) {
    if (kb === props.kbId && id.value === target) error.value = e
  } finally {
    busy.value = false
  }
}
async function importCases(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const kb = props.kbId
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error('案例文件不能超过 8 MiB。')
    const parsed = parseCases(JSON.parse(await file.text()))
    if (kb === props.kbId) cases.value = parsed
  } catch (e) {
    if (kb === props.kbId) error.value = e
  } finally {
    ;(event.target as HTMLInputElement).value = ''
  }
}
async function exportJson() {
  const target = id.value
  try {
    const r = await request<EvalRecord>(`/evaluations/${target}`)
    download(`evaluation-${target}.json`, JSON.stringify(r, null, 2))
  } catch (e) {
    if (id.value === target) error.value = e
  }
}
function exportCsv() {
  if (!current.value) return
  const cell = (v: unknown) => {
    let text = String(v ?? '')
    if (/^[=+@\-\t\r]/.test(text)) text = "'" + text
    return '"' + text.replace(/"/g, '""') + '"'
  }
  const rows = [
    [
      '问题',
      '状态',
      '预期',
      '精准率',
      '召回率',
      'MRR',
      'nDCG',
      '忠实度',
      '答案相关性',
      '正确性',
      '完整性',
      'RAGAS忠实度',
      'RAGAS答案相关性',
      'RAGAS正确性',
      'RAGAS完整性',
      '耗时毫秒',
      '人工评价',
      '回答',
    ],
    ...visibleResults.value.map(({ result: r }) => [
      r.case.query,
      r.status,
      r.case.expected,
      r.metrics?.precision,
      r.metrics?.recall,
      r.metrics?.mrr,
      r.metrics?.ndcg,
      r.review?.unsupported_claims === 'no' ? 1 : r.review?.unsupported_claims === 'yes' ? 0 : null,
      r.review?.answer_relevancy === 'relevant'
        ? 1
        : ['partial', 'irrelevant'].includes(r.review?.answer_relevancy || '')
          ? 0
          : null,
      r.review?.verdict === 'pass'
        ? 1
        : ['partial', 'fail'].includes(r.review?.verdict || '')
          ? 0
          : null,
      r.review?.fact_coverage === 'full'
        ? 1
        : ['partial', 'none'].includes(r.review?.fact_coverage || '')
          ? 0
          : null,
      r.judge?.faithfulness,
      r.judge?.answer_relevancy,
      r.judge?.answer_correctness,
      r.judge?.completeness,
      r.elapsed_ms,
      r.review.verdict,
      r.run?.answer,
    ]),
  ]
  download(
    `evaluation-${id.value}.csv`,
    '\ufeff' + rows.map((r) => r.map(cell).join(',')).join('\r\n'),
    'text/csv;charset=utf-8',
  )
}
async function saveBadCases() {
  const report = current.value
  if (!canIngest.value || !report || busy.value || active.value) return
  const lowRagas = (result: (typeof report.results)[number]) => {
    const judge = result.judge
    if (!judge) return false
    return [
      judge.faithfulness,
      judge.answer_relevancy,
      judge.answer_correctness,
      judge.completeness,
    ].some((score) => typeof score === 'number' && score < 0.7)
  }
  const bad = report.results.filter(
    (result) =>
      ['failed', 'cancelled'].includes(result.status) ||
      ['fail', 'partial'].includes(result.review?.verdict || '') ||
      (result.metrics?.recall != null && result.metrics.recall < 0.5) ||
      lowRagas(result),
  )
  if (!bad.length) {
    error.value = new Error('当前实验没有失败、部分正确或低分的 bad case 可沉淀。')
    return
  }
  const cases = bad.slice(0, 20).map((result) => result.case)
  const base = report.snapshot.case_set?.name || '评测'
  const stamp = new Date().toISOString().slice(0, 10)
  busy.value = true
  error.value = null
  try {
    const saved = await request<CaseSet>('/evaluations/case-sets', {
      method: 'POST',
      body: JSON.stringify({
        knowledge_base_id: props.kbId,
        name: `${base}-badcases-${stamp}`,
        tags: ['badcase', 'regression'],
        cases,
        split: 'development',
        review_status: 'draft',
        frozen: false,
      }),
    })
    previewSet.value = saved
    loadSavedSet(saved)
    await client.invalidateQueries({ queryKey: ['case-sets'] })
    if (bad.length > 20) window.alert('bad case 超过 20 道，已先保存前 20 道；请分批沉淀。')
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
const trace = computed(() => selected.value?.diagnostics?.trace)
function sourceBody(chunkId: string) {
  const r = selected.value
  if (!r || !chunkId) return ''
  const pools = [...(r.sources || []), ...(r.context_sources || []), ...(r.run?.sources || [])]
  for (const item of pools) {
    if (item.chunk_id === chunkId && item.content) return item.content
    for (const chunk of item.evidence_chunks || []) {
      if (chunk.chunk_id === chunkId && chunk.content) return chunk.content
    }
  }
  return ''
}
const evidenceRows = computed(() => {
  const r = selected.value
  if (!r) return []
  const before = trace.value?.before_rerank || r.sources
  const after = trace.value?.after_rerank || r.sources
  const context = new Set(
    (r.context_sources || []).flatMap((s) => [
      s.chunk_id,
      ...(s.evidence_chunks || []).map((c) => c.chunk_id),
    ]),
  )
  const cited = new Set(
    (r.run?.sources || []).flatMap((s) => [
      s.chunk_id,
      ...(s.evidence_chunks || []).map((c) => c.chunk_id),
    ]),
  )
  return before.map((s, i) => ({
    source: { ...s, content: s.content || sourceBody(s.chunk_id) },
    before: i + 1,
    after: after.findIndex((a) => a.chunk_id === s.chunk_id) + 1,
    context: context.has(s.chunk_id),
    cited: cited.has(s.chunk_id),
  }))
})
const timing = computed(() => {
  const r = selected.value
  const rows = Object.entries(r?.diagnostics?.stage_ms || {}).filter(([key]) => key !== 'total')
  if (typeof r?.assembly?.duration_ms === 'number') rows.push(['assemble', r.assembly.duration_ms])
  for (const event of r?.token_usage || [])
    if (typeof event.duration_ms === 'number') rows.push([event.stage, event.duration_ms])
  return rows
})
const maxTime = computed(() => Math.max(1, ...timing.value.map(([, v]) => v)))
</script>
<template>
  <p v-if="!canIngest" class="notice">
    当前账号仅可查看评测记录与测评集；运行评测、导入题集和保存评价需要员工或管理员权限。
  </p>
  <ErrorBanner :error="caseSets.error.value" />
  <CaseSetLibrary
    :sets="caseSets.data.value || []"
    :current-kb-id="kbId"
    :preview="previewSet"
    :busy="busy"
    :active="active"
    :preview-loading="previewLoading"
    :can-write="canIngest"
    :show-archived="showArchivedSets"
    @preview="previewCaseSet"
    @load="loadCaseSet"
    @test="testCaseSet"
    @archive="archiveCaseSet"
    @toggle-archived="showArchivedSets = !showArchivedSets"
    @import="importCaseSet"
    @refresh="refreshCaseSets"
    @export="exportCaseSet"
    @close-preview="previewSet = null"
  />
  <section class="card eval-quick">
    <div class="eval-quick-copy">
      <h2>快速评测</h2>
      <p class="small muted">
        选择一个已保存的测评集直接运行；改题、标注和高级参数在下方“自定义配置”里。
      </p>
    </div>
    <template v-if="quickSets.length">
      <label class="field"
        >测评集<select v-model="quickSetId" :disabled="busy || active" aria-label="快速评测测评集">
          <option v-for="set in quickSets" :key="set.case_set_id" :value="set.case_set_id">
            {{ set.name }} · v{{ set.version }}
          </option>
        </select></label
      >
      <label class="field"
        >运行方式<select v-model="kind" :disabled="busy || active" aria-label="快速评测运行方式">
          <option value="retrieval">仅检索</option>
          <option value="rag">完整 RAG</option>
        </select></label
      >
      <label class="field"
        >检索方式<select v-model="mode" :disabled="busy || active" aria-label="快速评测检索方式">
          <option v-for="(name, value) in modeLabels" :key="value" :value="value">
            {{ name }}
          </option>
        </select></label
      >
      <label v-if="kind === 'rag'" class="field"
        >生成层评分<select
          v-model="options.judge"
          :disabled="busy || active"
          aria-label="快速评测生成层评分"
          @change="judgeChoiceExplicit = true"
        >
          <option value="ragas" :disabled="!capabilities.data.value?.ragas_available">
            RAGAS 自动评分
          </option>
          <option value="none">稍后人工审核</option>
        </select></label
      >
      <p v-if="kind === 'rag' && !capabilities.data.value?.ragas_available" class="small muted">
        RAGAS 当前不可用；选择人工审核时，本次评测不会自动生成回答质量分数。
      </p>
      <button
        class="button primary"
        type="button"
        :disabled="!canIngest || busy || active || !quickSetId"
        @click="quickRun"
      >
        {{ active ? '评测执行中' : busy ? '提交中…' : '一键评测' }}
      </button>
    </template>
    <p v-else class="small muted">
      当前知识库还没有测评集；在下方填写案例并保存后，这里会出现一键入口。
    </p>
  </section>
  <div class="eval-layout">
    <form class="card eval-config" @submit.prevent="start">
      <h2>自定义配置</h2>
      <p class="small muted">最多 20 题、逐题运行。完整 RAG 会调用回答模型。</p>
      <p
        v-if="kind === 'rag' && capabilities.data.value?.max_model_calls_per_case"
        class="small muted"
      >
        本批应用层模型调用上限
        {{ cases.length * capabilities.data.value.max_model_calls_per_case }} 次；不含
        Embedding、重排与供应商内部重试。实际用量在结果中展示。
      </p>
      <fieldset class="eval-write-controls" :disabled="!canIngest">
        <label class="field"
          >运行方式<select v-model="kind" aria-label="评测运行方式" :disabled="busy || active">
            <option value="retrieval">仅检索</option>
            <option value="rag">完整 RAG</option>
          </select></label
        >
        <label class="field"
          >检索方式<select v-model="mode" aria-label="评测检索方式" :disabled="busy || active">
            <option v-for="(name, value) in modeLabels" :key="value" :value="value">
              {{ name }}
            </option>
          </select></label
        >
        <details class="eval-experiment-options">
          <summary>受控实验参数</summary>
          <label class="field"
            >重排<select v-model="options.rerank" aria-label="实验重排" :disabled="busy || active">
              <option value="inherit">跟随工作空间</option>
              <option value="on">开启（须已配置）</option>
              <option value="off">关闭</option>
            </select></label
          >
          <label v-if="kind === 'rag'" class="field"
            >资料 Token 预算<select
              v-model="options.context_tokens"
              aria-label="实验上下文预算"
              :disabled="busy || active"
            >
              <option :value="null">跟随工作空间</option>
              <option v-for="n in [512, 1024, 2048, 4096, 8192]" :key="n" :value="n">
                {{ n }}
              </option>
            </select></label
          >
          <label v-if="kind === 'rag'" class="field"
            >生成层评分<select
              v-model="options.judge"
              aria-label="生成层评分方式"
              :disabled="busy || active"
              @change="judgeChoiceExplicit = true"
            >
              <option value="none">人工审核（不自动评分）</option>
              <option value="ragas" :disabled="!capabilities.data.value?.ragas_available">
                RAGAS LLM-as-judge
              </option>
            </select></label
          >
          <p v-if="kind === 'rag' && !capabilities.data.value?.ragas_available" class="small muted">
            RAGAS worker 未安装或不可用；在 tools/ragas-worker 执行 uv sync 后可用 LLM
            语义评分。默认人工审核仍然有效。
          </p>
          <p class="small muted">
            只作用于本次实验，实际输入仍受模型窗口及回答/修复预留限制。开启重排不会替你配置模型。
          </p>
        </details>
        <label class="field"
          >指标 K<input
            v-model.number="k"
            type="number"
            min="1"
            max="20"
            required
            :disabled="busy || active"
            aria-label="指标 K"
        /></label>
        <p class="small muted">
          K K
          仅用于文件级指标；检索与问答都按问答候选配置限制片段数，再对返回片段按文件去重。只有完整标注参与质量均值，部分标注覆盖率不是完整召回率或其下界。
        </p>
        <label
          ><input v-model="scoped" type="checkbox" :disabled="busy || active" />
          限定检索文件范围</label
        >
        <div v-if="scoped" class="file-options">
          <label v-for="f in files.data.value" :key="f.file_id"
            ><input
              v-model="scope"
              type="checkbox"
              :value="f.file_id"
              :disabled="busy || active"
            />{{ f.name }}</label
          >
          <p v-if="!scope.length" class="small muted">范围为空，不会回退到全库。</p>
        </div>
        <details class="eval-case-sets">
          <summary>保存当前草稿为测评集</summary>
          <p class="small muted">
            从上方“测评集”区域查看或载入已有版本；这里保存当前编辑中的案例为新版本。
          </p>
          <label class="field"
            >名称<input
              v-model="setName"
              aria-label="案例集名称"
              maxlength="100"
              :disabled="busy || active"
          /></label>
          <label class="field"
            >业务标签<input
              v-model="setTags"
              aria-label="案例集标签"
              placeholder="年假, 人事"
              :disabled="busy || active"
          /></label>
          <label class="field"
            >集合用途<select v-model="setSplit" aria-label="案例集用途" :disabled="busy || active">
              <option value="development">开发调试</option>
              <option value="acceptance">验收测试</option>
            </select></label
          >
          <label
            ><input
              v-model="setVerified"
              type="checkbox"
              :disabled="busy || active"
            />案例已由人工核验</label
          >
          <label
            ><input
              v-model="setFrozen"
              type="checkbox"
              :disabled="busy || active || !setVerified"
            />标记为冻结版本（须完整标注及必要原文）</label
          >
          <button
            type="button"
            class="button"
            :disabled="!setName.trim() || busy || active || cases.some((c) => !c.query.trim())"
            @click="saveSet"
          >
            保存案例集版本
          </button>
          <p class="small muted">
            {{
              boundSet ? '已绑定保存版本。' : '当前草稿未绑定版本。'
            }}修改后保存将生成新版本；已有版本保持不变。每版最多 20 题。
          </p>
        </details>
        <div class="eval-case-list">
          <fieldset v-for="(c, i) in cases" :key="i" :disabled="busy || active">
            <legend>案例 {{ i + 1 }}</legend>
            <textarea
              v-model="c.query"
              rows="3"
              required
              maxlength="4000"
              :aria-label="`案例 ${i + 1} 问题`"
              placeholder="输入你希望验证的问题"
            />
            <label class="field"
              >预期<select
                v-model="c.expected"
                :aria-label="`案例 ${i + 1} 预期`"
                @change="resetLabels(c)"
              >
                <option value="unlabelled">未标注（仅观察）</option>
                <option value="answerable">资料中有答案</option>
                <option value="unanswerable">应资料不足</option>
              </select></label
            >
            <div v-if="c.expected === 'answerable'" class="file-options">
              <strong class="small">相关文件（必选）</strong
              ><label v-for="f in files.data.value" :key="f.file_id"
                ><input
                  v-model="c.relevant_file_ids"
                  type="checkbox"
                  :value="f.file_id"
                  @change="
                    c.negative_file_ids =
                      c.negative_file_ids?.filter((id) => !c.relevant_file_ids.includes(id)) || []
                  "
                />{{ f.name }}</label
              >
            </div>
            <details class="eval-advanced-label">
              <summary>高级标注（可选）</summary>
              <label class="field"
                >业务标签<input
                  :value="(c.tags || []).join(', ')"
                  :aria-label="`案例 ${i + 1} 业务标签`"
                  placeholder="同场景同标签；逗号分隔"
                  maxlength="800"
                  @change="
                    c.tags = ($event.target as HTMLInputElement).value
                      .split(/[,，]/)
                      .map((t) => t.trim())
                      .filter(Boolean)
                  "
              /></label>
              <label class="field"
                >标注范围<select v-model="c.judgment_scope" :aria-label="`案例 ${i + 1} 标注范围`">
                  <option value="full">完整（已穷尽相关文件）</option>
                  <option value="partial">部分（其他文件仍待审核）</option>
                </select></label
              >
              <details v-if="c.judgment_scope === 'partial'">
                <summary>明确不相关的文件</summary>
                <div class="file-options">
                  <label v-for="f in files.data.value" :key="f.file_id"
                    ><input
                      v-model="c.negative_file_ids"
                      type="checkbox"
                      :value="f.file_id"
                      :disabled="c.relevant_file_ids.includes(f.file_id)"
                    />{{ f.name }}</label
                  >
                </div>
              </details>
              <EvidenceEditor
                v-if="c.expected === 'answerable' && c.evidence_groups"
                v-model="c.evidence_groups"
                :files="
                  (corpus.data.value || []).filter((f) => c.relevant_file_ids.includes(f.file_id))
                "
              />
              <details>
                <summary>答案人工审核要点与参考答案</summary>
                <label class="field"
                  >审核要点<textarea
                    v-model="c.required_facts"
                    maxlength="2000"
                    rows="2"
                    placeholder="必须回答的事实、限制条件或应拒答原因"
                    :aria-label="`案例 ${i + 1} 审核要点`"
                  />
                </label>
                <label class="field"
                  >参考答案<textarea
                    v-model="c.reference_answer"
                    maxlength="4000"
                    rows="2"
                    placeholder="RAGAS 正确性和完整性评分使用；可留空"
                    :aria-label="`案例 ${i + 1} 参考答案`"
                  />
                </label>
              </details>
            </details>
            <button
              v-if="cases.length > 1"
              type="button"
              class="text-button"
              @click="cases.splice(i, 1)"
            >
              移除此题
            </button>
          </fieldset>
        </div>
        <div class="eval-actions">
          <button
            class="button"
            type="button"
            :disabled="busy || active || cases.length >= 20"
            @click="cases.push(blank())"
          >
            添加案例</button
          ><button
            class="text-button"
            type="button"
            :disabled="busy || active"
            @click="importing?.click()"
          >
            导入 JSON</button
          ><button
            class="text-button"
            type="button"
            @click="download('evaluation-cases.json', JSON.stringify(cases, null, 2))"
          >
            导出案例
          </button>
        </div>
        <input
          ref="importing"
          type="file"
          accept=".json"
          class="sr-only"
          aria-label="导入评测案例"
          @change="importCases"
        />
        <button
          v-if="canIngest"
          class="button primary full"
          :disabled="
            busy ||
            active ||
            !kbId ||
            cases.some(
              (c) =>
                !c.query.trim() || (c.expected === 'answerable' && !c.relevant_file_ids.length),
            )
          "
        >
          {{ busy ? '提交中…' : active ? '评测执行中' : '开始评测' }}
        </button>
        <button
          v-if="active && canIngest"
          type="button"
          class="button full"
          :disabled="busy"
          @click="stop"
        >
          停止评测
        </button>
        <ErrorBanner :error="files.error.value || corpus.error.value" />
      </fieldset>
    </form>
    <section class="eval-results">
      <ErrorBanner :error="error || record.error.value || history.error.value" />
      <div class="card eval-history">
        <label
          ><input
            v-model="showArchived"
            type="checkbox"
            aria-label="查看已归档评测"
          />查看已归档评测</label
        >
        <label class="field"
          >评测记录<select v-model="id" aria-label="评测记录">
            <option value="">选择历史记录</option>
            <option v-for="r in ownHistory" :key="r.evaluation_id" :value="r.evaluation_id">
              {{ date(r.created_at) }} · {{ r.kind === 'rag' ? '完整 RAG' : '检索' }} ·
              {{ status(r.status) }}
            </option>
          </select></label
        >
        <div class="eval-actions">
          <button
            class="button"
            :disabled="historyOffset === 0"
            @click="historyOffset = Math.max(0, historyOffset - 20)"
          >
            上一页评测</button
          ><span
            >共 {{ history.data.value?.total || 0 }} 条 · 第
            {{ Math.floor(historyOffset / 20) + 1 }} 页</span
          ><button
            class="button"
            :disabled="historyOffset + 20 >= (history.data.value?.total || 0)"
            @click="historyOffset += 20"
          >
            下一页评测
          </button>
        </div>
      </div>
      <ExperimentComparison
        :key="`${kbId}:${comparisonEpoch}`"
        :current-id="id"
        :history="comparisonHistory"
      />
      <template v-if="current">
        <div class="result-heading">
          <div>
            <h2>
              {{ status(current.status) }} ·
              {{
                current.results.filter((r) =>
                  ['completed', 'failed', 'cancelled'].includes(r.status),
                ).length
              }}/{{ current.request.cases.length }}
              题
            </h2>
            <p>
              所选记录：{{ modeLabels[current.request.mode] }} ·
              {{ current.request.kind === 'rag' ? '完整 RAG' : '仅检索' }} · K={{
                current.request.k
              }}
            </p>
          </div>
          <div class="eval-actions">
            <button class="button" :disabled="busy || active" @click="loadConfiguration">
              载入此配置
            </button>
            <button class="button" :disabled="active" @click="exportReport">导出验收报告</button>
            <button
              v-if="canIngest"
              class="button"
              :disabled="busy || active"
              @click="prepareRetry"
            >
              检查失败题重试
            </button>
            <button
              v-if="canIngest"
              class="button"
              :disabled="busy || active"
              @click="manageRecord('archive')"
            >
              {{ current.archived ? '取消归档' : '归档评测' }}
            </button>
            <button
              v-if="canIngest"
              class="button danger"
              :disabled="busy || active"
              @click="manageRecord('delete')"
            >
              删除评测
            </button>
            <button class="button" @click="exportJson">导出 JSON</button
            ><button class="button" @click="exportCsv">导出 CSV</button>
            <button
              v-if="canIngest"
              class="button"
              :disabled="busy || active"
              @click="saveBadCases"
            >
              沉淀 bad case
            </button>
          </div>
        </div>
        <ErrorBanner :error="current.error" />
        <div v-if="retryPlan && canIngest" class="card eval-retry">
          <h3>重试计划</h3>
          <p>
            复用 {{ retryPlan.reuse_count }} 题；重试
            {{ retryPlan.retry_indices.map((i) => i + 1).join('、') || '无' }}。
          </p>
          <label v-if="retryPlan.unknown_indices.length"
            ><input v-model="confirmUnknown" type="checkbox" />第
            {{ retryPlan.unknown_indices.map((i) => i + 1).join('、') }}
            题调用结果未知，我确认重试可能再次计费。</label
          ><button
            class="button"
            :disabled="
              busy ||
              !retryPlan.retry_indices.length ||
              (retryPlan.unknown_indices.length > 0 && !confirmUnknown)
            "
            @click="retry"
          >
            创建重试评测
          </button>
        </div>
        <p v-if="current.request.retry_of" class="notice warning">
          本记录关联重试自 {{ current.request.retry_of }}。复用题有来源标记，不计入本次执行用量。
        </p>
        <label class="field"
          >按业务标签筛选<select v-model="tag" aria-label="评测业务标签筛选">
            <option value="">全部案例</option>
            <option v-for="t in availableTags" :key="t" :value="t">{{ t }}</option>
          </select></label
        >
        <section class="eval-metric-section">
          <h3>检索层指标</h3>
          <div class="eval-metrics">
            <div v-for="key in retrievalLabels" :key="key" class="card">
              <small>{{ metricLabels[key] }}@{{ current.request.k }}</small
              ><strong>{{ percent(current.summary.metrics[key]) }}</strong>
            </div>
          </div>
          <p class="small muted">
            指标分母：{{ current.summary.metric_queries }} 道已执行完整有答案标注题（含
            {{ current.summary.labelled_failures }}
            道失败，其中 {{ current.summary.retrieval_scored_failures }} 道保留检索结果；其余失败题无可用检索分数，计零）。部分标注
            {{ current.summary.partial_label_queries || 0 }}
            题；部分、未标注、应拒答和取消题不参与检索质量均值。命中率与 nDCG
            保留在图表数值表中；命中率衡量是否找到至少一个相关文件，不等于答案正确率。
          </p>
        </section>
        <p v-if="current.generation?.errors" class="notice warning">
          RAGAS 评分失败
          {{ current.generation.errors }} 题。请检查可选依赖、模型配置或服务状态；失败题不会显示为 0
          分。
        </p>
        <p
          v-if="
            current.status === 'running' &&
            current.request.options?.judge === 'ragas' &&
            current.results.every((r) => r.status !== 'pending' && r.status !== 'running')
          "
          class="notice"
        >
          RAGAS 判定中：已完成
          {{ current.results.filter((r) => r.judge_status && r.judge_status !== 'running').length }}
          / {{ current.results.filter((r) => r.judge_status).length }} 题；已有结果会分批保存。
        </p>
        <AnswerQualityChart
          v-if="
            current.quality && (current.request.kind === 'rag' || current.generation?.available)
          "
          :quality="current.quality"
          :generation="current.generation"
        />
        <details
          v-if="current.quality && current.request.kind === 'rag'"
          class="card eval-quality-details"
        >
          <summary>人工审核明细与诊断</summary>
          <QualityOverview :quality="current.quality" />
          <LayerDiagnosis
            :retrieval="current.summary.metrics"
            :human="current.quality"
            :generation="current.generation"
            :show-generation="current.request.kind === 'rag' || current.generation?.available"
          />
        </details>
        <p
          v-if="
            !current.snapshot.case_set || current.snapshot.case_set.review_status !== 'verified'
          "
          class="notice warning"
        >
          本实验未绑定人工核验案例集，当前结果仅用于调试，不能作为业务签收结论。
        </p>
        <p v-if="current.snapshot.case_set" class="small muted">
          案例集 {{ current.snapshot.case_set.name }} · v{{ current.snapshot.case_set.version }} ·
          {{ current.snapshot.case_set.split === 'acceptance' ? '验收' : '开发' }} ·
          {{ current.snapshot.case_set.frozen ? '已冻结' : '未冻结' }} ·
          {{ current.snapshot.case_set.review_status === 'verified' ? '人工已核验' : '草稿待核验' }}
        </p>
        <div class="eval-outcomes card">
          <span>完成 {{ current.summary.completed }}</span
          ><span>失败 {{ current.summary.failed }}</span
          ><span>取消 {{ current.summary.cancelled }}</span
          ><span
            >应拒答且返回资料不足 {{ current.summary.refusal_correct }}/{{
              current.summary.refusal_total
            }}</span
          ><span
            >有答案却资料不足 {{ current.summary.false_refusals }}/{{
              current.summary.answerable_total
            }}</span
          ><span
            >必要证据完整进入上下文 {{ current.summary.context_evidence_complete || 0 }}/{{
              current.summary.context_evidence_queries || 0
            }}</span
          ><span v-if="current.request.kind === 'rag'"
            >整体拒答成功 {{ current.summary.refusal_correct }}/{{
              current.summary.refusal_attempted ?? current.summary.refusal_total
            }}（含服务失败）</span
          ><span v-if="current.request.kind === 'rag'">人工已评 {{ current.summary.reviewed }}</span
          ><span v-if="current.quality && current.request.kind === 'rag'"
            >人工通过率 {{ formatPercent(current.quality.pass_rate) }}</span
          >
        </div>
        <section class="card eval-chart-card">
          <MetricChart :curves="current.summary.curves" />
        </section>
        <div class="card table-scroll">
          <table class="eval-table">
            <thead>
              <tr>
                <th>案例</th>
                <th>状态</th>
                <th>精准率@{{ current.request.k }}</th>
                <th>召回率@{{ current.request.k }}</th>
                <th>MRR@{{ current.request.k }}</th>
                <th>回答质量（忠/正/完）</th>
                <th>耗时</th>
                <th>Token（实际）</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="{ result: r, index: i } in visibleResults"
                :key="i"
                :class="{ 'eval-selected': selectedIndex === i }"
              >
                <td>
                  <button class="text-button" @click="selectedIndex = i">
                    {{ i + 1 }}. {{ r.case.query }}
                  </button>
                </td>
                <td>
                  {{ status(r.status) }}<span v-if="r.reused_from" class="badge muted">复用</span>
                </td>
                <td>{{ retrievalScore(r, 'precision') }}</td>
                <td>{{ retrievalScore(r, 'recall') }}</td>
                <td>{{ retrievalScore(r, 'mrr') }}</td>
                <td class="eval-answer-marks">
                  <span v-if="current.request.kind === 'retrieval'">不适用（仅检索）</span>
                  <span v-else-if="r.status === 'failed' || r.status === 'cancelled'"
                    >无最终回答</span
                  >
                  <span v-else
                    >忠 {{ answerMark(r.review?.unsupported_claims, 'no') }} · 正
                    {{ answerMark(r.review?.verdict, 'pass') }} · 完
                    {{ answerMark(r.review?.fact_coverage, 'full') }}</span
                  >
                </td>
                <td>{{ r.elapsed_ms == null ? '—' : `${r.elapsed_ms} ms` }}</td>
                <td>{{ tokenCount(r) ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <section v-if="selected" class="card eval-detail">
          <h2>案例 {{ selectedIndex + 1 }} · {{ selected.case.query }}</h2>
          <ErrorBanner :error="selected.error" />
          <p v-if="selected.query_plan" class="small muted">
            实际查询：{{ selected.query_plan.queries.join(' / ')
            }}<span v-if="selected.query_plan.degraded">（规划已回退）</span>
          </p>
          <div class="eval-pipeline">
            <span>查询</span><b>→</b
            ><span
              >候选
              {{
                selected.diagnostics?.deduplicated_count ??
                selected.diagnostics?.trace?.before_rerank.length ??
                '—'
              }}</span
            >
            ><b>→</b><span>{{ selected.diagnostics?.rerank_applied ? '已重排' : '未重排' }}</span
            ><b>→</b><span>模型证据 {{ selected.context_sources?.length ?? '—' }}</span
            ><b>→</b><span>引用 {{ selected.run?.sources.length ?? '—' }}</span>
          </div>
          <p v-if="selected.diagnostics?.degraded?.length" class="notice warning">
            降级：{{ selected.diagnostics.degraded.join('、') }}
          </p>
          <div class="eval-timings">
            <div v-for="[stage, ms] in timing" :key="stage">
              <span>{{ stage }}</span>
              <div><i :style="{ width: `${(ms / maxTime) * 100}%` }" /></div>
              <small>{{ ms }} ms</small>
            </div>
          </div>
          <details v-if="selected.assembly">
            <summary>上下文预算与组装诊断</summary>
            <p>
              资料 {{ selected.assembly.evidence_tokens }} · 历史
              {{ selected.assembly.history_tokens }} · 完整输入估算
              {{ selected.assembly.total_estimated_tokens }} /
              {{ selected.assembly.input_budget }} tokens
            </p>
            <div class="eval-budget">
              <i
                :style="{
                  width: `${Math.min(100, (Number(selected.assembly.total_estimated_tokens) / Math.max(1, Number(selected.assembly.input_budget))) * 100)}%`,
                }"
              />
            </div>
            <pre>{{ JSON.stringify(selected.assembly, null, 2) }}</pre>
          </details>
          <template v-if="selected.run"
            ><h3>
              最终答案
              <span
                v-if="selected.run.answer_status === 'insufficient_evidence'"
                class="badge muted"
                >资料不足</span
              >
            </h3>
            <MarkdownAnswer
              :text="selected.run.answer || ''"
              :segments="selected.run.answer_segments"
              :sources="selected.run.sources"
              @source="source = $event"
          /></template>
          <section v-if="selected.judgments" class="eval-judgments">
            <h3>文件标注覆盖</h3>
            <p>
              已审核 {{ selected.judgments.judged }}/{{ selected.judgments.returned }} 个返回文件 ·
              覆盖率 {{ percent(selected.judgments.coverage) }} · 已审核文件精准率
              {{ percent(selected.judgments.judged_precision) }}
            </p>
            <p v-if="selected.case.judgment_scope === 'partial'" class="notice warning">
              部分标注：未审核文件不是负样本，上述已审核精准率不能作为完整 Precision@K 发布。
            </p>
          </section>
          <section v-if="selected.evidence" class="eval-evidence-coverage">
            <h3>必要原文覆盖</h3>
            <p class="small muted">
              这是文件哈希绑定的原文包含检查，引用是否支持结论由人工审核。失效或无法映射的锚点不计证据覆盖分数。
            </p>
            <p v-if="!selected.evidence.valid" class="notice warning">
              标注失效或未映射，请核对原文、页码、文件版本及解析结果。
            </p>
            <div
              v-for="(value, stage) in selected.evidence.coverage"
              :key="stage"
              class="eval-coverage-row"
            >
              <span>{{ stages[stage] || stage }}</span
              ><progress
                max="1"
                :value="value ?? undefined"
                :aria-label="`${stages[stage]}证据覆盖率`"
              /><strong>{{ value == null ? '未评分' : percent(value) }}</strong>
            </div>
            <p>
              规则提示：{{
                selected.evidence.causes.map((c) => causeLabels[c] || c).join('、') ||
                '未发现证据流转丢失；回答质量仍需审核'
              }}
            </p>
            <details v-for="(group, g) in selected.evidence.groups" :key="g">
              <summary>{{ g + 1 }}. {{ group.fact }}</summary>
              <p v-for="(a, n) in group.alternatives" :key="n">
                {{ fileName(a.file_id) }} · {{ a.anchor_status }} · {{ a.quote }}
              </p>
            </details>
          </section>
          <h3>证据流转（切片级）</h3>
          <p class="small muted">指标按文件去重，以下保留切片排名。仅检索时不会调用回答模型。</p>
          <div class="table-scroll">
            <table class="eval-table">
              <thead>
                <tr>
                  <th>文件 / 片段</th>
                  <th>初排</th>
                  <th>重排后</th>
                  <th>进上下文</th>
                  <th>被引用</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in evidenceRows" :key="row.source.chunk_id">
                  <td>
                    <button class="text-button" @click="source = row.source">
                      {{ row.source.name }} · {{ row.source.chunk_id.slice(0, 6) }}
                    </button>
                  </td>
                  <td>{{ row.before }}</td>
                  <td>{{ row.after || '—' }}</td>
                  <td>{{ row.context ? '✓' : '—' }}</td>
                  <td>{{ row.cited ? '✓' : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <details>
            <summary>本题标注与资料快照</summary>
            <p>
              相关文件：{{
                selected.case.relevant_file_ids.map(fileName).join('、') || '无正相关标注'
              }}
            </p>
            <p>审核要点：{{ selected.case.required_facts || '未填写' }}</p>
            <p>参考答案：{{ selected.case.reference_answer || '未填写' }}</p>
            <p class="small muted">实验指纹 {{ current.snapshot.fingerprint }}</p>
          </details>
          <section v-if="selected.judge || selected.judge_error" class="eval-judge">
            <h3>生成层 RAGAS 评分</h3>
            <p v-if="selected.judge_error" class="notice warning">{{ selected.judge_error }}</p>
            <div v-else class="eval-outcomes">
              <span>忠实度 {{ percent(selected.judge?.faithfulness) }}</span>
              <span>答案相关性 {{ percent(selected.judge?.answer_relevancy) }}</span>
              <span>答案正确性 {{ percent(selected.judge?.answer_correctness) }}</span>
              <span>完整性 {{ percent(selected.judge?.completeness) }}</span>
            </div>
            <p v-if="selected.judge?.ragas_version" class="small muted">
              RAGAS {{ selected.judge.ragas_version }} · worker
              {{ selected.judge.worker || 'isolated' }}
              <span v-if="selected.judge.judge_model"> · {{ selected.judge.judge_model }}</span>
              <span v-if="selected.judge.thinking_mode === 'disabled'"> · 非思考评审</span>
              <span v-if="selected.judge.scoring_phase === 'post_run_backfill'"> · 运行后补评</span>
            </p>
            <p class="small muted">
              RAGAS LLM-as-judge 分数与人工审核分开保存；缺失的参考答案会使正确性和完整性无法评分。
            </p>
          </section>
          <p v-if="current.request.kind === 'retrieval'" class="notice">
            本次为仅检索评测，没有生成回答，答案质量评分不适用。请运行完整 RAG 后审核实际回答。
          </p>
          <fieldset v-else class="eval-review eval-write-controls" :disabled="!canIngest">
            <h3>人工答案评价</h3>
            <p class="small muted">系统不把合法引用当作事实支持证明。请结合答案和原文审核。</p>
            <p v-if="reviewOrigin(selected.review)" class="small muted" data-testid="review-origin">
              {{ reviewOrigin(selected.review) }}
            </p>
            <label class="field"
              >评价<select
                v-model="dimensions.verdict"
                aria-label="人工评价"
                :disabled="active || busy"
              >
                <option value="pending">待审核</option>
                <option value="pass">通过</option>
                <option value="partial">部分正确</option>
                <option value="fail">错误</option>
              </select></label
            ><textarea
              v-model="dimensions.notes"
              maxlength="2000"
              rows="2"
              aria-label="评价备注"
              :disabled="active || busy"
              placeholder="遗漏、无依据结论或引用问题"
            />
            <label v-for="field in reviewFields" :key="field.key" class="field"
              >{{ field.label
              }}<select
                v-model="dimensions[field.key]"
                :aria-label="field.label"
                :disabled="active || busy"
              >
                <option v-for="(label, value) in field.options" :key="value" :value="value">
                  {{ label }}
                </option>
              </select></label
            >
            <fieldset :disabled="active || busy">
              <legend>人工归因（可多选）</legend>
              <label
                v-for="cause in [
                  'annotation_gap',
                  'parsing',
                  'retrieval',
                  'rerank',
                  'assembly',
                  'generation',
                  'unknown',
                ]"
                :key="cause"
                ><input v-model="dimensions.causes" type="checkbox" :value="cause" />{{
                  causeLabels[cause]
                }}
              </label>
            </fieldset>
            <button v-if="canIngest" class="button" :disabled="active || busy" @click="review">
              保存评价
            </button>
          </fieldset>
        </section>
      </template>
      <div v-else class="card empty">
        <h2>从一道问题看见 RAG 的能力</h2>
        <p>选择仅检索或完整问答；填写相关文件标注后，即可查看召回率、精准率和命中率。</p>
        <p class="small muted">已有资料 + 小样本 + 同一执行链路；不会自动下载公开基准数据集。</p>
      </div>
    </section>
    <SourcePanel :source="source" @close="source = null" />
  </div>
</template>
