<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { AnswerMetric, GenerationSummary, QualitySummary } from './types'
import { percent } from './format'

const props = defineProps<{ quality: QualitySummary; generation?: GenerationSummary | null }>()
type DisplayMetric = {
  key: 'faithfulness' | 'answer_relevancy' | 'correctness' | 'completeness'
  label: string
  definition: string
  value: number | null
  numerator: number | null
  denominator: number
  judged: number
  pending: number
  missing: number
  counts: Record<string, number>
  acceptable?: number
  acceptable_rate?: number | null
}
const humanOrder = ['faithfulness', 'answer_relevancy', 'correctness', 'completeness'] as const
const ragasOrder = [
  'faithfulness',
  'answer_relevancy',
  'answer_correctness',
  'completeness',
] as const
const ragasLabels: Record<string, string> = {
  faithfulness: '忠实度',
  answer_relevancy: '答案相关性',
  answer_correctness: '答案正确性',
  completeness: '完整性',
}
const ragasDefinitions: Record<string, string> = {
  faithfulness: 'RAGAS 判断回答中的结论是否由检索上下文支持。',
  answer_relevancy: 'RAGAS 判断回答是否直接回应问题，而不是答非所问。',
  answer_correctness: 'RAGAS 结合参考答案判断事实正确性；需要参考答案。',
  completeness: 'RAGAS LLM 评审判断回答是否覆盖参考答案中的全部必要事实。',
}
const source = ref<'human' | 'ragas'>('human')
watch(
  () => props.generation?.available,
  (available) => {
    if (available) source.value = 'ragas'
  },
  { immediate: true },
)
const humanMetrics = computed<DisplayMetric[]>(() =>
  humanOrder
    .map((key) => props.quality.metrics?.[key])
    .filter((metric): metric is AnswerMetric => Boolean(metric))
    .map((metric) => ({
      ...metric,
      missing: 0,
    })),
)
const ragasMetrics = computed<DisplayMetric[]>(() =>
  ragasOrder.map((key) => {
    const metric = props.generation?.metrics?.[key]
    return {
      key: key === 'answer_correctness' ? 'correctness' : key,
      label: ragasLabels[key],
      definition: ragasDefinitions[key],
      value: metric?.value ?? null,
      numerator: null,
      denominator: metric?.judged ?? 0,
      judged: metric?.judged ?? 0,
      pending: 0,
      missing: metric?.missing ?? 0,
      counts: {},
    }
  }),
)
const metrics = computed(() => (source.value === 'ragas' ? ragasMetrics.value : humanMetrics.value))
const hasScored = computed(() => metrics.value.some((metric) => metric.value != null))
const colors: Record<string, string> = {
  faithfulness: '#24745d',
  answer_relevancy: '#246a9e',
  correctness: '#0c927e',
  completeness: '#b66920',
}
const chart = { width: 520, height: 238, base: 182, bar: 56 }
const centers = [88, 215, 342, 469]
const height = (metric: DisplayMetric) => (metric.value == null ? 0 : metric.value * 148)
const detail = (metric: DisplayMetric) => {
  const counts = Object.entries(metric.counts)
    .map(([key, value]) => `${key} ${value}`)
    .join(' · ')
  return `${metric.label}：${percent(metric.value)}，已评 ${metric.judged}${counts ? `；${counts}` : ''}`
}
</script>

