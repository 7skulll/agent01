import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import EvaluationWorkbench from '../src/features/evaluation/EvaluationWorkbench.vue'
import { auth } from '../src/features/auth/store'
import type { CaseSet } from '../src/features/evaluation/types'

vi.hoisted(() => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
})
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', async (original) => ({ ...(await original<object>()), request }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({ data: ref(undefined), error: ref(null), isPending: ref(false) }),
  useQueryClient: () => ({ invalidateQueries: vi.fn(), setQueryData: vi.fn() }),
}))

const saved: CaseSet = {
  case_set_id: 'old-set',
  name: '旧题集',
  version: 1,
  tags: [],
  kb_id: 'default',
  cases: [
    {
      query: '旧问题',
      expected: 'unlabelled',
      relevant_file_ids: [],
      negative_file_ids: [],
      judgment_scope: 'full',
      required_facts: '',
      reference_answer: '',
      tags: [],
      evidence_groups: [],
    },
  ],
}

async function workbench() {
  let state: any
  const app = createSSRApp(EvaluationWorkbench, { kbId: 'default' })
  app.mixin({
    created() {
      if (!(this as any).$.parent) state = (this as any).$.setupState
    },
  })
  const html = await renderToString(app)
  return { state, html }
}

beforeEach(() => {
  request.mockReset()
  auth.permissions = {
    ingest_files: true,
    manage_knowledge_bases: true,
    manage_models: false,
    manage_users: false,
  }
})

describe('测评操作边界', () => {
  it('加载新题集失败时保留旧草稿，但绝不提交旧题集', async () => {
    const { state } = await workbench()
    state.loadSavedSet(saved)
    request.mockRejectedValue(new Error('题集读取失败'))
    await state.testCaseSet({ ...saved, case_set_id: 'new-set', cases: undefined })
    expect(state.cases[0].query).toBe('旧问题')
    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.calls[0][0]).toBe('/evaluations/case-sets/new-set')
    expect(state.error.message).toBe('题集读取失败')
  })
  it('普通用户看到只读提示，没有提交入口，直接调用也不会发起写请求', async () => {
    auth.permissions!.ingest_files = false
    const { state, html } = await workbench()
    expect(html).toContain('当前账号仅可查看评测记录与测评集')
    expect(html).not.toContain('开始评测')
    expect(html).not.toContain('导入测评集 JSON')
    expect(html).toMatch(/<fieldset[^>]*disabled/)
    await state.start()
    await state.saveSet()
    await state.testCaseSet(saved)
    expect(request).not.toHaveBeenCalled()
  })
  it('新题集加载成功后只提交新题目', async () => {
    const { state } = await workbench()
    state.loadSavedSet(saved)
    const fresh = {
      ...saved,
      case_set_id: 'new-set',
      name: '新题集',
      cases: [{ ...saved.cases[0], query: '新问题' }],
    }
    request.mockResolvedValueOnce(fresh).mockResolvedValueOnce({ evaluation_id: 'new-evaluation' })
    await state.testCaseSet({ ...fresh, cases: undefined })
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls[1][0]).toBe('/evaluations')
    const body = JSON.parse(request.mock.calls[1][1].body)
    expect(body.case_set_id).toBe('new-set')
    expect(body.cases[0].query).toBe('新问题')
  })
})
