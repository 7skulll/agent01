<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { request, base, date } from '../../api/client'
import { api } from '../../api/client'
import { modeLabels, type Mode, type Source } from '../../api/types'
import {
  metricLabels,
  evaluationStatus,
  type EvalCase,
  type EvalRecord,
  type EvalResult,
  type Scores,
} from './types'
import MetricChart from './MetricChart.vue'
import ErrorBanner from '../../components/ErrorBanner.vue'
import MarkdownAnswer from '../../components/MarkdownAnswer.vue'
import SourcePanel from '../../components/SourcePanel.vue'
const props = defineProps<{ kbId: string }>()
const client = useQueryClient()
const mode = ref<Mode>('hybrid'),
  kind = ref('retrieval'),
  k = ref(5),
  scoped = ref(false),
  scope = ref<string[]>([])
const blank = (): EvalCase => ({
  query: '',
  expected: 'unlabelled',
  relevant_file_ids: [],
  required_facts: '',
})
const cases = ref<EvalCase[]>([blank()]),
  busy = ref(false),
  error = ref<unknown>(null),
  selectedIndex = ref(0),
  source = ref<Source | null>(null)
const id = ref(''),
  verdict = ref('pending'),
  notes = ref(''),
  importing = ref<HTMLInputElement>()
let pending: { signature: string; key: string } | undefined
const files = useQuery({
  queryKey: computed(() => ['all-files', props.kbId]),
  enabled: computed(() => !!props.kbId),
  queryFn: () => api.allFiles(props.kbId),
})
const history = useQuery({
  queryKey: ['evaluations'],
  queryFn: () =>
    request<
      { evaluation_id: string; status: string; kb_id: string; created_at: string; kind: string }[]
    >('/evaluations'),
  refetchInterval: 5000,
})
const ownHistory = computed(() => (history.data.value || []).filter((r) => r.kb_id === props.kbId))
const record = useQuery({
  queryKey: computed(() => ['evaluation', id.value]),
  enabled: computed(() => !!id.value),
  queryFn: () => request<EvalRecord>(`/evaluations/${id.value}`),
  refetchInterval: (q) => (q.state.data?.status === 'running' ? 1000 : false),
})
const current = computed(() =>
  record.data.value?.request.knowledge_base_id === props.kbId ? record.data.value : undefined,
)
const active = computed(() => current.value?.status === 'running')
const selected = computed(() => current.value?.results[selectedIndex.value])
const labels = Object.keys(metricLabels) as (keyof Scores)[]
watch(
  () => props.kbId,
  () => {
    id.value = localStorage.getItem(`rag-evaluation:${props.kbId}`) || ''
    cases.value = [blank()]
    scope.value = []
    scoped.value = false
    selectedIndex.value = 0
    source.value = null
    error.value = null
    pending = undefined
  },
  { immediate: true },
)
watch(id, (value) => {
  if (value) localStorage.setItem(`rag-evaluation:${props.kbId}`, value)
  selectedIndex.value = 0
})
watch(ownHistory, (rows) => {
  if (!id.value) {
    const running = rows.find((r) => r.status === 'running')
    if (running) id.value = running.evaluation_id
  }
})
watch(selected, (r) => {
  verdict.value = r?.review?.verdict || 'pending'
  notes.value = r?.review?.notes || ''
})
const status = (value: string) => evaluationStatus[value] || value
const percent = (value: number | null | undefined) =>
  value == null ? '未标注' : `${(value * 100).toFixed(1)}%`
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
  k.value = r.request.k
  scoped.value = r.request.file_ids !== null
  scope.value = [...(r.request.file_ids || [])]
  cases.value = JSON.parse(JSON.stringify(r.request.cases))
  pending = undefined
}
async function start() {
  if (busy.value || active.value) return
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
  }
  const signature = JSON.stringify(payload)
  if (!pending || pending.signature !== signature) pending = { signature, key: crypto.randomUUID() }
  try {
    const response = await request<EvalRecord>('/evaluations', {
      method: 'POST',
      body: JSON.stringify({ ...payload, idempotency_key: pending.key }),
    })
    client.setQueryData(['evaluation', response.evaluation_id], response)
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
  if (!id.value || busy.value) return
  busy.value = true
  try {
    const response = await request<EvalRecord>(`/evaluations/${id.value}/stop`, { method: 'POST' })
    client.setQueryData(['evaluation', response.evaluation_id], response)
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
async function review() {
  if (!selected.value || !id.value || active.value) return
  busy.value = true
  try {
    const response = await request<EvalRecord>(
      `/evaluations/${id.value}/reviews/${selectedIndex.value}`,
      { method: 'PATCH', body: JSON.stringify({ verdict: verdict.value, notes: notes.value }) },
    )
    client.setQueryData(['evaluation', response.evaluation_id], response)
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
async function importCases(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  try {
    if (file.size > 256 * 1024) throw new Error('案例文件不能超过 256 KiB。')
    const data = JSON.parse(await file.text())
    const rows = Array.isArray(data) ? data : data.cases
    if (!Array.isArray(rows) || !rows.length || rows.length > 20)
      throw new Error('请导入 1–20 条案例的 JSON 数组。')
    cases.value = rows.map((r: any) => {
      if (
        typeof r.query !== 'string' ||
        !r.query.trim() ||
        r.query.length > 4000 ||
        !['unlabelled', 'answerable', 'unanswerable'].includes(r.expected || 'unlabelled') ||
        !Array.isArray(r.relevant_file_ids || [])
      )
        throw new Error('案例字段格式无效。')
      return {
        query: r.query,
        expected: r.expected || 'unlabelled',
        relevant_file_ids: r.relevant_file_ids || [],
        required_facts: r.required_facts || '',
      }
    })
  } catch (e) {
    error.value = e
  } finally {
    ;(event.target as HTMLInputElement).value = ''
  }
}
function download(name: string, contents: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([contents], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function exportJson() {
  try {
    const r = await request<EvalRecord>(`/evaluations/${id.value}`)
    download(`evaluation-${id.value}.json`, JSON.stringify(r, null, 2))
  } catch (e) {
    error.value = e
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
      '命中',
      'MRR',
      'nDCG',
      '耗时毫秒',
      '人工评价',
      '回答',
    ],
    ...current.value.results.map((r) => [
      r.case.query,
      r.status,
      r.case.expected,
      r.metrics?.precision,
      r.metrics?.recall,
      r.metrics?.hit,
      r.metrics?.mrr,
      r.metrics?.ndcg,
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
const trace = computed(() => selected.value?.diagnostics?.trace)
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
    source: s,
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
const tokenCount = (r: EvalResult) => {
  const events = r.token_usage || []
  return events.some(
    (e) =>
      typeof e.actual?.input_tokens === 'number' || typeof e.actual?.output_tokens === 'number',
  )
    ? events.reduce((n, e) => n + (e.actual?.input_tokens || 0) + (e.actual?.output_tokens || 0), 0)
    : null
}
</script>
<template>
  <div class="eval-layout">
    <form class="card eval-config" @submit.prevent="start">
      <h2>实验配置</h2>
      <p class="small muted">最多 20 题、逐题运行。完整 RAG 会调用回答模型。</p>
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
        K
        用于指标，不改变问答候选配置。文件级排名会将同文件多个切片去重；相关文件标注按穷尽集合解释。
      </p>
      <label
        ><input v-model="scoped" type="checkbox" :disabled="busy || active" />
        限定检索文件范围</label
      >
      <div v-if="scoped" class="file-options">
        <label v-for="f in files.data.value" :key="f.file_id"
          ><input v-model="scope" type="checkbox" :value="f.file_id" :disabled="busy || active" />{{
            f.name
          }}</label
        >
        <p v-if="!scope.length" class="small muted">范围为空，不会回退到全库。</p>
      </div>
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
              @change="c.relevant_file_ids = []"
            >
              <option value="unlabelled">未标注（仅观察）</option>
              <option value="answerable">资料中有答案</option>
              <option value="unanswerable">应资料不足</option>
            </select></label
          >
          <div v-if="c.expected === 'answerable'" class="file-options">
            <strong class="small">相关文件（必选）</strong
            ><label v-for="f in files.data.value" :key="f.file_id"
              ><input v-model="c.relevant_file_ids" type="checkbox" :value="f.file_id" />{{
                f.name
              }}</label
            >
          </div>
          <details>
            <summary>答案人工审核要点</summary>
            <textarea
              v-model="c.required_facts"
              maxlength="2000"
              rows="2"
              placeholder="必须回答的事实、限制条件或应拒答原因"
              :aria-label="`案例 ${i + 1} 审核要点`"
            />
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
        class="button primary full"
        :disabled="
          busy ||
          active ||
          !kbId ||
          cases.some(
            (c) => !c.query.trim() || (c.expected === 'answerable' && !c.relevant_file_ids.length),
          )
        "
      >
        {{ busy ? '提交中…' : active ? '评测执行中' : '开始评测' }}
      </button>
      <button v-if="active" type="button" class="button full" :disabled="busy" @click="stop">
        停止评测
      </button>
      <ErrorBanner :error="files.error.value" />
    </form>
    <section class="eval-results">
      <ErrorBanner :error="error || record.error.value || history.error.value" />
      <div class="card eval-history">
        <label class="field"
          >评测记录<select v-model="id" aria-label="评测记录">
            <option value="">选择历史记录</option>
            <option v-for="r in ownHistory" :key="r.evaluation_id" :value="r.evaluation_id">
              {{ date(r.created_at) }} · {{ r.kind === 'rag' ? '完整 RAG' : '检索' }} ·
              {{ status(r.status) }}
            </option>
          </select></label
        >
      </div>
      <template v-if="current">
        <div class="result-heading">
          <div>
            <h2>
              {{ status(current.status) }} · {{ current.results.length }}/{{
                current.request.cases.length
              }}
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
            <button class="button" @click="exportJson">导出 JSON</button
            ><button class="button" @click="exportCsv">导出 CSV</button>
          </div>
        </div>
        <ErrorBanner :error="current.error" />
        <div class="eval-metrics">
          <div v-for="key in labels" :key="key" class="card">
            <small>{{ metricLabels[key] }}@{{ current.request.k }}</small
            ><strong>{{ percent(current.summary.metrics[key]) }}</strong>
          </div>
        </div>
        <p class="small muted">
          指标分母：{{ current.summary.metric_queries }} 道已执行有答案标注题（含
          {{ current.summary.labelled_failures }}
          道失败计零）。未标注、应拒答和取消题不参与检索质量均值。命中率衡量是否找到至少一个相关文件，不等于答案正确率。
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
          ><span>人工已评 {{ current.summary.reviewed }}</span>
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
                <th>Recall@{{ current.request.k }}</th>
                <th>耗时</th>
                <th>Token（实际）</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(r, i) in current.results"
                :key="i"
                :class="{ 'eval-selected': selectedIndex === i }"
              >
                <td>
                  <button class="text-button" @click="selectedIndex = i">
                    {{ i + 1 }}. {{ r.case.query }}
                  </button>
                </td>
                <td>{{ status(r.status) }}</td>
                <td>{{ r.status === 'failed' ? '失败（计零）' : percent(r.metrics?.recall) }}</td>
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
            ><span>候选 {{ selected.diagnostics?.trace?.before_rerank.length ?? '—' }}</span
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
            <p class="small muted">实验指纹 {{ current.snapshot.fingerprint }}</p>
          </details>
          <div class="eval-review">
            <h3>人工答案评价</h3>
            <p class="small muted">系统不把合法引用当作事实支持证明。请结合答案和原文审核。</p>
            <label class="field"
              >评价<select v-model="verdict" aria-label="人工评价" :disabled="active">
                <option value="pending">待审核</option>
                <option value="pass">通过</option>
                <option value="partial">部分正确</option>
                <option value="fail">错误</option>
              </select></label
            ><textarea
              v-model="notes"
              maxlength="2000"
              rows="2"
              aria-label="评价备注"
              :disabled="active"
              placeholder="遗漏、无依据结论或引用问题"
            /><button class="button" :disabled="active || busy" @click="review">保存评价</button>
          </div>
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
