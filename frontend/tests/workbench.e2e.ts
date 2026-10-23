import { expect, test } from '@playwright/test'
const cid = '11111111-1111-4111-8111-111111111111',
  rid = '22222222-2222-4222-8222-222222222222'
const defaultBases = [
  { kb_id: 'default', name: '默认知识库', file_count: 0, created_at: '2026-10-16T01:00:00Z' },
]
const source = {
  citation_id: 1,
  chunk_id: 'chunk',
  file_id: 'file',
  name: '年假制度.md',
  content: '员工每年享有十天带薪年假。',
  heading: '员工年假',
  page: null,
  channels: ['keyword'],
  keyword_score: 2.5,
}
const conversation = {
  thread_id: cid,
  created_at: '2026-10-16T01:00:00Z',
  updated_at: '2026-10-16T01:00:00Z',
  status: 'ready',
  active_run_id: null,
}
const run = {
  run_id: rid,
  conversation_id: cid,
  request_id: rid,
  status: 'completed',
  message: '员工有多少天年假？',
  answer: '员工有十天年假。[1]',
  sources: [source],
  retrieval_mode: 'keyword',
  knowledge_base_id: 'default',
  created_at: conversation.created_at,
  updated_at: conversation.updated_at,
  committed: true,
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname
    if (path.endsWith('/auth/status')) {
      await route.fulfill({
        json: {
          has_users: true,
          setup_required: false,
          registration_enabled: false,
          password_min_length: 8,
        },
      })
      return
    }
    if (path.endsWith('/auth/me')) {
      await route.fulfill({
        json: {
          user: {
            user_id: 'local',
            username: 'local',
            display_name: '本地管理员',
            role: 'admin',
            is_active: true,
            created_at: '',
            updated_at: '',
            anonymous: true,
            service: false,
          },
          permissions: {
            ingest_files: true,
            manage_knowledge_bases: true,
            manage_models: true,
            manage_users: true,
          },
        },
      })
      return
    }
    if (path.endsWith('/knowledge-bases')) {
      await route.fulfill({ json: defaultBases })
      return
    }
    const data = path.endsWith('/health')
      ? { status: 'ok', chat_ready: true, embedding_ready: false }
      : path.endsWith('/conversations')
        ? [conversation]
        : path.endsWith('/turns')
          ? { items: [run], total: 1, limit: 50, offset: 0 }
          : path.endsWith('/files')
            ? { items: [], total: 0, limit: 20, offset: 0 }
            : path.endsWith('/search')
              ? {
                  query: '年假',
                  queries: ['年假'],
                  sources: [source],
                  diagnostics: { degraded: [], stage_ms: { total: 12 }, rerank_applied: false },
                }
              : path.endsWith('/index')
                ? { count: 0 }
                : { conversation }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
  })
})
test('create, switch, persist and delete knowledge bases including the last one', async ({
  page,
}) => {
  let bases = [...defaultBases]
  await page.route('**/api/v1/knowledge-bases*', async (route) => {
    if (new URL(route.request().url()).pathname !== '/api/v1/knowledge-bases')
      return route.fallback()
    if (route.request().method() === 'POST') {
      const item = {
        ...defaultBases[0]!,
        kb_id: 'project',
        name: route.request().postDataJSON().name,
      }
      bases.push(item)
      return route.fulfill({ status: 201, json: item })
    }
    return route.fulfill({ json: bases })
  })
  await page.route('**/api/v1/knowledge-bases/*', async (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback()
    const id = new URL(route.request().url()).pathname.split('/').pop()!
    bases = bases.filter((item) => item.kb_id !== id)
    return route.fulfill({ json: { kb_id: id, deleted: true, cleanup_pending: 0 } })
  })
  await page.goto('/knowledge')
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('default')
  await page.getByRole('button', { name: '新建知识库', exact: true }).click()
  await page.getByLabel('知识库名称').fill('项目资料')
  await page.getByRole('button', { name: '创建知识库', exact: true }).click()
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('project')
  await expect(page.getByText('正在管理「项目资料」', { exact: false })).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('project')
  await page.getByRole('button', { name: '删除当前知识库' }).click()
  await expect(page.getByRole('dialog')).toContainText('项目资料')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  expect(bases).toHaveLength(2)
  await page.getByRole('button', { name: '删除当前知识库' }).click()
  await page.getByRole('button', { name: '确认删除知识库' }).click()
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('default')
  await page.getByRole('button', { name: '删除当前知识库' }).click()
  await page.getByRole('button', { name: '确认删除知识库' }).click()
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: '上传文件', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '新建知识库', exact: true })).toBeEnabled()
})

