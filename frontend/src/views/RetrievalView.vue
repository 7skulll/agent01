<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { useRouter } from 'vue-router'
import {
  Search,
  SlidersHorizontal,
  ArrowUpRight,
  FileText,
  Clock3,
  Layers,
  ChevronDown,
} from 'lucide-vue-next'
import { api } from '../api/client'
import { modeLabels, type Mode, type SearchResult, type Source } from '../api/types'
import ErrorBanner from '../components/ErrorBanner.vue'
import EvaluationWorkbench from '../features/evaluation/EvaluationWorkbench.vue'
import SourcePanel from '../components/SourcePanel.vue'
import { useKnowledgeBases } from '../features/knowledge/useKnowledgeBases'
const panel = ref('search')
const bases = useKnowledgeBases()
const { activeKnowledgeBaseId } = bases
const router = useRouter()
const query = ref(''),
  mode = ref<Mode>('hybrid'),
  topK = ref(5),
  minScore = ref(0.2),
  expansions = ref(''),
  rerank = ref(''),
  scoped = ref(false),
  fileIds = ref<string[]>([])
const searching = ref(false),
  error = ref<unknown>(null),
  result = ref<SearchResult>(),
  selected = ref<Source | null>(null),
  searchedMode = ref<Mode>('hybrid')
