import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import path from 'node:path'
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
const results = process.env.V3_RESULTS
const mode = process.env.V3_MODE || 'keyword'
const rerank = process.env.V3_RERANK || 'off'
const prefix = process.env.V3_PREFIX || 'evaluation-v3'
const get = async (url) =>
  page.evaluate(async (u) => {
    const r = await fetch(u)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.json()
  }, url)
try {
  await page.goto(process.env.V3_URL + '/retrieval')
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await page.getByLabel('案例 1 问题').fill('员工每年有多少天带薪年假，谁审批？')
  await page.getByLabel('评测运行方式').selectOption('rag')
  await page.getByLabel('评测检索方式').selectOption(mode)
  await page.getByLabel('案例 1 预期').selectOption('answerable')
  await page.getByLabel('v3验收制度.md', { exact: true }).check()
  await page.locator('.eval-experiment-options summary').click()
  await page.getByLabel('实验重排', { exact: true }).selectOption(rerank)
  await page.getByLabel('实验上下文预算').selectOption('512')
  await page.locator('.eval-anchors summary').click()
  for (const [index, fact, quote] of [
    [1, '年假天数', '每年享有十天带薪年假'],
    [2, '审批要求', '申请需要直属主管审批'],
  ]) {
    await page.getByRole('button', { name: '添加必要事实' }).click()
    await page.getByLabel(`必要事实 ${index}`, { exact: true }).fill(fact)
    await page.getByLabel(`事实 ${index} 证据 1 原文`).fill(quote)
  }
  await page.getByRole('button', { name: '添加案例', exact: true }).click()
  await page.getByLabel('案例 2 问题').fill('员工生日礼金的精确金额是多少？')
  await page.getByLabel('案例 2 预期').selectOption('unanswerable')
  await page.locator('.eval-case-sets summary').click()
  await page.getByLabel('案例集名称').fill('真实链路合成题-待人工审核')
  await page.getByRole('button', { name: '保存案例集版本', exact: true }).click()
  await expect(page.getByText('已绑定保存版本。', { exact: false })).toBeVisible()
  async function start() {
    const response = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/v1/evaluations' && r.request().method() === 'POST',
    )
    await page.getByRole('button', { name: '开始评测', exact: true }).click()
    const submitted = await (await response).json()
    assert.ok(submitted.evaluation_id)
    await expect(page.getByRole('heading', { name: /已完成 · 2\/2/ })).toBeVisible({
      timeout: 120000,
    })
    const result = await get('/api/v1/evaluations/' + submitted.evaluation_id)
    assert.equal(result.request.mode, mode)
    assert.equal(result.request.options.rerank, rerank)
    assert.deepEqual(
      result.results.map((v) => v.status),
      ['completed', 'completed'],
    )
    assert.deepEqual(
      result.results.map((v) => v.run.answer_status),
      ['answered', 'insufficient_evidence'],
    )
    assert.equal(result.results[0].evidence.context_complete, true)
    assert.equal(result.summary.metrics.recall, 1)
    assert.equal(result.summary.metrics.precision, 1 / 5)
    assert.equal(result.summary.refusal_correct, 1)
    assert.ok(result.results[0].run.sources.length > 0)
    if (rerank === 'on') {
      assert.equal(
        result.results[0].diagnostics.rerank_applied,
        true,
        '重排必须实际成功，降级不算通过',
      )
      assert.ok(result.results[0].sources.some((v) => typeof v.rerank_score === 'number'))
    }
    return result
  }
  const first = await start()
  assert.equal(first.results[0].run.answer_status, 'answered')
  assert.equal(first.results[1].run.answer_status, 'insufficient_evidence')
  assert.equal(first.results[0].evidence.context_complete, true)
  assert.equal(first.request.options.context_tokens, 512)
  await page.getByLabel('评价备注').fill('自动验收流程验证，回答仍待人工审核。')
  const reviewSaved = page.waitForResponse(
    (r) => r.url().includes('/reviews/0') && r.request().method() === 'PATCH',
  )
  await page.getByRole('button', { name: '保存评价', exact: true }).click()
  await reviewSaved
  await page.reload()
  await page.getByRole('tab', { name: 'RAG 评测', exact: true }).click()
  await expect(page.getByLabel('评价备注')).toHaveValue('自动验收流程验证，回答仍待人工审核。')
  let downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出验收报告', exact: true }).click()
  await (await downloaded).saveAs(path.join(results, `${prefix}-acceptance.md`))
  await page.getByRole('button', { name: '载入此配置', exact: true }).click()
  await page.locator('.eval-experiment-options summary').click()
  await page.getByLabel('实验上下文预算').selectOption('1024')
  const second = await start()
  await page.getByLabel('基线实验 A').selectOption(first.evaluation_id)
  await page.getByLabel('对照实验 B').selectOption(second.evaluation_id)
  await page.getByRole('button', { name: '比较实验', exact: true }).click()
  await expect(page.getByRole('img', { name: '两次实验检索指标差值' })).toBeVisible()
  downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出比较报告', exact: true }).click()
  await (await downloaded).saveAs(path.join(results, `${prefix}-comparison.md`))
  await page.screenshot({ path: path.join(results, `${prefix}-live.png`), fullPage: true })
  for (const width of [1024, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    )
  }
  await page.setViewportSize({ width: 1440, height: 1050 })
  await page.getByRole('button', { name: '归档评测', exact: true }).click()
  await page.getByLabel('查看已归档评测').check()
  await page.getByLabel('评测记录', { exact: true }).selectOption(second.evaluation_id)
  await expect(page.getByRole('button', { name: '取消归档', exact: true })).toBeVisible()
  page.once('dialog', (dialog) => dialog.accept())
  const deleted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === '/api/v1/evaluations/' + second.evaluation_id &&
      r.request().method() === 'DELETE',
  )
  await page.getByRole('button', { name: '删除评测', exact: true }).click()
  assert.equal((await deleted).status(), 200)
  assert.deepEqual(errors, [])
  console.log(
    JSON.stringify({
      baseline: first.evaluation_id,
      deleted_experiment: second.evaluation_id,
      mode,
      rerank,
      scope: 'synthetic_two_cases_real_browser_backend_models',
      checks: [
        'real_browser_create_version',
        'real_rag_answer_refusal',
        'multi_fact_context',
        'isolated_budget_override',
        'pending_review_persistence',
        'paired_comparison',
        'report_downloads',
        'archive_delete',
        'responsive_layout',
        ...(rerank === 'on' ? ['real_rerank_applied_without_fallback'] : []),
      ],
      results: [first, second].map((r) => ({
        evaluation_id: r.evaluation_id,
        options: r.request.options,
        statuses: r.results.map((v) => v.run.answer_status),
        summary: r.summary,
        usage: r.quality.usage,
        cases: r.results.map((v) => ({
          query: v.case.query,
          answer: v.run.answer,
          answer_status: v.run.answer_status,
          elapsed_ms: v.elapsed_ms,
          source_count: v.sources.length,
          context_source_count: v.context_sources.length,
          rerank_applied: v.diagnostics.rerank_applied,
          rerank_model: v.diagnostics.rerank_model,
          rerank_count: v.diagnostics.rerank_count,
          rerank_scores: v.sources.map((s) => s.rerank_score),
          evidence: v.evidence,
        })),
      })),
    }),
  )
} finally {
  await browser.close()
}
