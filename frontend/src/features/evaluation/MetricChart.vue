<script setup lang="ts">
import { computed } from 'vue'
import { metricLabels, type Scores } from './types'
import { percent } from './format'
const props = defineProps<{ curves: (Scores & { k: number })[] }>()
const colors = { precision: '#24745d', recall: '#0c927e', hit: '#b66920' }
const keys = ['precision', 'recall', 'hit'] as const
const points = (key: keyof Scores) =>
  props.curves.map((r, i) => `${45 + i * 110},${155 - (r[key] || 0) * 130}`).join(' ')
const labelled = computed(() => props.curves.some((r) => r.recall !== null))
</script>
<template>
  <div class="metric-chart">
    <h3>检索质量随 K 的变化</h3>
    <p class="small muted">文件级、去重排名；未返回位置按未命中计算。悬停数据点查看数值。</p>
    <svg
      v-if="labelled"
      viewBox="0 0 520 195"
      role="img"
      aria-label="精准率、召回率和命中率随 K 的变化"
    >
      <g v-for="tick in [0, 0.5, 1]" :key="tick">
        <line x1="45" x2="485" :y1="155 - tick * 130" :y2="155 - tick * 130" stroke="#e9edf3" />
        <text x="6" :y="159 - tick * 130" font-size="11" fill="#707b8c">{{ tick * 100 }}%</text>
      </g>
      <g v-for="key in keys" :key="key">
        <polyline
          :points="points(key)"
          :stroke-dasharray="key === 'hit' ? '6 4' : undefined"
          fill="none"
          :stroke="colors[key]"
          stroke-width="2.5"
        />
        <circle
          v-for="(r, i) in curves"
          :key="r.k"
          :cx="45 + i * 110"
          :cy="155 - (r[key] || 0) * 130"
          r="4"
          :fill="colors[key]"
        >
          <title>{{ metricLabels[key] }}@{{ r.k }}：{{ percent(r[key], '未标注') }}</title>
        </circle>
      </g>
      <text
        v-for="(r, i) in curves"
        :key="r.k"
        :x="45 + i * 110"
        y="180"
        text-anchor="middle"
        font-size="12"
        fill="#707b8c"
      >
        K={{ r.k }}
      </text>
    </svg>
    <p v-else class="notice">没有已执行的有答案标注题，暂不计算检索质量。</p>
    <div class="eval-legend">
      <span v-for="key in keys" :key="key"
        ><i :style="{ background: colors[key] }" />{{ metricLabels[key] }}</span
      >
    </div>
    <details>
      <summary>查看数值表与口径</summary>
      <table class="eval-table">
        <thead>
          <tr>
            <th>K</th>
            <th>精准率</th>
            <th>召回率</th>
            <th>命中率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in curves" :key="r.k">
            <td>{{ r.k }}</td>
            <td v-for="key in keys" :key="key">
              {{ percent(r[key], '未标注') }}
            </td>
          </tr>
        </tbody>
      </table>
      <p class="small muted">
        Precision@K=相关文件数/K；Recall@K=相关文件数/标注相关文件总数；Hit@K=至少命中一个相关文件的题目比例。K
        超过实际返回数时以空位补足。失败的有标签题计零，取消和未执行题不计入。
      </p>
    </details>
  </div>
</template>
