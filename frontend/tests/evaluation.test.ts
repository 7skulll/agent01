import { describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import { actualTokens, normalizeCases, parseCases } from '../src/features/evaluation/cases'

const labelled = () => ({
  query: '年假多少天',
  expected: 'answerable',
  relevant_file_ids: ['policy'],
})
describe('evaluation import boundary', () => {
  it('normalizes legacy cases and clones reactive drafts without mutating saved records', () => {
    const parsed = parseCases([labelled()])
    const copied = normalizeCases(reactive(parsed))
    copied[0]!.relevant_file_ids.push('other')
    expect(parsed[0]!.relevant_file_ids).toEqual(['policy'])
    expect(copied[0]!.judgment_scope).toBe('full')
  })
  it.each([
    null,
    { cases: [] },
    [null],
    [{ ...labelled(), judgment_scope: 'unknown' }],
    [{ ...labelled(), evidence_groups: [null] }],
    [{ ...labelled(), negative_file_ids: 'policy' }],
    [{ ...labelled(), evidence_groups: [{ fact: '天数', alternatives: [] }] }],
    [
      {
        ...labelled(),
        evidence_groups: [
          {
            fact: '天数',
            alternatives: [{ file_id: 'policy', quote: '十天带薪年假', content_hash: null }],
          },
        ],
      },
    ],
    [{ ...labelled(), negative_file_ids: ['policy'] }],
  ])('rejects malformed input without letting it reach the nested editor: %j', (data) => {
    expect(() => parseCases(data)).toThrow()
  })
  it('preserves valid multi-fact evidence and partial labels', () => {
    const c = {
      ...labelled(),
      judgment_scope: 'partial',
      negative_file_ids: ['other'],
      evidence_groups: [
        {
          fact: '年假天数',
          alternatives: [
            { file_id: 'policy', content_hash: 'a'.repeat(64), quote: '十天带薪年假', page: 2 },
          ],
        },
      ],
    }
    expect(parseCases({ cases: [c] })[0]).toMatchObject(c)
  })
})
describe('actual token totals', () => {
  it('requires all model call usages and both token fields', () => {
    expect(actualTokens({ token_usage: [] })).toBeNull()
    expect(
      actualTokens({ token_usage: [{ stage: 'answer', actual: { input_tokens: 20 } }] }),
    ).toBeNull()
    expect(
      actualTokens({
        token_usage: [
          { stage: 'answer', actual: { input_tokens: 20, output_tokens: 2 } },
          { stage: 'repair' },
        ],
      }),
    ).toBeNull()
    expect(
      actualTokens({
        token_usage: [{ stage: 'answer', actual: { input_tokens: 0, output_tokens: 0 } }],
      }),
    ).toBe(0)
    expect(
      actualTokens({
        token_usage: [
          { stage: 'answer', actual: { input_tokens: 20, output_tokens: 2 } },
          { stage: 'repair', actual: { input_tokens: 10, output_tokens: 3 } },
        ],
      }),
    ).toBe(35)
  })
})

it('V3 displays a known zero for a completed zero-call run', () => {
  expect(actualTokens({ run: { model_calls: 0 }, token_usage: [] })).toBe(0)
  expect(actualTokens({ run: { model_calls: 1 }, token_usage: [] })).toBeNull()
})

it('V3 rejects fractional token counts', () => {
  expect(
    actualTokens({
      token_usage: [{ stage: 'answer', actual: { input_tokens: 1.5, output_tokens: 2 } }],
    }),
  ).toBeNull()
})
