import { effectScope, nextTick, ref } from 'vue'
import { expect, it } from 'vitest'
import type { EvalResult } from '../src/features/evaluation/types'
import { useReviewDraft } from '../src/features/evaluation/useReviewDraft'
import { blankCase, caseSetSignature } from '../src/features/evaluation/cases'

it('case-set metadata changes invalidate a binding even when questions stay unchanged', () => {
  const cases = [blankCase()]
  const metadata = {
    name: 'baseline',
    tags: ['hr'],
    split: 'development',
    review_status: 'verified',
    frozen: true,
  }
  const signature = caseSetSignature(cases, metadata)
  for (const change of [
    { name: 'changed' },
    { tags: ['other'] },
    { split: 'acceptance' },
    { review_status: 'draft' },
    { frozen: false },
  ])
    expect(caseSetSignature(cases, { ...metadata, ...change })).not.toBe(signature)
  expect(caseSetSignature(cases, { ...metadata, name: ' baseline ' })).toBe(signature)
})

it('background refetch keeps unsaved review edits and save acknowledges only its original selection', async () => {
  const scope = effectScope()
  const key = ref('a:0')
  const result = ref<EvalResult>({
    case: blankCase(),
    status: 'completed',
    sources: [],
    review: { verdict: 'pending', notes: '' },
  })
  const review = scope.run(() => useReviewDraft(key, result))!
  review.draft.value.notes = 'unsaved'
  result.value = { ...result.value, review: { verdict: 'pending', notes: '' } }
  await nextTick()
  expect(review.draft.value.notes).toBe('unsaved')
  const submitted = JSON.parse(JSON.stringify(review.draft.value))
  review.saved('a:0', submitted, { verdict: 'pending', notes: 'saved' })
  expect(review.dirty.value).toBe(false)
  expect(review.draft.value.notes).toBe('saved')
  key.value = 'b:0'
  result.value = { ...result.value, review: { verdict: 'fail', notes: 'other' } }
  await nextTick()
  review.saved('a:0', submitted, { verdict: 'pass', notes: 'wrong' })
  expect(review.draft.value.notes).toBe('other')
  scope.stop()
})
