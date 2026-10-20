import { computed, ref, watch, type Ref } from 'vue'
import type { AnswerReview, EvalResult } from './types'

function normalize(review?: AnswerReview): AnswerReview {
  return {
    verdict: review?.verdict || 'pending',
    notes: review?.notes || '',
    fact_coverage: review?.fact_coverage || 'pending',
    unsupported_claims: review?.unsupported_claims || 'pending',
    conditions: review?.conditions || 'pending',
    citation_support: review?.citation_support || 'pending',
    refusal: review?.refusal || 'pending',
    causes: [...(review?.causes || [])],
  }
}

// A background refetch must not replace edits; explicit selection changes load that case's review.
export function useReviewDraft(key: Ref<string>, selected: Ref<EvalResult | undefined>) {
  const draft = ref<AnswerReview>(normalize())
  let loadedKey = '',
    baseline = JSON.stringify(draft.value)
  const dirty = computed(() => JSON.stringify(draft.value) !== baseline)
  watch(
    [key, selected],
    ([nextKey, result]) => {
      if (!result) return
      if (nextKey !== loadedKey || !dirty.value) {
        loadedKey = nextKey
        draft.value = normalize(result.review)
        baseline = JSON.stringify(draft.value)
      }
    },
    { immediate: true },
  )
  function saved(target: string, submitted: AnswerReview, review: AnswerReview) {
    if (target !== key.value || JSON.stringify(submitted) !== JSON.stringify(draft.value)) return
    loadedKey = target
    draft.value = normalize(review)
    baseline = JSON.stringify(draft.value)
  }
  return { draft, dirty, saved }
}
