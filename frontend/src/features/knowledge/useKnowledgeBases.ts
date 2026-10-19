import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { api } from '../../api/client'

const storageKey = 'rag-active-knowledge-base'
export const activeKnowledgeBaseId = ref(localStorage.getItem(storageKey) || '')
watch(activeKnowledgeBaseId, (id) => localStorage.setItem(storageKey, id))

export function useKnowledgeBases() {
  const query = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: api.knowledgeBases,
    refetchInterval: 15000,
  })
  watch(
    query.data,
    (items) => {
      if (items && !items.some((item) => item.kb_id === activeKnowledgeBaseId.value))
        activeKnowledgeBaseId.value = items[0]?.kb_id || ''
    },
    { immediate: true },
  )
  const current = computed(() =>
    query.data.value?.find((item) => item.kb_id === activeKnowledgeBaseId.value),
  )
  return { ...query, current, activeKnowledgeBaseId }
}