test('uploads keep their original library when switching during an upload', async ({ page }) => {
  await page.route('**/api/v1/knowledge-bases', (route) =>
    route.fulfill({
      json: [...defaultBases, { ...defaultBases[0]!, kb_id: 'project', name: '项目资料' }],
    }),
  )
  const targets: string[] = []
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/knowledge-bases/*/files', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    targets.push(new URL(route.request().url()).pathname)
    if (targets.length === 1) await gate
    return route.fulfill({ status: 201, json: {} })
  })
  await page.goto('/knowledge')
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('default')
  await page.getByLabel('选择上传文件').setInputFiles([
    { name: '一.txt', mimeType: 'text/plain', buffer: Buffer.from('一') },
    { name: '二.txt', mimeType: 'text/plain', buffer: Buffer.from('二') },
  ])
  await expect.poll(() => targets.length).toBe(1)
  await page.getByLabel('当前知识库', { exact: true }).selectOption('project')
  release()
  await expect.poll(() => targets.length).toBe(2)
  expect(targets).toEqual([
    '/api/v1/knowledge-bases/default/files',
    '/api/v1/knowledge-bases/default/files',
  ])
  await expect(page.getByText('一.txt', { exact: true })).toHaveCount(0)
})

test('search and chat use the selected library and switching clears old results', async ({
  page,
}) => {
  await page.route('**/api/v1/knowledge-bases', (route) =>
    route.fulfill({
      json: [...defaultBases, { ...defaultBases[0]!, kb_id: 'project', name: '项目资料' }],
    }),
  )
  await page.goto('/retrieval')
  await page.getByLabel('当前知识库', { exact: true }).selectOption('project')
  await page.getByLabel('查询问题', { exact: true }).fill('年假')
  const search = page.waitForRequest((request) =>
    request.url().endsWith('/knowledge-bases/project/search'),
  )
  await page.getByRole('button', { name: '开始检索' }).click()
  await search
  await expect(page.getByText('员工每年享有十天带薪年假。', { exact: true })).toBeVisible()
  await page.getByLabel('当前知识库', { exact: true }).selectOption('default')
  await expect(page.getByText('员工每年享有十天带薪年假。', { exact: true })).toHaveCount(0)
  await page.getByLabel('当前知识库', { exact: true }).selectOption('project')
  await page.goto(`/chat/${cid}`)
  await page.route('**/api/v1/conversations/*/runs', (route) =>
    route.fulfill({ status: 202, json: { ...run, knowledge_base_id: 'project' } }),
  )
  await page.getByLabel('输入问题', { exact: true }).fill('项目问题')
  const submission = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith('/runs'),
  )
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  expect((await submission).postDataJSON().knowledge_base_id).toBe('project')
})
test('history sources survive refresh and code/HTML stay safe', async ({ page }) => {
  await page.goto(`/chat/${cid}`)
  await expect(page.getByText('员工有十天年假。')).toBeVisible()
  await page.getByRole('button', { name: '查看来源 1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('员工每年享有十天带薪年假。')
  await page.getByRole('button', { name: '关闭来源' }).click()
  await page.reload()
  await page.getByRole('button', { name: '查看来源 1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('年假制度.md')
})
test('structured citations keep body years and array subscripts literal across refresh', async ({
  page,
}) => {
  const text = '报告[2026]、array[1]和普通编号[1]都是正文。'
  const structured = {
    ...run,
    answer: text + '[1]',
    answer_status: 'answered',
    answer_reason: null,
    answer_protocol: 'rag-answer-v2',
    answer_segments: [{ text, source_ids: [1] }],
  }
  await page.route('**/turns?*', (route) =>
    route.fulfill({ json: { items: [structured], total: 1, limit: 50, offset: 0 } }),
  )
  await page.goto(`/chat/${cid}`)
  await expect(page.getByRole('button', { name: '查看来源 1', exact: true })).toHaveCount(1)
  await expect(page.locator('.markdown')).toContainText(text)
  await page.getByRole('button', { name: '查看来源 1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText(source.content)
  await page.getByRole('button', { name: '关闭来源' }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: '查看来源 1', exact: true })).toHaveCount(1)
})

test('insufficient evidence is a completed business result and survives refresh', async ({
  page,
}) => {
  const refused = {
    ...run,
    answer: '现有资料不足以确认该问题的答案，请补充包含所需信息的资料。',
    answer_status: 'insufficient_evidence',
    answer_reason: 'missing_fact',
    answer_protocol: 'rag-answer-v2',
    answer_segments: [],
    sources: [],
  }
  await page.route('**/turns?*', (route) =>
    route.fulfill({ json: { items: [refused], total: 1, limit: 50, offset: 0 } }),
  )
  await page.goto(`/chat/${cid}`)
  await expect(page.getByText('资料不足', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '查看来源 1', exact: true })).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.reload()
  await expect(page.getByText('资料不足', { exact: true })).toBeVisible()
  await expect(page.locator('.markdown')).toContainText(refused.answer)
})
test('assembled source displays the actual content once with extraction and neighbor context', async ({
  page,
}) => {
  const content = '员工每年享有十天带薪年假。申请需要主管审批。'
  const packed = {
    ...source,
    content,
    truncated: true,
    evidence_chunks: [
      {
        chunk_id: 'main',
        chunk_index: 0,
        start: 10,
        end: 30,
        page: 1,
        role: 'main',
        content: '员工每年享有十天带薪年假。',
      },
      {
        chunk_id: 'neighbor',
        chunk_index: 1,
        start: 0,
        end: 10,
        page: 1,
        role: 'neighbor',
        content: '申请需要主管审批。',
      },
    ],
    supporting_chunks: [{ chunk_id: 'neighbor', content: '申请需要主管审批。' }],
  }
  await page.route('**/turns?*', (route) =>
    route.fulfill({
      json: { items: [{ ...run, sources: [packed] }], total: 1, limit: 50, offset: 0 },
    }),
  )
  await page.goto(`/chat/${cid}`)
  await page.getByRole('button', { name: '查看来源 1', exact: true }).click()
  const panel = page.getByRole('dialog')
  await expect(panel.getByText(content, { exact: true })).toHaveCount(1)
  await expect(panel.getByText('按问题与上下文预算抽取原文')).toBeVisible()
  await expect(panel).toContainText('本段包含 2 个原文片段')
  await expect(panel.getByRole('heading', { name: '相邻补充片段' })).toHaveCount(0)
})
test('retrieval scope and mode follow real API semantics', async ({ page }) => {
  await page.goto('/retrieval')
  await page.getByLabel('查询问题', { exact: true }).fill('年假')
  await page.getByLabel('检索方式', { exact: true }).selectOption('keyword')
  await expect(page.getByLabel('最低向量分数')).toHaveCount(0)
  await page.getByLabel('检索范围', { exact: true }).selectOption('true')
  const request = page.waitForRequest((r) => r.url().endsWith('/search') && r.method() === 'POST')
  await page.getByRole('button', { name: '开始检索' }).click()
  expect((await request).postDataJSON()).toMatchObject({ file_ids: [], mode: 'keyword' })
  await expect(page.getByText('年假制度.md', { exact: true })).toBeVisible()
  await expect(page.getByText('2.5000')).toBeVisible()
})
test('files page handles empty state, queue validation and batch index', async ({ page }) => {
  await page.goto('/knowledge')
  await expect(page.getByText('知识从第一份文档开始')).toBeVisible()
  await page
    .getByLabel('选择上传文件')
    .setInputFiles({ name: 'empty.txt', mimeType: 'text/plain', buffer: Buffer.from('') })
  await expect(page.getByRole('alert')).toContainText('单文件不超过 50 MiB')
  await page.getByRole('button', { name: '构建 / 重试索引' }).click()
  await expect(page.getByRole('status')).toContainText('没有需要构建索引')
})
test('mobile navigation and workspace remain accessible', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/chat')
  await expect(page.getByRole('heading', { name: /让每一次提问/ })).toBeVisible()
  await page.screenshot({
    path: 'test-results/mobile-chat.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '打开导航' }).click()
  await page.getByRole('link', { name: /知识库文件/ }).click()
  await expect(page.getByRole('heading', { name: '知识库文件.' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: 'test-results/mobile-knowledge.png',
    fullPage: true,
    animations: 'disabled',
  })
})

test('SSE reconnect resumes cursor and replaces drafts with the durable answer', async ({
  page,
}) => {
  let complete = false,
    streams = 0
  const cursors: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname
    if (path.endsWith('/auth/status')) {
      await route.fulfill({
        json: {
          has_users: true,
          setup_required: false,
          registration_enabled: false,
          password_min_length: 8,
        },
      })
      return
    }
    if (path.endsWith('/auth/me')) {
      await route.fulfill({
        json: {
          user: {
            user_id: 'local',
            username: 'local',
            display_name: '本地管理员',
            role: 'admin',
            is_active: true,
            created_at: '',
            updated_at: '',
            anonymous: true,
            service: false,
          },
          permissions: {
            ingest_files: true,
            manage_knowledge_bases: true,
            manage_models: true,
            manage_users: true,
          },
        },
      })
      return
    }
    if (path.endsWith('/knowledge-bases')) {
      await route.fulfill({ json: defaultBases })
      return
    }
    const current = {
      ...run,
      status: complete ? 'completed' : 'running',
      answer: complete ? run.answer : null,
      sources: complete ? run.sources : [],
    }
    if (path.endsWith('/events')) {
      cursors.push(url.searchParams.get('after') || '')
      const frame = (seq: number, event: string, data: unknown) =>
        `id: ${rid}:${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`
      const body =
        streams++ === 0
          ? frame(1, 'message_start', { message_id: 'old' }) +
            frame(2, 'text_delta', { message_id: 'old', text: '未完成的旧草稿' }) +
            'event: stream_error\ndata: {"message":"连接中断"}\n\n'
          : frame(3, 'message_reset', { message_id: 'old' }) +
            frame(4, 'message_start', { message_id: 'new' }) +
            frame(5, 'text_delta', { message_id: 'new', text: '新草稿' }) +
            frame(6, 'done', { answer: run.answer, sources: run.sources })
      if (streams > 1) complete = true
      await route.fulfill({ contentType: 'text/event-stream', body })
    } else {
      const data = path.endsWith('/turns')
        ? { items: [current], total: 1, limit: 50, offset: 0 }
        : path.endsWith(`/runs/${rid}`)
          ? current
          : path.endsWith('/conversations')
            ? [{ ...conversation, active_run_id: complete ? null : rid }]
            : { status: 'ok', chat_ready: true }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    }
  })
  await page.goto(`/chat/${cid}`)
  await expect(page.getByText('员工有十天年假。')).toBeVisible({ timeout: 15000 })
  expect(cursors.slice(0, 2)).toEqual(['0', '2'])
  await expect(page.getByText('未完成的旧草稿')).toHaveCount(0)
  await expect(page.getByText('新草稿', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '查看来源 1', exact: true })).toBeVisible()
})

test('retrying an uncertain submission keeps the same idempotency key', async ({ page }) => {
  const keys: string[] = []
  let complete = false
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/status')) {
      await route.fulfill({
        json: {
          has_users: true,
          setup_required: false,
          registration_enabled: false,
          password_min_length: 8,
        },
      })
      return
    }
    if (path.endsWith('/auth/me')) {
      await route.fulfill({
        json: {
          user: {
            user_id: 'local',
            username: 'local',
            display_name: '本地管理员',
            role: 'admin',
            is_active: true,
            created_at: '',
            updated_at: '',
            anonymous: true,
            service: false,
          },
          permissions: {
            ingest_files: true,
            manage_knowledge_bases: true,
            manage_models: true,
            manage_users: true,
          },
        },
      })
      return
    }
    if (path.endsWith('/knowledge-bases')) {
      await route.fulfill({ json: defaultBases })
      return
    }
    if (path.endsWith('/runs') && route.request().method() === 'POST') {
      keys.push(route.request().postDataJSON().idempotency_key)
      if (keys.length === 1) {
        await route.abort('connectionfailed')
        return
      }
      complete = true
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(run) })
      return
    }
    const data = path.endsWith('/turns')
      ? { items: complete ? [run] : [], total: complete ? 1 : 0, limit: 50, offset: 0 }
      : path.endsWith('/conversations')
        ? [conversation]
        : { status: 'ok', chat_ready: true }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
  })
  await page.goto(`/chat/${cid}`)
  await page.getByLabel('输入问题', { exact: true }).fill('员工有多少天年假？')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByText('员工有十天年假。')).toBeVisible()
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBe(keys[1])
})

