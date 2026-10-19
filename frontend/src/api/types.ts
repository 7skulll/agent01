import type { components } from './generated'
type Schema = components['schemas']
export type ModelsInfo = Schema['ModelsInfo']
export type LLMInput = Schema['LLMInput']
export type EmbeddingInput = Schema['EmbeddingInput']
export type KnowledgeBase = Schema['KnowledgeBase']
export type AnswerSegment = Schema['AnswerSegment']
export type Mode = 'hybrid' | 'keyword' | 'vector'
export type Conversation = Schema['Conversation']
export type Run = Omit<Schema['ConversationTurn'], 'sources'> & { sources: Source[] }
export type Turns = Omit<Schema['ConversationTurns'], 'items'> & { items: Run[] }
export type FileItem = Schema['FileSummary']
export type FileList = Schema['FileListResponse']
export type Health = Schema['HealthResponse']
export interface Source {
  citation_id: number
  chunk_id: string
  file_id: string
  name: string
  content: string
  heading?: string | null
  page?: number | null
  kind?: string
  score?: number
  vector_score?: number | null
  keyword_score?: number | null
  rerank_score?: number | null
  channels?: string[]
  matched_queries?: string[]
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
export interface SearchResult {
  query: string
  queries: string[]
  sources: Source[]
  index_version?: string
  lexical_version?: string
  diagnostics: {
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
