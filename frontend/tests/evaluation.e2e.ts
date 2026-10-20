import { test, expect } from '@playwright/test'
const eid = '3a97417c-f1b6-4363-bd70-710ea1ea654b'
const source = {
  citation_id: 1,
  chunk_id: 'chunk-one',
  file_id: 'policy',
  name: '年假制度.md',
  content: '员工每年享有十天带薪年假。',
  heading: '年假',
  page: null,
  kind: 'text',
  score: 0.9,
}
function makeRecord(body: any) {
  const labelled =
    body.cases[0].expected === 'answerable' && body.cases[0].judgment_scope !== 'partial'
  const metric = (k: number) => ({
    precision: labelled ? 1 / k : null,
    recall: labelled ? 1 : null,
    hit: labelled ? 1 : null,
    mrr: labelled ? 1 : null,
    ndcg: labelled ? 1 : null,
  })
  return {
    evaluation_id: eid,
    status: 'completed',
    created_at: '2026-10-19T03:00:00Z',
    request: body,
    snapshot: {
      fingerprint: 'fixed-inputs',
      files: [{ file_id: 'policy', name: '年假制度.md', content_hash: 'hash' }],
      config: {},
      code_sha256: 'code',
    },
    results: [
      {
        case: body.cases[0],
        status: 'completed',
        sources: [source],
        context_sources: body.kind === 'rag' ? [source] : [],
        diagnostics: {
          trace: { before_rerank: [source], after_rerank: [source] },
          rerank_applied: true,
          stage_ms: { keyword: 4, rerank: 12, total: 18 },
        },
        assembly: {
          evidence_tokens: 40,
          history_tokens: 0,
          total_estimated_tokens: 180,
          input_budget: 3000,
        },
        run:
          body.kind === 'rag'
            ? {
                answer: '员工有十天年假。[1]',
                answer_status: 'answered',
                answer_protocol: 'rag-answer-v2',
                answer_segments: [{ text: '员工有十天年假。', source_ids: [1] }],
                sources: [source],
              }
            : undefined,
        token_usage: [{ stage: 'answer', actual: { input_tokens: 100, output_tokens: 20 } }],
        elapsed_ms: 800,
        metrics: labelled ? metric(body.k) : null,
        judgments: { judged: 1, returned: 1, coverage: 1, judged_precision: 1 },
        evidence: body.cases[0].evidence_groups?.length
          ? {
              valid: true,
              context_complete: body.kind === 'rag' ? true : null,
              causes: [],
              coverage: {
                parsed: 1,
                retrieved: 1,
                reranked: 1,
                selected: 1,
                ...(body.kind === 'rag' ? { context: 1 } : {}),
              },
              groups: body.cases[0].evidence_groups.map((g: any) => ({
                ...g,
                alternatives: g.alternatives.map((a: any) => ({ ...a, anchor_status: 'matched' })),
              })),
            }
          : undefined,
        review: { verdict: 'pending', notes: '' },
      },
    ],
    summary: {
      metrics: metric(body.k),
      curves: [1, 3, 5, 10, 20].map((k) => ({ k, ...metric(k) })),
      metric_queries: labelled ? 1 : 0,
      labelled_failures: 0,
      completed: 1,
      failed: 0,
      cancelled: 0,
      refusal_correct: 0,
      refusal_total: 0,
      false_refusals: 0,
      answerable_total: body.kind === 'rag' && labelled ? 1 : 0,
      reviewed: 0,
    },
  }
}
async function setup(page: any) {
  let record: any
  const submissions: any[] = []
  const sets: any[] = []
  await page.route('**/api/v1/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/knowledge-bases')
      return route.fulfill({
        json: [
          {
            kb_id: 'default',
            name: '默认知识库',
            file_count: 1,
            created_at: '2026-10-19T03:00:00Z',
          },
        ],
      })
    if (path.endsWith('/health'))
      return route.fulfill({ json: { status: 'ok', chat_ready: true, embedding_ready: false } })
    if (path.endsWith('/files'))
      return route.fulfill({
        json: {
          items: [{ file_id: 'policy', name: '年假制度.md', kb_id: 'default' }],
          total: 1,
          limit: 20,
          offset: 0,
        },
      })
    if (path === '/api/v1/evaluations/corpus')
      return route.fulfill({
        json: [{ file_id: 'policy', name: '年假制度.md', content_hash: 'a'.repeat(64) }],
      })
    if (path === '/api/v1/evaluations/case-sets') {
      if (route.request().method() === 'POST') {
        const saved = {
          ...route.request().postDataJSON(),
          case_set_id: 'b'.repeat(8) + '-1111-4111-8111-111111111111',
          version: sets.length + 1,
        }
        sets.unshift(saved)
        return route.fulfill({ status: 201, json: saved })
      }
      return route.fulfill({ json: sets })
    }
    if (path.startsWith('/api/v1/evaluations/case-sets/')) return route.fulfill({ json: sets[0] })
    if (path === '/api/v1/evaluations/history')
      return route.fulfill({
        json: {
          items: record
            ? [
                {
                  evaluation_id: eid,
                  status: record.status,
                  kb_id: 'default',
                  created_at: record.created_at,
                  kind: record.request.kind,
                },
              ]
            : [],
          total: record ? 1 : 0,
          offset: 0,
          limit: 20,
        },
      })
    if (path === '/api/v1/evaluations') {
      if (route.request().method() === 'POST') {
        submissions.push(route.request().postDataJSON())
        record = makeRecord(submissions.at(-1))
        return route.fulfill({ status: 202, json: record })
      }
      return route.fulfill({
        json: record
          ? [
              {
                evaluation_id: eid,
                status: record.status,
                kb_id: 'default',
                created_at: record.created_at,
                kind: record.request.kind,
              },
            ]
          : [],
      })
    }
    if (path.endsWith('/reviews/0')) {
      record.results[0].review = route.request().postDataJSON()
      record.summary.reviewed = 1
      return route.fulfill({ json: record })
    }
    if (path.endsWith('/stop')) {
      record.status = 'stopped'
      return route.fulfill({ json: record })
    }
    if (path === `/api/v1/evaluations/${eid}`) return route.fulfill({ json: record })
    return route.fulfill({ json: {} })
  })
  return { submissions, setRecord: (r: any) => (record = r) }
}

