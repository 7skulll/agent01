<script setup lang="ts">
import { computed } from 'vue'
import type { GenerationSummary, QualitySummary, Scores } from './types'
import { percent } from './format'

const props = defineProps<{
  retrieval: Scores
  human?: QualitySummary
  generation?: GenerationSummary
  showGeneration?: boolean
}>()
const useRagas = computed(() => Boolean(props.generation?.available))
const value = (ragasValue: number | null | undefined, humanValue: number | null | undefined) =>
  useRagas.value ? (ragasValue ?? null) : (humanValue ?? null)
const faithfulness = computed(() =>
  value(props.generation?.metrics?.faithfulness?.value, props.human?.metrics?.faithfulness?.value),
)
const relevancy = computed(() =>
  value(
    props.generation?.metrics?.answer_relevancy?.value,
    props.human?.metrics?.answer_relevancy?.value,
  ),
)
const correctness = computed(() =>
  value(
    props.generation?.metrics?.answer_correctness?.value,
    props.human?.metrics?.correctness?.value,
  ),
)
const completeness = computed(() =>
  value(props.generation?.metrics?.completeness?.value, props.human?.metrics?.completeness?.value),
)
const source = computed(() => (useRagas.value ? 'RAGAS' : '人工审核'))
const issues = computed(() => {
  const rows: { level: 'warning' | 'info'; text: string }[] = []
  const recall = props.retrieval.recall
  if (recall != null && recall < 0.7) {
    rows.push({
      level: 'warning',
      text: `检索层召回率 ${percent(recall)}，优先检查切分、召回范围和混合检索；文件没召回时，生成层再强也无法引用。`,
    })
  }
  if (faithfulness.value != null && faithfulness.value < 0.7) {
    rows.push({
      level: 'warning',
      text: `生成层忠实度 ${percent(faithfulness.value)}，存在无依据表述风险，优先检查上下文裁剪、提示词和引用约束。`,
    })
  }
  if (relevancy.value != null && relevancy.value < 0.7) {
    rows.push({
      level: 'warning',
      text: `答案相关性 ${percent(relevancy.value)}，回答可能答非所问；检查问题理解、查询改写和回答协议。`,
    })
  }
  if (completeness.value != null && completeness.value < 0.7) {
    rows.push({
      level: 'warning',
      text: `完整性 ${percent(completeness.value)}，必要事实可能遗漏；检查上下文预算、答案长度和多文档综合。`,
    })
  }
  if (recall != null && recall >= 0.8 && correctness.value != null && correctness.value < 0.7) {
    rows.push({
      level: 'warning',
      text: `检索召回 ${percent(recall)} 但正确性 ${percent(correctness.value)}；更像生成层问题，而不是检索漏召回。`,
    })
  }
  if (!rows.length) {
    rows.push({
      level: 'info',
      text: '当前已评分指标未触发明确短板。请继续查看逐题证据和人工审核，不要把启发式提示当作统计结论。',
    })
  }
  return rows
})
const metrics = computed(() =>
  [
    { label: '召回率', value: props.retrieval.recall, source: '检索层' },
    { label: '精准率', value: props.retrieval.precision, source: '检索层' },
    { label: 'MRR', value: props.retrieval.mrr, source: '检索层' },
    { label: '忠实度', value: faithfulness.value, source },
    { label: '答案相关性', value: relevancy.value, source },
    { label: '正确性', value: correctness.value, source },
    { label: '完整性', value: completeness.value, source },
  ].filter((metric) => metric.source === '检索层' || props.showGeneration),
)
</script>

<template>
  <section class="card eval-layer-diagnosis">
    <div class="eval-generation-heading">
      <div>
        <h2>分层诊断</h2>
        <p class="small muted">
          先判断检索层是否把必要资料召回，再判断生成层是否忠实、相关、正确和完整。阈值提示只用于定位，不是统计显著性结论。
        </p>
      </div>
      <span v-if="showGeneration" class="badge muted">生成层来源：{{ source }}</span>
    </div>
    <div class="eval-diagnosis-metrics">
      <span v-for="metric in metrics" :key="metric.label + metric.source">
        <small>{{ metric.source }} · {{ metric.label }}</small>
        <strong>{{ percent(metric.value) }}</strong>
      </span>
    </div>
    <ul class="eval-diagnosis-issues">
      <li v-for="(issue, index) in issues" :key="index" :class="issue.level">
        {{ issue.text }}
      </li>
    </ul>
  </section>
</template>