test('Markdown cannot execute HTML or turn code and years into citations', async ({ page }) => {
  await page.route('**/turns?*', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            ...run,
            answer:
              '<script>alert(1)</script>\n\n`array[1]` [2026] [1] [恶意链接](javascript:alert(1))',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    }),
  )
  await page.goto(`/chat/${cid}`)
  await expect(page.getByRole('button', { name: '查看来源 1', exact: true })).toHaveCount(1)
  await expect(page.locator('.markdown code')).toHaveText('array[1]')
  await expect(page.locator('.markdown script')).toHaveCount(0)
  await expect(page.locator('.markdown a[href^="javascript:"]')).toHaveCount(0)
})

test('desktop layouts fit 1440 and 1024 pixel viewports', async ({ page }) => {
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 960 })
    for (const path of ['/chat', '/knowledge', '/retrieval']) {
      await page.goto(path)
      await expect(page.getByRole('link', { name: '知序 RAG WORKSPACE' })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      )
      if (width === 1440)
        await page.screenshot({ path: `test-results/desktop-${path.slice(1)}.png`, fullPage: true })
    }
  }
})

test('rename and delete conversations with persistent titles, confirmation and busy errors', async ({
  page,
}) => {
  let items = [{ ...conversation, title: null as string | null }]
  let busy = true
  await page.route('**/api/v1/conversations?*', (route) => route.fulfill({ json: items }))
  await page.route(`**/api/v1/conversations/${cid}`, async (route) => {
    if (route.request().method() === 'PATCH') {
      items[0]!.title = route.request().postDataJSON().title
      return route.fulfill({ json: items[0] })
    }
    if (route.request().method() === 'DELETE') {
      if (busy)
        return route.fulfill({
          status: 409,
          json: { detail: { code: 'conversation_busy', message: '请先取消任务。' } },
        })
      items = []
      return route.fulfill({ json: { thread_id: cid, deleted: true, cleanup_pending: 0 } })
    }
    return route.fallback()
  })
  await page.goto(`/chat/${cid}`)
  await page.getByRole('button', { name: '重命名会话 111111' }).click()
  await page.screenshot({ path: 'test-results/manage-conversation.png', fullPage: true })
  await page.getByLabel('会话名称').fill('年假咨询')
  await page.getByRole('button', { name: '保存名称' }).click()
  await expect(page.getByRole('link', { name: /年假咨询/ })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('link', { name: /年假咨询/ })).toBeVisible()
  await page.getByRole('button', { name: '删除会话 年假咨询' }).click()
  await page
    .getByRole('dialog', { name: '删除会话' })
    .getByRole('button', { name: '取消', exact: true })
    .click()
  expect(items).toHaveLength(1)
  await page.getByRole('button', { name: '删除会话 年假咨询' }).click()
  await page.getByRole('button', { name: '确认删除会话' }).click()
  await expect(page.getByRole('dialog').getByText('请先取消任务。')).toBeVisible()
  busy = false
  await page.getByRole('button', { name: '确认删除会话' }).click()
  await expect(page).toHaveURL(/\/chat$/)
  await expect(page.getByText('你的对话会保存在这里')).toBeVisible()
  await expect(page.getByRole('heading', { name: /让每一次提问/ })).toBeVisible()
})

