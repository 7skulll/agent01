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
  const labelled = body.cases[0].expected === 'answerable'
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
