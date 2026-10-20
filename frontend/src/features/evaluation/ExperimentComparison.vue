<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request, date } from '../../api/client'
import ErrorBanner from '../../components/ErrorBanner.vue'
import { download } from './download'
import { metricLabels, type Comparison, type Scores } from './types'
const props = defineProps<{
  currentId: string
  history: { evaluation_id: string; status: string; created_at: string; kind: string }[]
}>()
const left = ref(''),
  right = ref(''),
  result = ref<Comparison>(),
  error = ref<unknown>(),
  busy = ref(false)
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
    '| 问题 | 结果 | Δ Recall | Δ nDCG | Δ ms | Δ Token |',
    '|---|---|---|---|---|---|',
    ...r.rows.map(
      (row) =>
        `| ${safe(row.query)} | ${safe(outcomes[row.outcome])} | ${delta(row.delta.recall)} | ${delta(row.delta.ndcg)} | ${delta(row.elapsed_ms_delta, 'ms')} | ${delta(row.tokens_delta, 'tokens')} |`,
    ),
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
      <div class="table-scroll">
        <table class="eval-table">
          <thead>
            <tr>
              <th>问题</th>
              <th>结果</th>
              <th>Δ Recall</th>
              <th>Δ nDCG</th>
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
    </template>
  </section>
</template>