test('download and delete files, preserve original library target and clear upload leftovers', async ({
  page,
}) => {
  const item = {
    file_id: 'managed-file',
    kb_id: 'default',
    name: '管理制度.txt',
    extension: '.txt',
    size: 10,
    index_status: 'indexed',
    chunk_count: 1,
    keyword_ready: true,
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
  }
  let files = [item]
  let busy = true
  await page.route('**/api/v1/knowledge-bases/default/files?*', (route) =>
    route.fulfill({ json: { items: files, total: files.length, limit: 20, offset: 0 } }),
  )
  await page.route('**/api/v1/knowledge-bases/default/files/managed-file/download', (route) =>
    route.fulfill({
      body: '员工年假十天。',
      contentType: 'application/octet-stream',
      headers: { 'Content-Disposition': "attachment; filename*=UTF-8''test.txt" },
    }),
  )
  await page.route('**/api/v1/knowledge-bases/default/files/managed-file', (route) => {
    if (busy)
      return route.fulfill({
        status: 409,
        json: { detail: { code: 'knowledge_base_busy', message: '索引正在执行。' } },
      })
    files = []
    return route.fulfill({ json: { file_id: item.file_id, deleted: true, cleanup_pending: 1 } })
  })
  await page.route('**/api/v1/maintenance/files/gc', (route) =>
    route.fulfill({ json: { queued: 2, cleanup_pending: 0 } }),
  )
  await page.goto('/knowledge')
  const downloading = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 管理制度.txt' }).click()
  expect((await downloading).suggestedFilename()).toBe('管理制度.txt')
  await page.getByRole('button', { name: '删除文件 管理制度.txt' }).click()
  await page.getByRole('button', { name: '确认删除文件' }).click()
  await expect(page.getByRole('dialog').getByText('索引正在执行。')).toBeVisible()
  busy = false
  await page.getByRole('button', { name: '确认删除文件' }).click()
  await expect(page.getByRole('cell', { name: /管理制度.txt/ })).toHaveCount(0)
  await expect(page.getByText('文件已删除，原文件清理将在后续重试。')).toBeVisible()
  await page.getByRole('button', { name: '清理上传残留' }).click()
  await expect(
    page.getByText('已检查全部知识库，新增清理 2 个过期孤儿文件；待重试 0 个。'),
  ).toBeVisible()
})

