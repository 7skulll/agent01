<script setup lang="ts">
import type { QualitySummary } from './types'
import { percent } from './format'
defineProps<{ quality: QualitySummary }>()
const labels: Record<string, string> = {
  fact_coverage: '必要事实完整覆盖',
  unsupported_claims: '存在无依据事实',
  conditions: '条件与例外正确',
  answer_relevancy: '答案直接回应问题',
  citation_support: '引用完整支持',
  refusal: '人工拒答判断正确',
}
</script>
<template>
  <section class="card eval-quality">
    <h2>回答质量总览 · 人工审核</h2>
    <p class="small muted">
      仅统计本次评测中已完成的完整 RAG 回答。重新运行产生的新回答需要重新审核；未审核、不适用和服务失败不会当作通过。
    </p>
    <div
      v-for="(value, key) in quality.dimensions"
      :key="key"
      class="eval-quality-row"
      :class="{ 'is-unscored': !value.judged }"
    >
      <div class="eval-quality-label">
        <span>{{ labels[key] || key }}</span
        ><small>已评 {{ value.judged }} · 待评 {{ value.pending }} · 不适用 {{ value.na }}</small>
      </div>
      <div class="eval-quality-track">
        <progress
          v-if="value.judged"
          max="1"
          :value="value.rate ?? 0"
          :aria-label="labels[key] || key"
        />
        <span v-else class="eval-quality-pending">尚未审核</span>
      </div>
      <strong>{{ percent(value.rate) }}</strong>
    </div>
    <h3>本次执行用量</h3>
    <p>
      已知实际 Token {{ quality.usage.known_tokens }} · 已知 {{ quality.usage.known_queries }} 题 /
      未知 {{ quality.usage.unknown_queries }} 题 · 已记录模型调用
      {{ quality.usage.new_model_calls }} 次 · 复用 {{ quality.reused }} 题
    </p>
    <p class="small muted">
      未知用量不补零；复用题不计入本次调用与 Token。供应商未提供价格时不估算金额。
    </p>
    <details>
      <summary>问题归因分布</summary>
      <p v-if="!Object.keys(quality.causes).length">暂无已记录归因。</p>
      <p v-for="(n, key) in quality.causes" :key="key">{{ key }}：{{ n }}</p>
    </details>
  </section>
</template>
