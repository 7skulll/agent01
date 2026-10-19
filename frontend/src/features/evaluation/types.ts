import type { Mode, Source, Run } from '../../api/types'
export interface EvalCase {
  query: string
  expected: 'unlabelled' | 'answerable' | 'unanswerable'
  relevant_file_ids: string[]
  required_facts: string
}
export interface Scores {
  precision: number | null
  recall: number | null
  hit: number | null
  mrr: number | null
  ndcg: number | null
}
export interface EvalResult {
  case: EvalCase
  status: string
  sources: Source[]
  context_sources?: Source[]
  diagnostics?: {
    trace?: { before_rerank: Source[]; after_rerank: Source[] }
    stage_ms?: Record<string, number>
    degraded?: string[]
    rerank_applied?: boolean
  }
  assembly?: Record<string, number | string | object>
  run?: Run
  query_plan?: { queries: string[]; rerank_query?: string; degraded?: boolean }
  token_usage?: {
    duration_ms?: number
    stage: string
    actual?: { input_tokens?: number; output_tokens?: number }
  }[]
  metrics?: Scores | null
  elapsed_ms?: number
  error?: { code: string; message: string }
  review: { verdict: string; notes: string }
}
export interface EvalRecord {
  evaluation_id: string
  status: string
  created_at: string
  request: {
    knowledge_base_id: string
    kind: string
    mode: Mode
    k: number
    cases: EvalCase[]
    file_ids: string[] | null
  }
  results: EvalResult[]
  error?: { code: string; message: string }
  snapshot: {
    fingerprint: string
    files: { file_id: string; name: string; content_hash: string }[]
    config: Record<string, unknown>
    code_sha256: string
  }
  summary: {
    metrics: Scores
    curves: (Scores & { k: number })[]
    metric_queries: number
    labelled_failures: number
    completed: number
    failed: number
    cancelled: number
    refusal_correct: number
    refusal_total: number
    false_refusals: number
    answerable_total: number
    reviewed: number
  }
}
export const metricLabels: Record<keyof Scores, string> = {
  precision: '精准率',
  recall: '召回率',
  hit: '命中率',
  mrr: 'MRR',
  ndcg: 'nDCG',
}
export const evaluationStatus: Record<string, string> = {
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
  interrupted: '重启中断',
  cancelled: '已取消',
}