test('chat file scope distinguishes all, selected and empty; uncertain retry preserves scope and key', async ({
  page,
}) => {
  const submissions: any[] = []
  await page.route('**/api/v1/knowledge-bases/default/files?*', (route) =>
    route.fulfill({
      json: {
        items: [{ file_id: 'selected-file', name: '指定制度.txt' }],
        total: 1,
        limit: 20,
        offset: 0,
      },
    }),
  )
  await page.route(`**/api/v1/conversations/${cid}/runs`, async (route) => {
    submissions.push(route.request().postDataJSON())
    await route.fulfill({ status: 503, json: { detail: { message: '暂时不可用' } } })
  })
  await page.goto(`/chat/${cid}`)
  await page.getByText('问答范围：全部文件', { exact: true }).click()
  await page.getByLabel('仅在指定文件中查找').check()
  await page.getByLabel('指定制度.txt').check()
  await page.getByLabel('输入问题').fill('年假多少天？')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByRole('button', { name: '发送问题' })).toBeEnabled()
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect.poll(() => submissions.length).toBe(2)
  expect(submissions[0].file_ids).toEqual(['selected-file'])
  expect(submissions[1]).toEqual(submissions[0])
  await expect(page.getByRole('button', { name: '发送问题' })).toBeEnabled()
  await page.getByLabel('指定制度.txt').uncheck()
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect.poll(() => submissions.length).toBe(3)
  expect(submissions[2].file_ids).toEqual([])
  expect(submissions[2].idempotency_key).not.toBe(submissions[0].idempotency_key)
  await expect(page.getByRole('button', { name: '发送问题' })).toBeEnabled()
  await page.getByLabel('仅在指定文件中查找').uncheck()
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect.poll(() => submissions.length).toBe(4)
  expect(submissions[3].file_ids).toBeNull()
})