test('same-page RAG evaluation displays labelled metrics, trace, answer, review and durable history', async ({
  page,
}) => {
  const fixture = await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('员工年假多少天？')
  await page.getByLabel('评测运行方式').selectOption('rag')
  await page.getByLabel('案例 1 预期').selectOption('answerable')
  await page.getByLabel('年假制度.md', { exact: true }).check()
  await page.getByRole('button', { name: '开始评测', exact: true }).click()
  await expect(page.locator('.eval-metrics .card').nth(0)).toContainText('20.0%')
  await expect(page.locator('.eval-metrics .card').nth(1)).toContainText('100.0%')
  await expect(page.getByRole('img', { name: '精准率、召回率和命中率随 K 的变化' })).toBeVisible()
  await expect(page.getByText('员工有十天年假。', { exact: false })).toBeVisible()
  await expect(page.getByRole('heading', { name: '证据流转（切片级）' })).toBeVisible()
  expect(fixture.submissions).toHaveLength(1)
  expect(fixture.submissions[0].cases[0].relevant_file_ids).toEqual(['policy'])
  await page.getByLabel('人工评价', { exact: true }).selectOption('pass')
  await page.getByLabel('评价备注').fill('适用条件完整')
  await page.getByRole('button', { name: '保存评价' }).click()
  await expect(page.getByText('人工已评 1', { exact: true })).toBeVisible()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 JSON', exact: true }).click()
  expect((await download).suggestedFilename()).toBe(`evaluation-${eid}.json`)
  await page.reload()
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.getByLabel('评价备注')).toHaveValue('适用条件完整')
  await page.getByRole('button', { name: '载入此配置' }).click()
  await expect(page.getByLabel('评测运行方式')).toHaveValue('rag')
  await expect(page.getByLabel('案例 1 问题')).toHaveValue('员工年假多少天？')
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 960 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (width === 1440)
      await page.screenshot({ path: 'test-results/evaluation-v1.png', fullPage: true })
  }
})

test('unlabelled experiments show missing quality metrics instead of perfect scores', async ({
  page,
}) => {
  await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('观察检索结果')
  await page.getByRole('button', { name: '开始评测', exact: true }).click()
  await expect(page.locator('.eval-metrics')).toContainText('未标注')
  await expect(page.getByText('没有已执行的有答案标注题，暂不计算检索质量。')).toBeVisible()
  await expect(page.locator('.eval-metrics')).not.toContainText('100.0%')
})

test('batch editor enforces 20 cases, imports own labels and retains retrieval page', async ({
  page,
}) => {
  await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('导入评测案例').setInputFiles({
    name: 'cases.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify(
        Array.from({ length: 20 }, (_, i) => ({
          query: `问题${i + 1}`,
          expected: 'unlabelled',
          relevant_file_ids: [],
        })),
      ),
    ),
  })
  await expect(page.getByRole('button', { name: '添加案例' })).toBeDisabled()
  await expect(page.getByLabel('案例 20 问题', { exact: true })).toHaveValue('问题20')
  await page.getByRole('tab', { name: '检索调试', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始检索', exact: true })).toBeVisible()
})

