<script setup lang="ts">
import { computed, ref } from 'vue'
import { date } from '../../api/client'
import type { CaseSet } from './types'

const props = defineProps<{
  sets: CaseSet[]
  currentKbId: string
  preview?: CaseSet | null
  busy: boolean
  active: boolean
  previewLoading?: boolean
  canWrite: boolean
}>()
const emit = defineEmits<{
  (event: 'preview', set: CaseSet): void
  (event: 'load', set: CaseSet): void
  (event: 'test', set: CaseSet): void
  (event: 'import', file: File): void
  (event: 'refresh'): void
  (event: 'export', set: CaseSet): void
  (event: 'close-preview'): void
}>()
const importing = ref<HTMLInputElement>()
const scope = ref<'current' | 'all'>('current')
const caseCount = (set: CaseSet) => set.case_count ?? set.cases?.length ?? 0
const corpusCount = (set: CaseSet) => set.corpus_count ?? 0
const visibleSets = computed(() =>
  scope.value === 'current'
    ? props.sets.filter((set) => !set.kb_id || set.kb_id === props.currentKbId)
    : props.sets,
)
const totalCases = computed(() => visibleSets.value.reduce((sum, set) => sum + caseCount(set), 0))
const verifiedCount = computed(
  () => visibleSets.value.filter((set) => set.review_status === 'verified').length,
)
const frozenCount = computed(() => visibleSets.value.filter((set) => set.frozen).length)
const splitLabel = (set: CaseSet) => (set.split === 'acceptance' ? '验收' : '开发')
const reviewLabel = (set: CaseSet) =>
  set.review_status === 'verified' ? '人工已核验' : '草稿待核验'
const expectedLabel: Record<string, string> = {
  answerable: '有答案',
  unanswerable: '应拒答',
  unlabelled: '未标注',
}
const scopeLabel: Record<string, string> = {
  full: '完整标注',
  partial: '部分标注',
}
function pickFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (file) emit('import', file)
  ;(event.target as HTMLInputElement).value = ''
}
</script>