test('retry an earlier failed upload while a later file is uploading', async ({ page }) => {
  let attempts = 0
  let releaseSecond!: () => void
  const secondGate = new Promise<void>((resolve) => {
    releaseSecond = resolve
  })
  await page.route('**/api/v1/knowledge-bases/default/files', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataBuffer()?.toString() || ''
    if (body.includes('first.txt')) {
      attempts++
      if (attempts === 1)
        return route.fulfill({ status: 503, json: { detail: { message: '暂时不可用' } } })
    } else await secondGate
    return route.fulfill({ status: 201, json: { file_id: 'uploaded' } })
  })
  await page.goto('/knowledge')
  await expect(page.getByLabel('当前知识库', { exact: true })).toHaveValue('default')
  await page.getByLabel('选择上传文件').setInputFiles([
    { name: 'first.txt', mimeType: 'text/plain', buffer: Buffer.from('first') },
    { name: 'second.txt', mimeType: 'text/plain', buffer: Buffer.from('second') },
  ])
  const first = page.locator('.upload-row').filter({ hasText: 'first.txt' })
  const second = page.locator('.upload-row').filter({ hasText: 'second.txt' })
  try {
    await expect(second).toContainText('上传中')
    await first.getByRole('button', { name: '重试', exact: true }).click()
    await expect(first).toContainText('等待上传')
  } finally {
    releaseSecond()
  }
  await expect(first).toContainText('已上传')
  await expect(second).toContainText('已上传')
  expect(attempts).toBe(2)
})