test('V2 saves a named case-set version with text anchors and structured answer review', async ({
  page,
}) => {
  const fixture = await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('员工年假多少天？')
  await page.getByLabel('评测运行方式').selectOption('rag')
  await page.getByLabel('案例 1 预期').selectOption('answerable')
  await page.getByLabel('年假制度.md', { exact: true }).check()
  await page.locator('.eval-anchors summary').click()
  await page.getByRole('button', { name: '添加必要事实' }).click()
  await page.getByLabel('必要事实 1', { exact: true }).fill('年假天数')
  await page.getByLabel('事实 1 证据 1 原文').fill('十天带薪年假')
  await page.locator('.eval-case-sets summary').click()
  await page.getByLabel('案例集名称').fill('人事基础题')
  await page.getByLabel('案例集标签').fill('年假,人事')
  await page.getByRole('button', { name: '保存案例集版本' }).click()
  await expect(page.getByText('已绑定保存版本。', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '开始评测', exact: true }).click()
  await expect(page.getByRole('heading', { name: '必要原文覆盖' })).toBeVisible()
  expect(fixture.submissions[0].case_set_id).toBeTruthy()
  expect(fixture.submissions[0].cases[0].evidence_groups[0].alternatives[0].content_hash).toBe(
    'a'.repeat(64),
  )
  await page.getByLabel('必要事实覆盖', { exact: true }).selectOption('full')
  await page.getByLabel('存在无依据事实').selectOption('no')
  await page.getByLabel('适用条件与例外').selectOption('pass')
  await page.getByLabel('引用语义支持').selectOption('full')
  await page.getByLabel('拒答判断').selectOption('na')
  await page.getByLabel('人工评价', { exact: true }).selectOption('pass')
  await page.getByRole('button', { name: '保存评价' }).click()
  await page.reload()
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.getByLabel('引用语义支持')).toHaveValue('full')
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 960 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (width === 1440)
      await page.screenshot({ path: 'test-results/evaluation-v2.png', fullPage: true })
  }
})

test('V2 paired comparison visualizes deltas and reports incompatible experiments', async ({
  page,
}) => {
  await setup(page)
  const other = '4a97417c-f1b6-4363-bd70-710ea1ea654b'
  await page.route(/\/api\/v1\/evaluations(?:\/history)?(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        total: 2,
        limit: 20,
        offset: 0,
        items: [eid, other].map((evaluation_id) => ({
          evaluation_id,
          kb_id: 'default',
          kind: 'retrieval',
          status: 'completed',
          created_at: '2026-10-19T03:00:00Z',
        })),
      },
    }),
  )
  let compatible = true
  await page.route('**/api/v1/evaluations/compare?**', (route) =>
    compatible
      ? route.fulfill({
          json: {
            modes: ['keyword', 'hybrid'],
            counts: { win: 1, tie: 0, loss: 0, unscored: 0 },
            metric_delta: { precision: 0.2, recall: 0.5, hit: 0, mrr: 0.5, ndcg: 0.5 },
            rows: [
              {
                query: '员工年假多少天',
                outcome: 'win',
                delta: { recall: 0.5, ndcg: 0.5 },
                elapsed_ms_delta: 20,
                tokens_delta: null,
              },
            ],
            config_changes: {},
            code_changed: false,
          },
        })
      : route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'incompatible_evaluations',
              message: '无法直接比较：语料或标注版本不同。',
              retryable: false,
            },
          },
        }),
  )
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('基线实验 A').selectOption(eid)
  await page.getByLabel('对照实验 B').selectOption(other)
  await page.getByRole('button', { name: '比较实验', exact: true }).click()
  await expect(page.getByRole('img', { name: '两次实验检索指标差值' })).toBeVisible()
  await expect(page.locator('.eval-comparison')).toContainText('+50.0 pp')
  compatible = false
  await page.getByRole('button', { name: '比较实验', exact: true }).click()
  await expect(page.locator('.eval-comparison')).toContainText('无法直接比较')
  await expect(page.getByRole('img', { name: '两次实验检索指标差值' })).toHaveCount(0)
})

