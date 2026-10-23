<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request, date } from '../../api/client'
import ErrorBanner from '../../components/ErrorBanner.vue'
import { download } from './download'
import { percent } from './format'
import { metricLabels, type Comparison, type Scores } from './types'
const props = defineProps<{
  currentId: string
  history: { evaluation_id: string; status: string; created_at: string; kind: string }[]
}>()
const left = ref(''),
  right = ref(''),
  result = ref<Comparison>(),
  error = ref<unknown>(),
  busy = ref(false),
  gateBusy = ref(false),
  maxRecallDrop = ref(2),
  maxMrrDrop = ref(2),
  maxAnswerDrop = ref(5)
type GateCheck = {
  name: string
  source: string
  delta: number | null
  max_drop: number
  status: 'pass' | 'fail' | 'unscored'
}
type GateResult = {
  status: 'pass' | 'fail' | 'unscored'
  checks: GateCheck[]
  notice: string
}
const gate = ref<GateResult>()
watch(
  () => props.currentId,
  (value) => {
    left.value = value
    right.value = ''
    result.value = undefined
  },
  { immediate: true },
)
watch([left, right], () => {
  result.value = undefined
  gate.value = undefined
  error.value = undefined
})
const knownHistory = ref<typeof props.history>([])
watch(
  () => props.history,
  (rows) => {
    const byId = new Map(knownHistory.value.map((row) => [row.evaluation_id, row]))
    for (const row of rows) byId.set(row.evaluation_id, row)
    knownHistory.value = [...byId.values()]
  },
  { immediate: true },
)
const completed = computed(() => knownHistory.value.filter((r) => r.status === 'completed'))
const labels = Object.keys(metricLabels) as (keyof Scores)[]
const answerLabels = ['faithfulness', 'answer_relevancy', 'correctness', 'completeness'] as const
const answerMetrics = computed(() =>
  answerLabels
    .map((key) => result.value?.answer_metrics?.[key])
    .filter((metric): metric is NonNullable<typeof metric> => Boolean(metric)),
)
const hasAnswerPairs = computed(() => answerMetrics.value.some((metric) => metric.paired > 0))
const answerDelta = (row: Comparison['rows'][number], key: (typeof answerLabels)[number]) =>
  row.answer?.[key]?.delta ?? null
const outcomes: Record<string, string> = {
  win: '提升',
  tie: '持平',
  loss: '下降',
  unscored: '未评分',
}
const delta = (value: number | null, unit = 'pp') =>
  value == null
    ? '—'
    : `${value > 0 ? '+' : ''}${(unit === 'pp' ? value * 100 : value).toFixed(1)} ${unit}`