test('refresh all loaded conversation pages after a new record shifts the boundary', async ({
  page,
}) => {
  await page.clock.install()
  const records = Array.from({ length: 100 }, (_, i) => ({
    ...conversation,
    thread_id: `conversation-${i + 1}`,
    title: `记录-${i + 1}`,
  }))
  await page.route('**/api/v1/conversations?*', async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'))
    return route.fulfill({ json: records.slice(offset, offset + 50) })
  })
  await page.goto('/chat')
  await page.getByRole('button', { name: '加载更多会话', exact: true }).click()
  await expect(page.getByRole('link', { name: '记录-100', exact: false })).toBeVisible()
  records.unshift({ ...conversation, thread_id: 'new-conversation', title: '新增记录' })
  await page.clock.fastForward(16000)
  await expect(page.getByRole('link', { name: '新增记录', exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: /^记录-50 / })).toHaveCount(1)
  await expect(page.locator('.conversation-row')).toHaveCount(100)
  await page.getByRole('button', { name: '加载更多会话', exact: true }).click()
  await expect(page.locator('.conversation-row')).toHaveCount(101)
})

test('refresh loaded history pages without dropping the previous page boundary', async ({
  page,
}) => {
  await page.clock.install()
  const records = Array.from({ length: 100 }, (_, i) => ({
    ...run,
    run_id: `history-${i + 1}`,
    message: `历史问题-${i + 1}`,
  }))
  await page.route('**/api/v1/conversations/*/turns?*', async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'))
    return route.fulfill({
      json: { items: records.slice(offset, offset + 50), total: records.length, limit: 50, offset },
    })
  })
  await page.goto(`/chat/${cid}`)
  await page.getByRole('button', { name: '加载更早的问答', exact: true }).click()
  await expect(page.locator('.turn')).toHaveCount(100)
  records.unshift({ ...run, run_id: 'new-history', message: '新问题' })
  // Expire the configured 3s stale time before triggering a window refresh.
  await page.clock.fastForward(4000)
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
  await page.clock.runFor(50)
  await expect(page.getByText('新问题', { exact: true })).toBeVisible()
  await expect(page.getByText('历史问题-50', { exact: true })).toHaveCount(1)
  await expect(page.locator('.turn')).toHaveCount(100)
  await page.getByRole('button', { name: '加载更早的问答', exact: true }).click()
  await expect(page.locator('.turn')).toHaveCount(101)
})

