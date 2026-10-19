import { expect, test } from '@playwright/test'
const emptyModels = {
  llm: {
    provider: 'openai',
    model: '',
    base_url: '',
    key_configured: false,
    source: 'environment',
    ready: false,
    max_tokens: 1024,
    input_tokens: 8192,
  },
  embedding: {
    model: '',
    base_url: '',
    key_configured: false,
    source: 'environment',
    ready: false,
    dimension: 0,
    max_input_tokens: 0,
  },
  embedding_reindex_required: false,
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith('/knowledge-bases')
        ? []
        : { chat_ready: false, embedding_ready: false },
    }),
  )
})
test('save custom models without browser secret storage and preserve unsaved fields', async ({
  page,
}) => {
  let models = structuredClone(emptyModels)
  const requests: { kind: string; body: any }[] = []
  await page.route('**/api/v1/settings/models**', async (route) => {
    const kind = new URL(route.request().url()).pathname.split('/').pop() as 'llm' | 'embedding'
    if (route.request().method() === 'PUT') {
      const { api_key, ...values } = route.request().postDataJSON()
      requests.push({ kind, body: route.request().postDataJSON() })
      models = {
        ...models,
        [kind]: { ...models[kind], ...values, key_configured: true, source: 'custom', ready: true },
        embedding_reindex_required: kind === 'embedding',
      }
    }
    await route.fulfill({ json: models })
  })
  await page.goto('/settings')
  await page.getByLabel('对话服务地址').fill('https://chat.example/v1')
  await page.getByLabel('对话模型名称').fill('my-chat')
  await page.getByLabel('对话 API Key', { exact: true }).fill('fake-chat-key')
  await page.getByLabel('Embedding 模型名称').fill('unsaved-draft')
  await page.getByRole('button', { name: '保存对话模型', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('对话模型已保存')
  await expect(page.getByLabel('对话 API Key', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Embedding 模型名称')).toHaveValue('unsaved-draft')
  expect(requests[0].body.api_key).toBe('fake-chat-key')
  await page.getByRole('button', { name: '保存对话模型', exact: true }).click()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1].body).not.toHaveProperty('api_key')
  await page.getByLabel('Embedding 服务地址').fill('https://embeddings.example/v1')
  await page.getByLabel('Embedding 模型名称').fill('my-embedding')
  await page.getByLabel('Embedding API Key', { exact: true }).fill('fake-embedding-key')
  await page.getByLabel('向量维度', { exact: true }).fill('1024')
  await page.getByLabel('Embedding 输入上限', { exact: true }).fill('8192')
  await page.getByRole('button', { name: '保存 Embedding', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: '请到知识库重新构建索引' })).toBeVisible()
  await expect(page.getByLabel('Embedding API Key', { exact: true })).toHaveValue('')
  expect(requests[2].body.dimension).toBe(1024)
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))
  expect(stored).not.toContain('fake-chat-key')
  expect(stored).not.toContain('fake-embedding-key')
  await page.reload()
  await expect(page.getByLabel('对话模型名称')).toHaveValue('my-chat')
  await expect(page.getByLabel('Embedding 模型名称')).toHaveValue('my-embedding')
  await expect(page.getByLabel('对话 API Key', { exact: true })).toHaveValue('')
  await page.screenshot({
    path: 'test-results/model-settings-desktop.png',
    fullPage: true,
    animations: 'disabled',
  })
})

test('model settings handle busy conflicts, environment reset and mobile layout', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let current = {
    ...structuredClone(emptyModels),
    llm: {
      ...emptyModels.llm,
      model: 'custom-chat',
      base_url: 'https://models.example/v1',
      key_configured: true,
      source: 'custom',
      ready: true,
    },
  }
  await page.route('**/api/v1/settings/models**', async (route) => {
    if (route.request().method() === 'PUT')
      return route.fulfill({
        status: 409,
        json: {
          detail: {
            code: 'model_settings_busy',
            message: '存在未结束的问答，请先完成或取消后再修改模型。',
          },
        },
      })
    if (route.request().method() === 'DELETE') current = structuredClone(emptyModels)
    return route.fulfill({ json: current })
  })
  await page.goto('/settings')
  await page.getByRole('button', { name: '保存对话模型', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('未结束的问答')
  await page
    .getByRole('form', { name: '对话模型配置' })
    .getByRole('button', { name: '恢复环境配置' })
    .click()
  await expect(page.getByRole('status')).toContainText('已恢复服务端环境配置')
  await expect(page.getByLabel('对话模型名称')).toHaveValue('')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: 'test-results/model-settings-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
})
