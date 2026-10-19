<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  BrainCircuit,
  Database,
  KeyRound,
  Save,
  RotateCcw,
  Check,
  ArrowRight,
} from 'lucide-vue-next'
import { api } from '../api/client'
import ErrorBanner from '../components/ErrorBanner.vue'
import type { ModelsInfo } from '../api/types'
const client = useQueryClient()
const query = useQuery({ queryKey: ['model-settings'], queryFn: api.modelSettings })
const llm = reactive({
  provider: 'openai' as 'openai' | 'deepseek',
  model: '',
  base_url: '',
  api_key: '',
  max_tokens: 1024,
  input_tokens: 8192,
})
const embedding = reactive({
  model: '',
  base_url: '',
  api_key: '',
  dimension: 0,
  max_input_tokens: 0,
})
const saving = ref<'llm' | 'embedding' | ''>('')
const error = ref<unknown>(null)
const message = ref('')
const reindex = ref(false)
const loaded = computed(() => !!query.data.value)
const initialized = ref(false)
function populate(value: ModelsInfo, kind?: 'llm' | 'embedding') {
  if (!kind || kind === 'llm') Object.assign(llm, { ...value.llm, api_key: '' })
  if (!kind || kind === 'embedding') Object.assign(embedding, { ...value.embedding, api_key: '' })
}
watch(
  query.data,
  (value) => {
    if (value && !initialized.value) {
      populate(value)
      initialized.value = true
    }
  },
  { immediate: true },
)
async function save(kind: 'llm' | 'embedding', reset = false) {
  if (saving.value) return
  saving.value = kind
  error.value = null
  message.value = ''
  try {
    const value = reset
      ? await api.resetModel(kind)
      : kind === 'llm'
        ? await api.saveLLM({
            provider: llm.provider,
            model: llm.model,
            base_url: llm.base_url,
            api_key: llm.api_key || undefined,
            max_tokens: llm.max_tokens,
            input_tokens: llm.input_tokens,
          })
        : await api.saveEmbedding({
            model: embedding.model,
            base_url: embedding.base_url,
            api_key: embedding.api_key || undefined,
            dimension: embedding.dimension,
            max_input_tokens: embedding.max_input_tokens,
          })
    client.setQueryData(['model-settings'], value)
    populate(value, kind)
    reindex.value = value.embedding_reindex_required
    message.value = reset
      ? '已恢复服务端环境配置。'
      : `${kind === 'llm' ? '对话模型' : 'Embedding 模型'}已保存，即刻生效。`
    await Promise.all([
      client.invalidateQueries({ queryKey: ['health'] }),
      client.invalidateQueries({ queryKey: ['files'] }),
    ])
  } catch (e) {
    error.value = e
  } finally {
    saving.value = ''
  }
}
</script>
<template>
  <div class="page settings-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">MAKE IT YOURS</span>
        <h1>模型设置<span class="heading-dot">.</span></h1>
        <p>连接你自己的模型，让知识在熟悉的服务中流动。</p>
      </div>
      <span class="subtle-chip"><KeyRound :size="14" /> 自定义 API 接入</span>
    </div>
    <div class="settings-note">
      <KeyRound :size="18" />
      <p>
        API Key
        仅保存在当前服务端数据库，不会回传或写入浏览器存储。此处配置对整个工作空间生效；有未结束的问答或索引任务时，请先完成或取消。
      </p>
    </div>
    <ErrorBanner :error="error || query.error.value" />
    <div v-if="message" class="notice" role="status"><Check :size="15" /> {{ message }}</div>
    <div v-if="reindex" class="notice warning" role="status">
      Embedding 配置已切换，请到知识库重新构建索引后使用新的向量检索。<RouterLink
        to="/knowledge"
        class="text-button"
        >前往知识库 <ArrowRight :size="14"
      /></RouterLink>
    </div>
    <p v-if="query.isPending.value" class="muted">正在读取模型配置…</p>
    <div v-else-if="loaded" class="model-settings-grid">
      <form
        class="card model-settings-card"
        aria-label="对话模型配置"
        @submit.prevent="save('llm')"
      >
        <div class="section-heading">
          <div class="model-title">
            <span class="stat-icon green"><BrainCircuit :size="21" /></span>
            <div>
              <h2>对话模型</h2>
              <p>用于普通对话、知识问答与查询改写</p>
            </div>
          </div>
          <span class="badge" :class="query.data.value?.llm.ready ? 'completed' : 'muted'">{{
            query.data.value?.llm.ready ? '已配置' : '待配置'
          }}</span>
        </div>
        <div class="form-body">
          <label class="field"
            >接口类型<select v-model="llm.provider" aria-label="对话接口类型">
              <option value="openai">OpenAI 兼容</option>
              <option value="deepseek">DeepSeek</option></select
            ><small
              >服务商需支持所选协议；普通对话需要工具调用，知识问答使用 JSON mode。</small
            ></label
          >
          <label class="field"
            >服务地址<input
              v-model.trim="llm.base_url"
              aria-label="对话服务地址"
              type="url"
              required
              maxlength="2048"
              placeholder="https://your-provider.example/v1"
              autocomplete="off"
            /><small>填写 API Base URL，包含服务商要求的版本路径。</small></label
          >
          <label class="field"
            >模型名称<input
              v-model.trim="llm.model"
              aria-label="对话模型名称"
              required
              maxlength="200"
              placeholder="填写服务商提供的模型 ID"
              autocomplete="off"
          /></label>
          <label class="field"
            >API Key
            <span v-if="query.data.value?.llm.key_configured" class="key-saved"
              ><Check :size="12" /> 已设置，留空保留</span
            ><input
              v-model="llm.api_key"
              aria-label="对话 API Key"
              type="password"
              :required="!query.data.value?.llm.key_configured"
              autocomplete="new-password"
              :placeholder="
                query.data.value?.llm.key_configured ? '留空保留现有密钥' : '请输入你的 API Key'
              "
          /></label>
          <div class="field-row">
            <label class="field"
              >上下文窗口<input
                v-model.number="llm.input_tokens"
                aria-label="对话上下文窗口"
                type="number"
                required
                min="512"
                max="131072" /></label
            ><label class="field"
              >最大输出 Token<input
                v-model.number="llm.max_tokens"
                aria-label="最大输出 Token"
                type="number"
                required
                min="1"
                max="8192"
            /></label>
          </div>
          <div class="model-actions">
            <button
              type="button"
              class="text-button"
              :disabled="!!saving || query.data.value?.llm.source !== 'custom'"
              @click="save('llm', true)"
            >
              <RotateCcw :size="13" /> 恢复环境配置</button
            ><button class="button primary" :disabled="!!saving">
              <Save :size="14" />{{ saving === 'llm' ? '保存中…' : '保存对话模型' }}
            </button>
          </div>
        </div>
      </form>
      <form
        class="card model-settings-card"
        aria-label="Embedding 模型配置"
        @submit.prevent="save('embedding')"
      >
        <div class="section-heading">
          <div class="model-title">
            <span class="stat-icon blue"><Database :size="21" /></span>
            <div>
              <h2>Embedding 模型</h2>
              <p>用于文档向量化与语义检索</p>
            </div>
          </div>
          <span class="badge" :class="query.data.value?.embedding.ready ? 'completed' : 'muted'">{{
            query.data.value?.embedding.ready ? '已配置' : '待配置'
          }}</span>
        </div>
        <div class="form-body">
          <div class="model-protocol">
            <span class="subtle-chip">OpenAI 兼容 Embeddings API</span>
            <p>文档与查询共享此配置。更换模型或服务地址后，需要重新构建知识库索引。</p>
          </div>
          <label class="field"
            >服务地址<input
              v-model.trim="embedding.base_url"
              aria-label="Embedding 服务地址"
              type="url"
              required
              maxlength="2048"
              placeholder="https://your-provider.example/v1"
              autocomplete="off"
          /></label>
          <label class="field"
            >模型名称<input
              v-model.trim="embedding.model"
              aria-label="Embedding 模型名称"
              required
              maxlength="200"
              placeholder="填写 Embedding 模型 ID"
              autocomplete="off"
          /></label>
          <label class="field"
            >API Key
            <span v-if="query.data.value?.embedding.key_configured" class="key-saved"
              ><Check :size="12" /> 已设置，留空保留</span
            ><input
              v-model="embedding.api_key"
              aria-label="Embedding API Key"
              type="password"
              :required="!query.data.value?.embedding.key_configured"
              autocomplete="new-password"
              :placeholder="
                query.data.value?.embedding.key_configured
                  ? '留空保留现有密钥'
                  : '请输入你的 API Key'
              "
          /></label>
          <div class="field-row">
            <label class="field"
              >向量维度<input
                v-model.number="embedding.dimension"
                aria-label="向量维度"
                type="number"
                required
                min="0"
                max="16000"
              /><small>0 表示自动推断；HNSW 需明确填写维度。</small></label
            ><label class="field"
              >单条输入 Token 上限<input
                v-model.number="embedding.max_input_tokens"
                aria-label="Embedding 输入上限"
                type="number"
                required
                min="1"
                max="1000000"
              /><small>按所用服务的模型限制填写。</small></label
            >
          </div>
          <div class="model-actions">
            <button
              type="button"
              class="text-button"
              :disabled="!!saving || query.data.value?.embedding.source !== 'custom'"
              @click="save('embedding', true)"
            >
              <RotateCcw :size="13" /> 恢复环境配置</button
            ><button class="button primary" :disabled="!!saving">
              <Save :size="14" />{{ saving === 'embedding' ? '保存中…' : '保存 Embedding' }}
            </button>
          </div>
        </div>
      </form>
    </div>
    <p class="settings-footnote">
      保存仅更新连接配置，不会向服务商发送请求。模型服务需支持相应接口能力；“已配置”不代表远端连通性已验证。
    </p>
  </div>
</template>