test('desktop history can collapse without losing the current draft and remembers its state', async ({
  page,
}) => {
  await page.goto(`/chat/${cid}`)
  await page.getByLabel('输入问题').fill('保留当前问题')
  await expect(page.locator('#conversation-history')).toBeVisible()
  const expandedWidth = (await page.locator('.chat-main').boundingBox())!.width
  await page.getByRole('button', { name: '隐藏对话记录', exact: true }).click()
  await expect(page.locator('#conversation-history')).toBeHidden()
  await expect(page.getByRole('button', { name: '显示对话记录', exact: true })).toHaveAttribute(
    'aria-expanded',
    'false',
  )
  expect((await page.locator('.chat-main').boundingBox())!.width).toBeGreaterThan(expandedWidth)
  await expect(page.getByLabel('输入问题')).toHaveValue('保留当前问题')
  await page.screenshot({
    path: 'test-results/history-collapsed.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.reload()
  await expect(page.locator('#conversation-history')).toBeHidden()
  await page.getByRole('button', { name: '显示对话记录', exact: true }).click()
  await expect(page.locator('#conversation-history')).toBeVisible()
})

test('retrieval selector supports keyboard, outside dismissal, disabled state and submits the chosen mode', async ({
  page,
}) => {
  const submitted: any[] = []
  await page.route('**/api/v1/conversations/*/runs', async (route) => {
    submitted.push(route.request().postDataJSON())
    await route.fulfill({ status: 202, json: run })
  })
  await page.goto(`/chat/${cid}`)
  const picker = page.getByRole('combobox', { name: '问答检索方式', exact: true })
  await expect(picker).toContainText('混合检索')
  await picker.focus()
  await picker.press('ArrowDown')
  await expect(page.getByRole('listbox', { name: '问答检索方式' })).toBeVisible()
  await expect(
    page.getByRole('listbox', { name: '问答检索方式' }).getByRole('option', { selected: true }),
  ).toContainText('混合检索')
  await picker.press('ArrowDown')
  await picker.press('Enter')
  await expect(picker).toContainText('全文检索')
  await expect(page.getByRole('listbox')).toHaveCount(0)
  await picker.click()
  await page.screenshot({
    path: 'test-results/retrieval-selector.png',
    fullPage: true,
    animations: 'disabled',
  })
  await picker.press('Escape')
  await expect(picker).toBeFocused()
  await expect(page.getByRole('listbox')).toHaveCount(0)
  await picker.click()
  await page.getByLabel('输入问题').click({ position: { x: 15, y: 15 } })
  await expect(page.getByRole('listbox')).toHaveCount(0)
  await page.getByLabel(/使用知识库/).uncheck()
  await expect(picker).toBeDisabled()
  await page.getByLabel(/使用知识库/).check()
  await page.getByLabel('输入问题').fill('年假制度')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect.poll(() => submitted.length).toBe(1)
  expect(submitted[0].retrieval_mode).toBe('keyword')
})

test('mobile history opens as an overlay and the retrieval menu fits inside the viewport', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/chat')
  await expect(page.locator('#conversation-history')).toBeHidden()
  await page.getByRole('button', { name: '显示对话记录', exact: true }).click()
  await expect(page.locator('#conversation-history')).toBeVisible()
  await page
    .getByRole('button', { name: '关闭会话记录', exact: true })
    .click({ position: { x: 360, y: 250 } })
  await expect(page.locator('#conversation-history')).toBeHidden()
  await page.getByRole('combobox', { name: '问答检索方式', exact: true }).click()
  const menu = await page.getByRole('listbox').boundingBox()
  expect(menu!.x).toBeGreaterThanOrEqual(0)
  expect(menu!.x + menu!.width).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: 'test-results/mobile-selector.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('option', { name: /向量检索/ }).click()
  await expect(page.getByRole('combobox', { name: '问答检索方式', exact: true })).toContainText(
    '向量检索',
  )
})