test('invalid evidence imports preserve the current draft and do not break the editor', async ({
  page,
}) => {
  const fixture = await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('保留此草稿')
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.getByLabel('导入评测案例').setInputFiles({
    name: 'bad-anchors.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify([
        {
          query: '不能替换',
          expected: 'answerable',
          relevant_file_ids: ['policy'],
          evidence_groups: [null],
        },
      ]),
    ),
  })
  await expect(page.getByRole('alert')).toContainText('案例字段格式无效')
  await expect(page.getByLabel('案例 1 问题')).toHaveValue('保留此草稿')
  await page.getByRole('button', { name: '开始评测', exact: true }).click()
  await expect(page.locator('.eval-metrics')).toBeVisible()
  expect(fixture.submissions[0].cases[0].query).toBe('保留此草稿')
  expect(errors).toEqual([])
})

test('unknown model usage is not displayed as a complete actual token total', async ({ page }) => {
  const fixture = await setup(page)
  const saved = makeRecord({
    knowledge_base_id: 'default',
    kind: 'rag',
    mode: 'keyword',
    k: 5,
    cases: [{ query: '年假', expected: 'answerable', relevant_file_ids: ['policy'] }],
  })
  saved.results[0]!.token_usage.push({ stage: 'answer_repair' } as any)
  fixture.setRecord(saved)
  await page.addInitScript((id) => localStorage.setItem('rag-evaluation:default', id), eid)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(
    page
      .getByRole('table')
      .filter({ has: page.getByRole('columnheader', { name: 'Token（实际）', exact: true }) })
      .locator('tbody tr td')
      .nth(4),
  ).toHaveText('—')
})

test('late comparison errors are ignored after selecting another experiment', async ({ page }) => {
  await setup(page)
  const other = '4a97417c-f1b6-4363-bd70-710ea1ea654b'
  await page.route(/\/api\/v1\/evaluations(?:\/history)?(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        total: 2,
        limit: 20,
        offset: 0,
        items: [eid, other].map((evaluation_id) => ({
          evaluation_id,
          kb_id: 'default',
          kind: 'retrieval',
          status: 'completed',
          created_at: '2026-10-19T03:00:00Z',
        })),
      },
    }),
  )
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let started!: () => void
  const ready = new Promise<void>((resolve) => {
    started = resolve
  })
  await page.route('**/api/v1/evaluations/compare?**', async (route) => {
    started()
    await gate
    await route.fulfill({
      status: 409,
      json: {
        detail: { code: 'incompatible_evaluations', message: '旧实验的错误', retryable: false },
      },
    })
  })
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('基线实验 A').selectOption(eid)
  await page.getByLabel('对照实验 B').selectOption(other)
  await page.getByRole('button', { name: '比较实验', exact: true }).click()
  await ready
  await page.getByLabel('对照实验 B').selectOption(eid)
  release()
  await expect(page.getByRole('button', { name: '比较实验', exact: true })).toBeVisible()
  await expect(page.locator('.eval-comparison [role="alert"]')).toHaveCount(0)
})

test('V3 retry requires explicit unknown-call confirmation and keeps an idempotency key', async ({
  page,
}) => {
  const fixture = await setup(page)
  const parent = makeRecord({
    knowledge_base_id: 'default',
    kind: 'rag',
    mode: 'keyword',
    k: 5,
    file_ids: null,
    cases: [{ query: '未知调用', expected: 'unlabelled', relevant_file_ids: [] }],
  })
  parent.results[0]!.status = 'failed'
  fixture.setRecord(parent)
  await page.addInitScript((id) => localStorage.setItem('rag-evaluation:default', id), eid)
  const childId = '5a97417c-f1b6-4363-bd70-710ea1ea654b'
  const child = { ...parent, evaluation_id: childId, request: { ...parent.request, retry_of: eid } }
  const retries: any[] = []
  await page.route('**/api/v1/evaluations/*/retry-plan', (route) =>
    route.fulfill({ json: { retry_indices: [0], unknown_indices: [0], reuse_count: 0 } }),
  )
  await page.route('**/api/v1/evaluations/*/retry', (route) => {
    retries.push(route.request().postDataJSON())
    return route.fulfill({ status: 202, json: child })
  })
  await page.route(new RegExp(`/api/v1/evaluations/${childId}(?:\\?.*)?$`), (route) =>
    route.fulfill({ json: child }),
  )
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByRole('button', { name: '检查失败题重试', exact: true }).click()
  await expect(page.getByRole('button', { name: '创建重试评测', exact: true })).toBeDisabled()
  expect(retries).toHaveLength(0)
  await page.getByLabel(/调用结果未知，我确认重试可能再次计费/).check()
  await page.getByRole('button', { name: '创建重试评测', exact: true }).click()
  await expect(page.getByText(`本记录关联重试自 ${eid}`, { exact: false })).toBeVisible()
  expect(retries[0].confirm_unknown_calls).toBe(true)
  expect(retries[0].idempotency_key).toMatch(/^[a-f0-9-]{36}$/)
})