<template>
  <section class="card eval-dataset-library">
    <div class="eval-dataset-heading">
      <div>
        <h2>测评集</h2>
        <p class="small muted">
          显式查看当前知识库或全部知识库的测评集，包括历史创建的 CRUD-RAG 测评集。导入 JSON
          会校验标注文件是否属于目标知识库，并保存为可复用版本。
        </p>
      </div>
      <div class="eval-actions">
        <div class="eval-dataset-scope" role="group" aria-label="测评集范围">
          <button type="button" :class="{ active: scope === 'current' }" @click="scope = 'current'">
            当前知识库
          </button>
          <button type="button" :class="{ active: scope === 'all' }" @click="scope = 'all'">
            全部知识库
          </button>
        </div>
        <button class="button" type="button" :disabled="busy || active" @click="emit('refresh')">
          刷新
        </button>
        <button
          v-if="canWrite"
          class="button primary"
          type="button"
          :disabled="busy || active"
          @click="importing?.click()"
        >
          导入测评集 JSON
        </button>
        <input
          v-if="canWrite"
          ref="importing"
          type="file"
          accept=".json,application/json"
          class="sr-only"
          aria-label="导入测评集 JSON"
          @change="pickFile"
        />
      </div>
    </div>

    <div class="eval-dataset-stats" aria-label="测评集统计">
      <span
        ><strong>{{ visibleSets.length }}</strong> 个测评集</span
      >
      <span
        ><strong>{{ totalCases }}</strong> 道案例</span
      >
      <span
        ><strong>{{ verifiedCount }}</strong> 个人工核验</span
      >
      <span
        ><strong>{{ frozenCount }}</strong> 个冻结版本</span
      >
    </div>

    <div v-if="!visibleSets.length" class="eval-dataset-empty">
      <strong>{{ scope === 'current' ? '当前知识库还没有测评集' : '还没有测评集' }}</strong>
      <p v-if="scope === 'current' && sets.length">
        其他知识库已有 {{ sets.length }} 个测评集，切换到“全部知识库”即可查看和载入。
      </p>
      <p v-else-if="canWrite">
        可以导入 JSON
        测评集，或在下方“保存当前草稿为测评集”中填写名称后保存。导入格式支持案例数组，或包含
        <code>name</code>、<code>tags</code>、<code>cases</code> 的对象。
      </p>
      <p v-else>请联系员工或管理员创建测评集。</p>
    </div>

    <div v-else class="table-scroll">
      <table class="eval-table eval-dataset-table">
        <thead>
          <tr>
            <th>测评集</th>
            <th v-if="scope === 'all'">知识库</th>
            <th>用途</th>
            <th>审核状态</th>
            <th>案例</th>
            <th>语料</th>
            <th>标签</th>
            <th>创建</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="set in visibleSets" :key="set.case_set_id">
            <td>
              <strong>{{ set.name }}</strong>
              <small class="eval-dataset-version">v{{ set.version }}</small>
              <small class="eval-dataset-id">{{ set.case_set_id.slice(0, 8) }}</small>
            </td>
            <td v-if="scope === 'all'">
              <span class="eval-dataset-kb">{{ set.kb_name || set.kb_id || '未知知识库' }}</span>
            </td>
            <td>
              <span class="badge" :class="set.split === 'acceptance' ? 'warning' : 'muted'">{{
                splitLabel(set)
              }}</span>
            </td>
            <td>
              <span class="badge" :class="set.review_status === 'verified' ? 'success' : 'muted'">{{
                reviewLabel(set)
              }}</span>
              <span v-if="set.frozen" class="badge warning">已冻结</span>
            </td>
            <td>{{ caseCount(set) }}</td>
            <td>{{ corpusCount(set) }} 文件</td>
            <td class="eval-dataset-tags">
              <span v-for="tag in set.tags.slice(0, 3)" :key="tag">{{ tag }}</span>
              <small v-if="set.tags.length > 3">+{{ set.tags.length - 3 }}</small>
              <small v-if="!set.tags.length">无</small>
            </td>
            <td>{{ set.created_at ? date(set.created_at) : '—' }}</td>
            <td>
              <div class="eval-dataset-actions">
                <button class="text-button" type="button" @click="emit('preview', set)">
                  查看
                </button>
                <button
                  class="text-button"
                  type="button"
                  :disabled="busy || active"
                  @click="emit('load', set)"
                >
                  载入
                </button>
                <button
                  v-if="canWrite"
                  class="text-button strong"
                  type="button"
                  :disabled="busy || active"
                  @click="emit('test', set)"
                >
                  载入并测试
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <section v-if="preview" class="eval-dataset-preview" aria-label="测评集预览">
      <div class="eval-dataset-preview-heading">
        <div>
          <h3>{{ preview.name }} · v{{ preview.version }}</h3>
          <p class="small muted">
            {{ preview.kb_name || preview.kb_id || '当前知识库' }} · {{ splitLabel(preview) }} ·
            {{ reviewLabel(preview) }} · {{ preview.frozen ? '已冻结' : '未冻结' }} ·
            {{ preview.cases?.length || 0 }} 道案例 · {{ corpusCount(preview) }} 个语料文件
          </p>
        </div>
        <div class="eval-actions">
          <button
            class="button"
            type="button"
            :disabled="busy || active"
            @click="emit('load', preview)"
          >
            载入此测评集
          </button>
          <button
            v-if="canWrite"
            class="button primary"
            type="button"
            :disabled="busy || active"
            @click="emit('test', preview)"
          >
            载入并测试
          </button>
          <button class="button" type="button" @click="emit('export', preview)">导出 JSON</button>
          <button class="text-button" type="button" @click="emit('close-preview')">关闭</button>
        </div>
      </div>
      <p v-if="previewLoading" class="small muted">正在读取案例明细…</p>
      <div class="table-scroll">
        <table class="eval-table">
          <thead>
            <tr>
              <th>#</th>
              <th>问题</th>
              <th>预期</th>
              <th>标注范围</th>
              <th>相关文件</th>
              <th>必要证据</th>
              <th>标签</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in preview.cases || []" :key="index">
              <td>{{ index + 1 }}</td>
              <td>{{ item.query }}</td>
              <td>{{ expectedLabel[item.expected] || item.expected }}</td>
              <td>{{ scopeLabel[item.judgment_scope || 'full'] || item.judgment_scope }}</td>
              <td>{{ item.relevant_file_ids.length }}</td>
              <td>{{ item.evidence_groups?.length || 0 }}</td>
              <td class="eval-dataset-tags">
                <span v-for="tag in (item.tags || []).slice(0, 4)" :key="tag">{{ tag }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="preview.fingerprint" class="small muted">
        指纹：<code>{{ preview.fingerprint }}</code>
      </p>
    </section>
  </section>
</template>
