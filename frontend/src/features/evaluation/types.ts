import type { Mode, Source, Run } from '../../api/types'
export interface EvidenceGroup {
  fact: string
  alternatives: { file_id: string; content_hash: string; quote: string; page: number | null }[]
}
export interface AnswerMetric {
  key: 'faithfulness' | 'answer_relevancy' | 'correctness' | 'completeness'
  label: string
  definition: string
  source_field: string
  positive_value: string
  counts: Record<string, number>
  numerator: number
  denominator: number
  judged: number
  pending: number
  na: number
  value: number | null
  acceptable?: number
  acceptable_rate?: number | null
}
export interface QualitySummary {
  total: number
  answer_queries: number
  verdicts: Record<string, number>
  pass_rate: number | null
  metrics?: Record<
    'faithfulness' | 'answer_relevancy' | 'correctness' | 'completeness',
    AnswerMetric
  >
  dimensions: Record<string, { judged: number; pending: number; na: number; rate: number | null }>
  causes: Record<string, number>
  reused: number
  usage: {
    known_tokens: number
    known_queries: number
    unknown_queries: number
    new_model_calls: number
  }
}
export interface GenerationMetric {
  value: number | null
  judged: number
  missing: number
  source: 'ragas'
}
export interface GenerationSummary {
  available: boolean
  judged_queries: number
  attempted_queries?: number
  errors: number
  metrics: Record<
    'faithfulness' | 'answer_relevancy' | 'answer_correctness' | 'completeness',
    GenerationMetric
  >
}
export interface HistoryItem {
  evaluation_id: string
  status: string
  created_at: string
  kind: string
  kb_id: string
  archived?: boolean
  mode?: Mode
}
export interface HistoryPage {
  items: HistoryItem[]
  total: number
  limit: number
  offset: number
}
export interface ExperimentOptions {
  rerank: 'inherit' | 'on' | 'off'
  context_tokens: number | null
  judge?: 'none' | 'ragas'
}
export interface CaseSet {
  split?: 'development' | 'acceptance'
  review_status?: 'draft' | 'verified'
  frozen?: boolean
  archived?: boolean
  case_set_id: string
  kb_id?: string
  kb_name?: string
  name: string
  version: number
  tags: string[]
  cases: EvalCase[]
  created_at?: string
  case_count?: number
  corpus_count?: number
  fingerprint?: string
}
export interface AnswerReview {
  verdict: string
  notes: string
  fact_coverage?: string
  unsupported_claims?: string
  conditions?: string
  answer_relevancy?: string
  citation_support?: string
  refusal?: string
  causes?: string[]
  reviewer?: string | null
  reviewed_at?: string | null
  review_method?: 'human' | 'ai_proxy' | 'carried' | null
  carried_from?: string | null
}
export interface AnswerMetricComparison {
  label: string
  left: number | null
  right: number | null
  delta: number | null
  paired: number
  unpaired: number
  definition: string
}
export interface Comparison {
  rows: {
    query: string
    delta: Scores
    answer?: Record<
      'faithfulness' | 'answer_relevancy' | 'correctness' | 'completeness',
      { left: number | null; right: number | null; delta: number | null }
    >
    outcome: string
    elapsed_ms_delta: number | null
    tokens_delta: number | null
  }[]
  counts: Record<string, number>
  metric_delta: Scores
  answer_metrics?: Record<
    'faithfulness' | 'answer_relevancy' | 'correctness' | 'completeness',
    AnswerMetricComparison
  >
  config_changes: Record<string, unknown>
  code_changed: boolean
  modes: string[]
  experiment_changes?: Record<string, unknown>
  left_id?: string
  right_id?: string
}
export interface EvalCase {
  query: string
  expected: 'unlabelled' | 'answerable' | 'unanswerable'
  relevant_file_ids: string[]
  required_facts: string
  reference_answer?: string
  judgment_scope?: 'full' | 'partial'
  negative_file_ids?: string[]
  tags?: string[]
  evidence_groups?: EvidenceGroup[]
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
  reused_from?: string
  status: string
  sources: Source[]
  context_sources?: Source[]
  diagnostics?: {
    trace?: { before_rerank: Source[]; after_rerank: Source[] }
    stage_ms?: Record<string, number>
    degraded?: string[]
    rerank_applied?: boolean
    deduplicated_count?: number
    fused_count?: number
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
  judge?: {
    provider: string
    judged_at: string
    ragas_version?: string
    worker?: string
    judge_model?: string
    thinking_mode?: 'disabled' | 'default'
    judge_max_tokens?: number
    scoring_phase?: 'post_run_backfill'
    faithfulness: number | null
    answer_relevancy: number | null
    answer_correctness: number | null
    completeness: number | null
  }
  judge_error?: string
  judge_status?: 'running' | 'done' | 'partial' | 'failed'
  elapsed_ms?: number
  error?: { code: string; message: string }
  judgments?: {
    judged: number
    returned: number
    coverage: number | null
    judged_precision: number | null
  }
  evidence?: {
    valid: boolean
    context_complete: boolean | null
    causes: string[]
    coverage: Record<string, number | null>
    groups: {
      fact: string
      stages: Record<string, boolean>
      alternatives: { file_id: string; quote: string; anchor_status: string }[]
    }[]
  }
  review: AnswerReview
}
export interface EvalRecord {
  evaluation_id: string
  quality?: QualitySummary
  generation?: GenerationSummary
  archived?: boolean
  status: string
  created_at: string
  request: {
    options?: ExperimentOptions
    retry_of?: string | null
    case_set_id?: string | null
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
    case_set?: {
      name: string
      version: number
      tags?: string[]
      frozen?: boolean
      split?: CaseSet['split']
      review_status?: CaseSet['review_status']
    }
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
    retrieval_scored_failures: number
    completed: number
    failed: number
    cancelled: number
    refusal_correct: number
    refusal_total: number
    false_refusals: number
    answerable_total: number
    reviewed: number
    partial_label_queries?: number
    refusal_attempted?: number
    context_evidence_queries?: number
    context_evidence_complete?: number
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