test('V3 paginated history scopes archive state without dropping the last page', async ({
  page,
}) => {
  await setup(page)
  const urls: URL[] = []
  await page.route('**/api/v1/evaluations/history?**', (route) => {
    const url = new URL(route.request().url())
    urls.push(url)
    const offset = Number(url.searchParams.get('offset'))
    return route.fulfill({
      json: {
        total: url.searchParams.get('archived') === 'true' ? 0 : 21,
        limit: 20,
        offset,
        items:
          url.searchParams.get('archived') === 'true'
            ? []
            : Array.from({ length: offset ? 1 : 20 }, (_, i) => ({
                evaluation_id: `${String(i + offset).padStart(8, '0')}-1111-4111-8111-111111111111`,
                kb_id: 'default',
                kind: 'retrieval',
                status: 'completed',
                created_at: '2026-10-19T03:00:00Z',
              })),
      },
    })
  })
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.getByText('共 21 条 · 第 1 页', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '下一页评测' }).click()
  await expect(page.getByText('共 21 条 · 第 2 页', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '下一页评测' })).toBeDisabled()
  await page.getByLabel('查看已归档评测').check()
  await expect(page.getByText('共 0 条 · 第 1 页', { exact: false })).toBeVisible()
  expect(urls.at(-1)!.searchParams.get('offset')).toBe('0')
  expect(urls.at(-1)!.searchParams.get('knowledge_base_id')).toBe('default')
})

test('editing case-set metadata unbinds its saved version until explicitly saved again', async ({
  page,
}) => {
  const fixture = await setup(page)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('保持问题不变')
  await page.locator('.eval-case-sets summary').click()
  await page.getByLabel('案例集名称').fill('metadata baseline')
  await page.getByRole('button', { name: '保存案例集版本', exact: true }).click()
  await expect(page.getByText('已绑定保存版本。', { exact: false })).toBeVisible()
  await page.getByLabel('案例集用途').selectOption('acceptance')
  await expect(page.getByText('当前草稿未绑定版本。', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '开始评测', exact: true }).click()
  await expect(page.locator('.eval-metrics')).toBeVisible()
  expect(fixture.submissions[0].case_set_id).toBeNull()
})

test('background refresh does not discard an unsaved manual review', async ({ page }) => {
  const fixture = await setup(page)
  const saved = makeRecord({
    knowledge_base_id: 'default',
    kind: 'rag',
    mode: 'keyword',
    k: 5,
    cases: [{ query: '年假', expected: 'answerable', relevant_file_ids: ['policy'] }],
  })
  fixture.setRecord(saved)
  let reads = 0
  await page.route(new RegExp(`/api/v1/evaluations/${eid}(?:\\?.*)?$`), (route) => {
    reads++
    return route.fulfill({ json: saved })
  })
  await page.addInitScript((id) => localStorage.setItem('rag-evaluation:default', id), eid)
  await page.clock.install()
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('评价备注').fill('尚未保存的审核')
  const before = reads
  saved.results[0]!.review.notes = '服务端的新备注'
  await page.clock.fastForward(3100)
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => reads).toBeGreaterThan(before)
  await expect(page.getByLabel('评价备注')).toHaveValue('尚未保存的审核')
})

test('filtering to an unattempted tag hides details from the previous case', async ({ page }) => {
  const fixture = await setup(page)
  const saved: any = makeRecord({
    knowledge_base_id: 'default',
    kind: 'retrieval',
    mode: 'keyword',
    k: 5,
    cases: [
      { query: '已运行案例', expected: 'unlabelled', relevant_file_ids: [], tags: ['done'] },
      { query: '尚未运行案例', expected: 'unlabelled', relevant_file_ids: [], tags: ['pending'] },
    ],
  })
  fixture.setRecord(saved)
  await page.addInitScript((id) => localStorage.setItem('rag-evaluation:default', id), eid)
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.locator('.eval-detail')).toBeVisible()
  await page.getByLabel('评测业务标签筛选').selectOption('pending')
  await expect(page.locator('.eval-detail')).toHaveCount(0)
})

test('history clamps an empty final page after records are removed', async ({ page }) => {
  await setup(page)
  let total = 21
  await page.route('**/api/v1/evaluations/history?**', (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'))
    if (offset) total = 20
    return route.fulfill({ json: { total, limit: 20, offset, items: [] } })
  })
  await page.goto('/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.getByText('共 21 条 · 第 1 页', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '下一页评测' }).click()
  await expect(page.getByText('共 20 条 · 第 1 页', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '下一页评测' })).toBeDisabled()
})