async function compare() {
  busy.value = true
  error.value = undefined
  result.value = undefined
  gate.value = undefined
  const a = left.value,
    b = right.value
  try {
    const value = await request<Comparison>(
      `/evaluations/compare?left=${encodeURIComponent(a)}&right=${encodeURIComponent(b)}`,
    )
    if (a === left.value && b === right.value) result.value = value
  } catch (e) {
    if (a === left.value && b === right.value) error.value = e
  } finally {
    busy.value = false
  }
}
async function runGate() {
  if (!left.value || !right.value) return
  gateBusy.value = true
  error.value = undefined
  const a = left.value,
    b = right.value
  try {
    const value = await request<GateResult>(
      `/evaluations/gate?baseline=${encodeURIComponent(a)}&candidate=${encodeURIComponent(b)}` +
        `&max_recall_drop=${maxRecallDrop.value / 100}&max_mrr_drop=${maxMrrDrop.value / 100}` +
        `&max_answer_drop=${maxAnswerDrop.value / 100}`,
    )
    if (a === left.value && b === right.value) gate.value = value
  } catch (e) {
    if (a === left.value && b === right.value) error.value = e
  } finally {
    gateBusy.value = false
  }
}
const gateStatus: Record<string, string> = {
  pass: '通过',
  fail: '未通过',
  unscored: '待补评分',
}
const checkStatus: Record<string, string> = {
  pass: '通过',
  fail: '下降超限',
  unscored: '未评分',
}
function exportComparison() {
  if (!result.value) return
  const r = result.value
  const safe = (v: unknown) =>
    String(v ?? '未知')
      .replace(/\|/g, '\\|')
      .replace(/[\r\n]/g, ' ')
      .replace(/</g, '&lt;')
  const text = [
    '# RAG 同题实验比较',
    '',
    `基线 A：${r.left_id || left.value}；对照 B：${r.right_id || right.value}`,
    `胜平负：${JSON.stringify(r.counts)}`,
    `参数差异：${JSON.stringify(r.experiment_changes || {})}`,
    '',
    '| 问题 | 结果 | Δ Recall | Δ nDCG | Δ 忠实度 | Δ 答案相关性 | Δ 正确性 | Δ 完整性 | Δ ms | Δ Token |',
    '|---|---|---|---|---|---|---|---|---|---|',
    ...r.rows.map(
      (row) =>
        `| ${safe(row.query)} | ${safe(outcomes[row.outcome])} | ${delta(row.delta.recall)} | ${delta(row.delta.ndcg)} | ${delta(answerDelta(row, 'faithfulness'))} | ${delta(answerDelta(row, 'answer_relevancy'))} | ${delta(answerDelta(row, 'correctness'))} | ${delta(answerDelta(row, 'completeness'))} | ${delta(row.elapsed_ms_delta, 'ms')} | ${delta(row.tokens_delta, 'tokens')} |`,
    ),
    '',
    '回答质量差值只统计 A、B 两次实验都已明确审核的同一题；待审核和不适用不计为通过。',
    '',
    '小样本仅作诊断，不推断显著性。复用结果不参与真实执行用量/耗时比较。',
    '',
    `配置差异：${JSON.stringify(r.config_changes)}`,
  ].join('\n')
  download('rag-comparison.md', text, 'text/markdown;charset=utf-8')
}
</script>
<template>
  <section class="card eval-comparison">
    <h2>同题实验对比</h2>
    <p class="small muted">
      选择同一案例、标注版本、语料、运行方式和 K 的实验。差值均为 B − A；胜平负按逐题
      nDCG，小样本仅用于诊断。
    </p>
    <div class="eval-actions">
      <label class="field"
        >基线 A<select v-model="left" aria-label="基线实验 A">
          <option value="">请选择</option>
          <option v-for="r in completed" :key="r.evaluation_id" :value="r.evaluation_id">
            {{ date(r.created_at) }} · {{ r.evaluation_id.slice(0, 8) }}
          </option>
        </select></label
      >
      <label class="field"
        >对照 B<select v-model="right" aria-label="对照实验 B">
          <option value="">请选择</option>
          <option v-for="r in completed" :key="r.evaluation_id" :value="r.evaluation_id">
            {{ date(r.created_at) }} · {{ r.evaluation_id.slice(0, 8) }}
          </option>
        </select></label
      >
      <button class="button" :disabled="busy || !left || !right || left === right" @click="compare">
        {{ busy ? '比较中…' : '比较实验' }}
      </button>
    </div>
    <ErrorBanner :error="error" />
    <template v-if="result">
      <button class="button" @click="exportComparison">导出比较报告</button>
      <p>
        {{ result.modes.join(' → ') }} · 提升 {{ result.counts.win }} / 持平
        {{ result.counts.tie }} / 下降 {{ result.counts.loss }} / 未评分
        {{ result.counts.unscored }}
      </p>
      <div class="eval-deltas" role="img" aria-label="两次实验检索指标差值">
        <div v-for="key in labels" :key="key">
          <span>{{ metricLabels[key] }}</span>
          <div class="eval-delta-track">
            <i
              :style="{
                left: `${Math.min(50, 50 + (result.metric_delta[key] || 0) * 50)}%`,
                width: `${Math.abs(result.metric_delta[key] || 0) * 50}%`,
                background: (result.metric_delta[key] || 0) < 0 ? '#ae493c' : 'var(--accent)',
              }"
            />
          </div>
          <strong>{{ delta(result.metric_delta[key]) }}</strong>
        </div>
      </div>
      <template v-if="hasAnswerPairs">
        <h3>回答质量差值（仅两次都已审核的同题）</h3>
        <div class="eval-deltas" role="img" aria-label="两次实验回答质量指标差值">
          <div v-for="metric in answerMetrics" :key="metric.label">
            <span>{{ metric.label }}</span>
            <div class="eval-delta-track">
              <i
                :style="{
                  left: `${Math.min(50, 50 + (metric.delta || 0) * 50)}%`,
                  width: `${Math.abs(metric.delta || 0) * 50}%`,
                  background: (metric.delta || 0) < 0 ? '#ae493c' : 'var(--accent)',
                }"
              />
            </div>
            <strong>{{ delta(metric.delta) }}</strong>
          </div>
        </div>
        <p class="small muted">
          严格口径：忠实度=无无依据事实，正确性=人工总评通过，完整性=必要事实完整覆盖。配对
          {{ answerMetrics[0]?.paired ?? 0 }} 题，未配对 {{ answerMetrics[0]?.unpaired ?? 0 }}
          题；未审核不计为通过。
        </p>
        <details>
          <summary>回答质量配对明细</summary>
          <table class="eval-table">
            <thead>
              <tr>
                <th>指标</th>
                <th>A</th>
                <th>B</th>
                <th>Δ B−A</th>
                <th>配对题</th>
                <th>未配对题</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="metric in answerMetrics" :key="metric.label">
                <td>{{ metric.label }}</td>
                <td>{{ percent(metric.left) }}</td>
                <td>{{ percent(metric.right) }}</td>
                <td>{{ delta(metric.delta) }}</td>
                <td>{{ metric.paired }}</td>
                <td>{{ metric.unpaired }}</td>
              </tr>
            </tbody>
          </table>
        </details>
      </template>
      <div class="table-scroll">
        <table class="eval-table">
          <thead>
            <tr>
              <th>问题</th>
              <th>结果</th>
              <th>Δ Recall</th>
              <th>Δ nDCG</th>
              <th>Δ 忠实</th>
              <th>Δ 相关</th>
              <th>Δ 正确</th>
              <th>Δ 完整</th>
              <th>Δ 耗时</th>
              <th>Δ Token</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in result.rows" :key="i">
              <td>{{ row.query }}</td>
              <td>{{ outcomes[row.outcome] }}</td>
              <td>{{ delta(row.delta.recall) }}</td>
              <td>{{ delta(row.delta.ndcg) }}</td>
              <td>{{ delta(answerDelta(row, 'faithfulness')) }}</td>
              <td>{{ delta(answerDelta(row, 'answer_relevancy')) }}</td>
              <td>{{ delta(answerDelta(row, 'correctness')) }}</td>
              <td>{{ delta(answerDelta(row, 'completeness')) }}</td>
              <td>{{ delta(row.elapsed_ms_delta, 'ms') }}</td>
              <td>{{ delta(row.tokens_delta, 'tokens') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="result.code_changed" class="notice warning">
        两次实验代码版本不同，请结合变更解释差异。
      </p>
      <details>
        <summary>配置差异</summary>
        <pre>{{
          JSON.stringify(
            { experiments: result.experiment_changes, workspace: result.config_changes },
            null,
            2,
          )
        }}</pre>
      </details>
      <section class="eval-gate">
        <h3>回归门禁</h3>
        <p class="small muted">
          以 A 为基线检查
          B。指标下降超过阈值即未通过；未评分不会被当作通过。生成层门禁当前使用人工审核指标，RAGAS
          分数在实验页单独查看。默认阈值只用于开发回归，正式门禁需按业务样本校准。
        </p>
        <div class="eval-gate-controls">
          <label class="field"
            >召回率最大下降 %<input
              v-model.number="maxRecallDrop"
              type="number"
              min="0"
              max="100"
              step="0.5"
          /></label>
          <label class="field"
            >MRR 最大下降 %<input
              v-model.number="maxMrrDrop"
              type="number"
              min="0"
              max="100"
              step="0.5"
          /></label>
          <label class="field"
            >回答质量最大下降 %<input
              v-model.number="maxAnswerDrop"
              type="number"
              min="0"
              max="100"
              step="0.5"
          /></label>
          <button class="button" type="button" :disabled="gateBusy" @click="runGate">
            {{ gateBusy ? '检查中…' : '检查门禁' }}
          </button>
        </div>
        <div v-if="gate" class="eval-gate-result" :class="gate.status">
          <strong>门禁结果：{{ gateStatus[gate.status] || gate.status }}</strong>
          <table class="eval-table">
            <thead>
              <tr>
                <th>检查项</th>
                <th>层</th>
                <th>Δ B−A</th>
                <th>允许下降</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="check in gate.checks" :key="check.name">
                <td>{{ check.name }}</td>
                <td>{{ check.source === 'retrieval' ? '检索层' : '生成层' }}</td>
                <td>{{ check.delta == null ? '—' : delta(check.delta) }}</td>
                <td>{{ (check.max_drop * 100).toFixed(1) }} pp</td>
                <td>{{ checkStatus[check.status] || check.status }}</td>
              </tr>
            </tbody>
          </table>
          <p class="small muted">{{ gate.notice }}</p>
        </div>
      </section>
    </template>
  </section>
</template>
