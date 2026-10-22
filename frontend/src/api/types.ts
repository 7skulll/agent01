import type { components } from './generated'
type Schema = components['schemas']
export type ModelsInfo = Schema['ModelsInfo']
export type LLMInput = Schema['LLMInput']
export type EmbeddingInput = Schema['EmbeddingInput']
export type KnowledgeBase = Schema['KnowledgeBase']
export type AnswerSegment = Schema['AnswerSegment']
export type Mode = Schema['RunRequest']['retrieval_mode']
export type Conversation = Schema['Conversation']
export type Run = Omit<Schema['ConversationTurn'], 'sources'> & { sources: Source[] }
export type Turns = Omit<Schema['ConversationTurns'], 'items'> & { items: Run[] }
export type FileItem = Schema['FileSummary']
export type FileList = Schema['FileListResponse']
export type Health = Schema['HealthResponse']
type GeneratedSource = Schema['Source']
export type Source = Omit<
  GeneratedSource,
  | 'heading'
  | 'page'
  | 'kind'
  | 'chunk_index'
  | 'score'
  | 'truncated'
  | 'supporting_chunks'
  | 'alternatives'
> & {
  heading?: string | null
  page?: number | null
  kind?: string
  chunk_index?: number
  score?: number
  truncated?: boolean
  evidence_chunks?: {
    chunk_id: string
    chunk_index: number
    page?: number | null
    start: number
    end: number
    content: string
    role: 'main' | 'neighbor'
  }[]
  supporting_chunks?: { content: string; chunk_id?: string }[]
  alternatives?: unknown[]
}
export type SearchResult = Omit<
  Schema['SearchResponse'],
  'sources' | 'queries' | 'index_version' | 'diagnostics'
> & {
  queries: string[]
  sources: Source[]
  index_version?: string
  diagnostics: {
    unapplied_parameters?: Record<string, string>
    degraded?: string[]
    stage_ms?: Record<string, number>
    rerank_applied?: boolean
    scope_empty?: boolean
    [key: string]: unknown
  }
}
export const modeLabels: Record<Mode, string> = {
  hybrid: '混合检索',
  keyword: '全文检索',
  vector: '向量检索',
}
export const modeDescriptions: Record<Mode, string> = {
  hybrid: '结合语义与关键词，适合大多数问题',
  keyword: '按关键词查找名称、编号与具体条款',
  vector: '按语义查找相关内容与相近表达',
}
export const modeOptions = (Object.keys(modeLabels) as Mode[]).map((value) => ({
  value,
  label: modeLabels[value],
  description: modeDescriptions[value],
}))
export const statusLabels: Record<string, string> = {
  queued: '等待执行',
  running: '运行中',
  paused: '已暂停',
  recovering: '等待恢复',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  not_indexed: '待索引',
  parsing: '解析中',
  recorded: '已切片',
  indexing: '向量化中',
  indexed: '向量就绪',
}
export const activeStatuses = new Set(['queued', 'running'])
export const openStatuses = new Set(['queued', 'running', 'paused', 'recovering'])