const files = useQuery({
  queryKey: computed(() => ['all-files', activeKnowledgeBaseId.value]),
  enabled: computed(() => !!bases.current.value),
  queryFn: () => api.allFiles(activeKnowledgeBaseId.value),
})
let controller: AbortController | undefined
watch(activeKnowledgeBaseId, () => {
  controller?.abort()
  controller = undefined
  searching.value = false
  result.value = undefined
  selected.value = null
  fileIds.value = []
  error.value = null
})
const degraded = computed(() => result.value?.diagnostics.degraded || [])
function score(value?: number | null) {
  return value == null ? '—' : value.toFixed(4)
}
async function search() {
  if (!query.value.trim() || !bases.current.value) return
  controller?.abort()
  const own = (controller = new AbortController())
  error.value = null
  result.value = undefined
  searching.value = true
  searchedMode.value = mode.value
  try {
    const queries = expansions.value
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    if (queries.length > 2) throw new Error('最多输入两个扩展查询，每行一个。')
    const response = await api.search(
      activeKnowledgeBaseId.value,
      {
        query: query.value.trim(),
        queries,
        mode: mode.value,
        top_k: topK.value,
        ...(mode.value === 'vector' ? { min_score: minScore.value } : {}),
        ...(rerank.value.trim() ? { rerank_query: rerank.value.trim() } : {}),
        ...(scoped.value ? { file_ids: fileIds.value } : {}),
      },
      own.signal,
    )
    if (controller === own && !own.signal.aborted) result.value = response
  } catch (e) {
    if (!own.signal.aborted) error.value = e
  } finally {
    if (controller === own) searching.value = false
  }
}
onScopeDispose(() => controller?.abort())
function ask() {
  if (result.value)
    void router.push({
      path: '/chat',
      query: { question: result.value.query, mode: searchedMode.value },
    })
}
</script>
<template>
  <div class="page retrieval-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">RETRIEVAL EXPLORER</span>
        <h1>检索调试<span class="heading-dot">.</span></h1>
        <p>看见答案背后的召回过程，让每一次检索更清晰。</p>
      </div>
      <span class="subtle-chip"
        ><Layers :size="14" /> {{ bases.current.value?.name || '请先创建知识库' }}</span
      >
    </div>
    <div class="eval-tabs" role="tablist" aria-label="调试工作区">
      <button role="tab" :aria-selected="panel === 'search'" @click="panel = 'search'">
        检索调试
      </button>
      <button role="tab" :aria-selected="panel === 'evaluation'" @click="panel = 'evaluation'">
        RAG 评测
      </button>
    </div>
    <EvaluationWorkbench v-if="panel === 'evaluation'" :kb-id="activeKnowledgeBaseId" />
    <div v-else class="retrieval-layout">
      <form class="card search-form" @submit.prevent="search">
        <div class="section-heading">
          <h2>检索配置</h2>
          <SlidersHorizontal :size="17" class="muted" />
        </div>
        <div class="form-body">
          <label class="field"
            >查询问题<textarea
              v-model="query"
              required
              maxlength="4000"
              rows="4"
              placeholder="例如：员工年假的申请流程是什么？"
            ></textarea></label
          ><label class="field"
            >检索方式<select v-model="mode" aria-label="检索方式">
              <option v-for="(label, value) in modeLabels" :key="value" :value="value">
                {{ label }}
              </option></select
            ><small>{{
              mode === 'hybrid'
                ? '融合语义向量与 BM25 全文召回'
                : mode === 'keyword'
                  ? '直接匹配文档词项，无需 Embedding'
                  : '通过语义相似度查找相关资料'
            }}</small></label
          >
          <div class="field-row">
            <label class="field"
              >返回条数<input
                v-model.number="topK"
                type="number"
                min="1"
                max="20"
                required /></label
            ><label v-if="mode === 'vector'" class="field"
              >最低向量分数<input
                v-model.number="minScore"
                type="number"
                min="-1"
                max="1"
                step="0.05"
                required
            /></label>
          </div>
          <label class="field"
            >检索范围<select v-model="scoped" aria-label="检索范围">
              <option :value="false">全部文件</option>
              <option :value="true">指定文件</option>
            </select></label
          >
          <div v-if="scoped" class="file-options">
            <ErrorBanner :error="files.error.value" /><label
              v-for="file in files.data.value"
              :key="file.file_id"
              ><input v-model="fileIds" type="checkbox" :value="file.file_id" />{{
                file.name
              }}</label
            >
            <p v-if="!fileIds.length" class="muted small">尚未选择文件，本次检索范围为空。</p>
          </div>
          <details class="advanced">
            <summary>高级选项 <ChevronDown :size="14" /></summary>
            <label class="field"
              >扩展查询<textarea
                v-model="expansions"
                maxlength="8001"
                rows="3"
                placeholder="每行一个，最多两个"
              ></textarea></label
            ><label class="field"
              >重排查询（可选）<input
                v-model="rerank"
                maxlength="4000"
                placeholder="留空使用主查询"
            /></label>
            <p class="small muted">重排是否开启由服务端配置决定。</p>
          </details>
          <button
            class="button primary full"
            type="submit"
            :disabled="!query.trim() || !bases.current.value"
          >
            <Search :size="17" />{{ searching ? '重新检索' : '开始检索' }}
          </button>
        </div>
      </form>
      <section class="retrieval-results">
        <ErrorBanner :error="error" />
        <div v-if="searching" class="card empty">
          <span class="loader"></span>
          <h3>正在查找相关资料</h3>
          <p>正在召回、融合并整理结果…</p>
        </div>
        <template v-else-if="result"
          ><div class="result-heading">
            <div>
              <h2>
                检索结果 <span class="count">{{ result.sources.length }}</span>
              </h2>
              <p>
                {{ modeLabels[searchedMode]
                }}<template v-if="result.diagnostics.stage_ms?.total != null">
                  · {{ result.diagnostics.stage_ms.total }} ms</template
                >
              </p>
            </div>
            <button class="button" @click="ask">用这个问题提问 <ArrowUpRight :size="15" /></button>
          </div>
          <div
            v-if="Object.keys(result.diagnostics.unapplied_parameters || {}).length"
            class="notice"
          >
            {{ Object.entries(result.diagnostics.unapplied_parameters || {})
              .map(([name, reason]) => `${name} 未应用：${reason}`).join('；') }}
          </div>
          <div v-if="degraded.length" class="notice warning">
            检索已降级：{{ degraded.join('、') }}。当前结果来自可用路径。
          </div>
          <div v-if="!result.sources.length" class="card empty">
            <Search :size="32" />
            <h3>{{ result.diagnostics.scope_empty ? '检索范围为空' : '没有找到匹配资料' }}</h3>
            <p>
              {{
                result.diagnostics.scope_empty
                  ? '请选择文件，或切换到全部文件。'
                  : '尝试调整问题、检索方式，或检查文件索引状态。'
              }}
            </p>
          </div>
          <article v-for="source in result.sources" :key="source.chunk_id" class="card result-card">
            <div class="result-card-top">
              <span class="result-rank">{{ String(source.citation_id).padStart(2, '0') }}</span>
              <div>
                <strong><FileText :size="15" /> {{ source.name }}</strong
                ><small
                  >{{ source.heading || '正文'
                  }}<template v-if="source.page"> · 第 {{ source.page }} 页</template></small
                >
              </div>
              <div class="channels">
                <span v-for="channel in source.channels" :key="channel" class="subtle-chip">{{
                  channel === 'vector' ? '向量' : channel === 'keyword' ? '全文' : channel
                }}</span>
              </div>
            </div>
            <p class="result-content">{{ source.content }}</p>
            <div class="result-scores">
              <span
                >向量 <b>{{ score(source.vector_score) }}</b></span
              ><span
                >{{ source.character_fallback ? '字符匹配' : 'BM25' }}
                <b>{{ score(source.keyword_score) }}</b></span
              ><span
                >重排 <b>{{ score(source.rerank_score) }}</b></span
              ><button class="text-button" @click="selected = source">
                查看片段 <ArrowUpRight :size="13" />
              </button>
            </div>
          </article>
          <details class="card diagnostics">
            <summary>
              检索诊断
              <span>{{ result.diagnostics.rerank_applied ? '已应用重排' : '未应用重排' }}</span>
            </summary>
            <div class="diagnostics-body">
              <p class="small muted">各通道分数的度量不同，不代表答案可信概率。</p>
              <p>实际查询：{{ result.queries.join(' / ') }}</p>
              <div class="timing-list">
                <span v-for="(ms, stage) in result.diagnostics.stage_ms" :key="stage"
                  ><Clock3 :size="13" /> {{ stage }} <strong>{{ ms }} ms</strong></span
                >
              </div>
              <pre>{{ JSON.stringify(result, null, 2) }}</pre>
            </div>
          </details>
          <p class="small muted">
            跳转问答仅携带原问题和检索方式，问答会独立执行查询规划与检索。
          </p></template
        >
        <div v-else class="retrieval-placeholder">
          <span class="placeholder-icon"><ScanIcon /></span>
          <span class="eyebrow">FOLLOW THE EVIDENCE</span>
          <h2>让每一条线索，都清晰可见。</h2>
          <p>选择检索方式，查看哪些文档被找到，<br />以及它们为什么排在这里。</p>
          <div class="pipeline">
            <span><b>01</b> 查询</span><i>→</i><span><b>02</b> 召回</span><i>→</i
            ><span><b>03</b> 融合</span><i>→</i><span><b>04</b> 重排</span>
          </div>
        </div>
      </section>
    </div>
    <SourcePanel :source="selected" @close="selected = null" />
  </div>
</template>
<script lang="ts">
import { ScanSearch as ScanIcon } from 'lucide-vue-next'
export default { components: { ScanIcon } }
</script>
