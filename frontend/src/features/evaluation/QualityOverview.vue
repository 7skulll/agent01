<script setup lang="ts">
import type { QualitySummary } from './types'
import { percent } from './format'
defineProps<{ quality: QualitySummary }>()
const labels: Record<string, string> = {
  fact_coverage: '必要事实完整覆盖',
  unsupported_claims: '存在无依据事实',
  conditions: '条件与例外正确',
  citation_support: '引用完整支持',
  refusal: '人工拒答判断正确',
}
</script>
<template>
  <section class="card eval-quality">
    <h2>回答质量总览 · 人工审核</h2>
    <p class="small muted">
      仅统计完成的完整 RAG；未审核、不适用和服务失败不会当作通过。原文覆盖与语义质量分别观察。
    </p>
    <div class="eval-outcomes">
      <span>通过 {{ quality.verdicts.pass || 0 }}</span
      ><span>部分 {{ quality.verdicts.partial || 0 }}</span
      ><span>错误 {{ quality.verdicts.fail || 0 }}</span
      ><span>待审核 {{ quality.verdicts.pending || 0 }}</span
      ><strong>已审核通过率 {{ percent(quality.pass_rate) }}</strong>
    </div>
    <div v-for="(value, key) in quality.dimensions" :key="key" class="eval-quality-row">
      <div>
        <span>{{ labels[key] || key }}</span
        ><small>已评 {{ value.judged }} · 待评 {{ value.pending }} · 不适用 {{ value.na }}</small>
      </div>
      <progress max="1" :value="value.rate ?? 0" :aria-label="labels[key] || key" /><strong>{{
        percent(value.rate)
      }}</strong>
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
