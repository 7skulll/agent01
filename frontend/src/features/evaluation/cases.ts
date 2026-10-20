import type { EvalCase, EvalResult } from './types'

export const blankCase = (): EvalCase => ({
  query: '',
  expected: 'unlabelled',
  relevant_file_ids: [],
  required_facts: '',
  judgment_scope: 'full',
  negative_file_ids: [],
  tags: [],
  evidence_groups: [],
})

export const normalizeCases = (rows: EvalCase[]) =>
  rows.map((c) => ({ ...blankCase(), ...JSON.parse(JSON.stringify(c)) }))

// JSON is untrusted before it reaches nested editors. Validate before replacing the draft.
export function parseCases(data: unknown): EvalCase[] {
  const object = (v: unknown): Record<string, unknown> => {
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('案例字段格式无效。')
    return v as Record<string, unknown>
  }
  const text = (v: unknown, min: number, max: number): v is string =>
    typeof v === 'string' && v.trim().length >= min && v.length <= max
  const list = (v: unknown, max: number): unknown[] => {
    if (!Array.isArray(v) || v.length > max) throw new Error('案例列表字段格式或数量无效。')
    return v
  }
  const strings = (v: unknown, max: number, length: number): string[] => {
    const rows = list(v, max)
    if (!rows.every((v) => text(v, 1, length))) throw new Error('案例文件或标签格式无效。')
    return rows as string[]
  }
  const rows = list(Array.isArray(data) ? data : object(data).cases, 20)
  if (!rows.length) throw new Error('请导入 1–20 条案例。')
  const cases = rows.map((value) => {
    const row = object(value)
    if (Object.keys(row).some((key) => !Object.keys(blankCase()).includes(key)))
      throw new Error('案例包含不支持的字段。')
    const c = { ...blankCase(), ...row } as EvalCase
    if (
      !text(c.query, 1, 4000) ||
      !['unlabelled', 'answerable', 'unanswerable'].includes(c.expected) ||
      !['full', 'partial'].includes(c.judgment_scope!) ||
      !text(c.required_facts, 0, 2000)
    )
      throw new Error('问题、预期或标注范围格式无效。')
    const positive = strings(c.relevant_file_ids, 100, 128)
    const negative = strings(c.negative_file_ids, 100, 128)
    strings(c.tags, 10, 80)
    if (
      (c.expected === 'answerable') !== !!positive.length ||
      negative.some((id) => positive.includes(id))
    )
      throw new Error('有答案题须标注相关文件，正负标注不能重叠。')
    for (const value of list(c.evidence_groups, 10)) {
      const group = object(value)
      if (!text(group.fact, 1, 500)) throw new Error('必要事实格式无效。')
      const alternatives = list(group.alternatives, 5)
      if (!alternatives.length || c.expected !== 'answerable')
        throw new Error('必要事实须有证据且属于有答案题。')
      for (const value of alternatives) {
        const a = object(value)
        if (
          typeof a.file_id !== 'string' ||
          !positive.includes(a.file_id) ||
          typeof a.content_hash !== 'string' ||
          !/^[a-f0-9]{64}$/.test(a.content_hash) ||
          !text(a.quote, 4, 2000) ||
          (a.page != null && (!Number.isInteger(a.page) || Number(a.page) < 1))
        )
          throw new Error('原文证据的文件、哈希、短句或页码格式无效。')
      }
    }
    return c
  })
  return normalizeCases(cases)
}

export function actualTokens(
  result: Pick<EvalResult, 'token_usage'> & { run?: { model_calls?: number } },
): number | null {
  const events = result.token_usage || []
  if (!events.length) return result.run?.model_calls === 0 ? 0 : null
  if (
    events.some(
      (e) =>
        ![e.actual?.input_tokens, e.actual?.output_tokens].every(
          (v) => typeof v === 'number' && Number.isInteger(v) && v >= 0,
        ),
    )
  )
    return null
  return events.reduce((sum, e) => sum + e.actual!.input_tokens! + e.actual!.output_tokens!, 0)
}

export function caseSetSignature(
  cases: EvalCase[],
  metadata: {
    name: string
    tags: string[]
    split?: string
    review_status?: string
    frozen?: boolean
  },
): string {
  return JSON.stringify({
    cases,
    name: metadata.name.trim(),
    tags: metadata.tags.map((t) => t.trim()).filter(Boolean),
    split: metadata.split || 'development',
    review_status: metadata.review_status || 'draft',
    frozen: metadata.frozen || false,
  })
}