<template>
  <section v-if="humanMetrics.length || generation?.available" class="card eval-answer-quality">
    <div class="eval-generation-heading">
      <div>
        <h2>生成层指标</h2>
        <p class="small muted">
          与检索层分开观察。RAGAS 用 LLM-as-judge
          做语义评分；人工审核是严格二值判定。两者都保留原始记录，不互相覆盖。
        </p>
      </div>
      <div
        v-if="generation?.available"
        class="eval-dataset-scope"
        role="group"
        aria-label="生成层评分来源"
      >
        <button type="button" :class="{ active: source === 'ragas' }" @click="source = 'ragas'">
          RAGAS 自动评分
        </button>
        <button type="button" :class="{ active: source === 'human' }" @click="source = 'human'">
          人工审核
        </button>
      </div>
    </div>
    <p v-if="source === 'ragas'" class="small muted">
      RAGAS 已评 {{ generation?.judged_queries || 0 }} 题 · 缺失
      {{ generation?.errors || 0 }} 题评分失败。分数是语义判断，不是业务签收结论。
    </p>
    <p v-if="!metrics.length" class="notice">
      人工审核尚无已评的生成层指标。请先审核回答，或切换到可用的 RAGAS 自动评分。
    </p>
    <p v-else-if="!hasScored" class="eval-unscored-note">
      当前还没有已评分的生成层指标；完成人工审核或启用 RAGAS 后，这里会显示对比图和分数。
    </p>
    <svg
      v-if="metrics.length && hasScored"
      :viewBox="`0 0 ${chart.width} ${chart.height}`"
      role="img"
      aria-label="生成层指标：忠实度、答案相关性、正确性和完整性柱状图"
    >
      <g v-for="tick in [0, 0.25, 0.5, 0.75, 1]" :key="tick">
        <line
          x1="42"
          x2="500"
          :y1="chart.base - tick * 148"
          :y2="chart.base - tick * 148"
          stroke="#e9edf3"
        />
        <text x="4" :y="chart.base - tick * 148 + 4" font-size="10" fill="#707b8c">
          {{ tick * 100 }}%
        </text>
      </g>
      <g v-for="(metric, index) in metrics" :key="metric.key + index">
        <rect
          v-if="metric.value != null"
          :x="centers[index] - chart.bar / 2"
          :y="chart.base - height(metric)"
          :width="chart.bar"
          :height="height(metric)"
          :fill="colors[metric.key]"
          rx="3"
        >
          <title>{{ detail(metric) }}</title>
        </rect>
        <rect
          v-else
          :x="centers[index] - chart.bar / 2"
          :y="chart.base - 2"
          :width="chart.bar"
          height="2"
          fill="none"
          stroke="#9aa4b2"
          stroke-dasharray="4 3"
        />
        <text
          :x="centers[index]"
          :y="chart.base - height(metric) - 10"
          text-anchor="middle"
          font-size="13"
          font-weight="600"
          :fill="metric.value == null ? '#707b8c' : colors[metric.key]"
        >
          {{ percent(metric.value) }}
        </text>
        <text
          :x="centers[index]"
          :y="chart.base + 24"
          text-anchor="middle"
          font-size="12"
          fill="#334155"
        >
          {{ metric.label }}
        </text>
        <text
          :x="centers[index]"
          :y="chart.base + 42"
          text-anchor="middle"
          font-size="10"
          fill="#707b8c"
        >
          {{
            source === 'ragas'
              ? `已评 ${metric.judged}`
              : `${metric.numerator}/${metric.denominator}`
          }}
        </text>
      </g>
    </svg>
    <div v-if="metrics.length" class="eval-answer-metric-list">
      <div
        v-for="metric in metrics"
        :key="metric.key"
        :class="{ 'is-unscored': metric.value == null }"
      >
        <span class="eval-answer-metric-name"
          ><i :style="{ background: colors[metric.key] }" />{{ metric.label }}</span
        >
        <strong>{{ percent(metric.value) }}</strong>
        <small v-if="source === 'ragas'">
          已评 {{ metric.judged }} 题 · 缺失 {{ metric.missing }} 题
        </small>
        <small v-else>
          已审核 {{ metric.numerator }}/{{ metric.denominator }} · 待审核
          {{ metric.pending }}
        </small>
        <p>{{ metric.definition }}</p>
        <p
          v-if="
            source === 'human' && metric.key === 'correctness' && metric.acceptable_rate != null
          "
          class="small muted"
        >
          含部分正确的可接受率 {{ percent(metric.acceptable_rate) }}（{{ metric.acceptable }}/{{
            metric.denominator
          }}），不作为严格正确性。
        </p>
      </div>
    </div>
    <details v-if="metrics.length">
      <summary>查看计算口径与原始计数</summary>
      <table class="eval-table">
        <thead>
          <tr>
            <th>指标</th>
            <th>值</th>
            <th>已评</th>
            <th>原始计数 / 缺失</th>
            <th>来源</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in metrics" :key="metric.key">
            <td>{{ metric.label }}</td>
            <td>{{ percent(metric.value) }}</td>
            <td>{{ metric.judged }}</td>
            <td>
              <template v-if="source === 'ragas'">缺失 {{ metric.missing }}</template>
              <template v-else>
                <span v-for="(count, key) in metric.counts" :key="key">{{ key }}={{ count }} </span>
              </template>
            </td>
            <td>{{ source === 'ragas' ? 'RAGAS LLM-as-judge' : '人工审核' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="small muted">
        人工口径：忠实度=unsupported_claims 为 no；答案相关性=answer_relevancy 为
        relevant；正确性=verdict 为 pass；完整性=fact_coverage 为 full。RAGAS
        口径由语义评审模型计算，缺失分数不补零，也不覆盖人工记录。
      </p>
    </details>
  </section>
</template>
