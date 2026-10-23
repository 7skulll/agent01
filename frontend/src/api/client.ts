import type {
  AuthLogin,
  AuthSession,
  AuthStatus,
  AuthUser,
  Conversation,
  ModelsInfo,
  LLMInput,
  EmbeddingInput,
  FileList,
  Health,
  KnowledgeBase,
  Mode,
  Run,
  SearchResult,
  Turns,
  UserCreate,
  UserUpdate,
} from './types'
export const base = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '') + '/api/v1'
const TOKEN_KEY = 'rag.auth.token'
function storedToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}
/** 当前登录会话令牌，仅保存在浏览器本地；服务端只保存摘要。 */
export const sessionToken = { value: storedToken() }
export function setSessionToken(token: string | null) {
  sessionToken.value = token || ''
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式等无法持久化时仅保留内存状态 */
  }
}
export function authHeaders(): Record<string, string> {
  return sessionToken.value ? { Authorization: `Bearer ${sessionToken.value}` } : {}
}
const unauthorizedHandlers = new Set<() => void>()
/** 会话失效：清除本地令牌并通知界面回到登录页。 */
export function sessionExpired() {
  setSessionToken(null)
  unauthorizedHandlers.forEach((handler) => handler())
}
/** 会话失效时通知界面回到登录页；返回取消订阅函数。 */
export function onUnauthorized(handler: () => void) {
  unauthorizedHandlers.add(handler)
  return () => unauthorizedHandlers.delete(handler)
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public retryable = false,
  ) {
    super(message)
  }
}
export async function readError(response: Response): Promise<ApiError> {
  let detail: any
  try {
    detail = (await response.json()).detail
  } catch {
    /* non-JSON proxy errors */
  }
  return new ApiError(
    response.status,
    detail?.code || 'http_error',
    detail?.message || `请求失败（${response.status}）`,
    detail?.request_id || response.headers.get('X-Request-ID') || undefined,
    detail?.retryable ?? response.status >= 500,
  )
}
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(
    () => controller.abort(new Error('请求超时，请检查连接后重试。')),
    60000,
  )
  const abort = () => controller.abort(init.signal?.reason)
  if (init.signal?.aborted) abort()
  init.signal?.addEventListener('abort', abort, { once: true })
  try {
    const response = await fetch(base + path, {
      ...init,
      signal: controller.signal,
      headers: {
        ...(init.body && !(init.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...authHeaders(),
        ...init.headers,
      },
    })
    if (!response.ok) {
      const error = await readError(response)
      if (response.status === 401 && !path.startsWith('/auth/')) sessionExpired()
      throw error
    }
    return (await response.json()) as T
  } finally {
    clearTimeout(timeout)
    init.signal?.removeEventListener('abort', abort)
  }
}
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export const api = {
  authStatus: () => request<AuthStatus>('/auth/status'),
  login: (username: string, password: string) =>
    post<AuthLogin>('/auth/login', { username, password }),
  setup: (username: string, password: string, displayName: string) =>
    post<AuthLogin>('/auth/setup', {
      username,
      password,
      display_name: displayName,
    }),
  register: (username: string, password: string, displayName: string) =>
    post<AuthLogin>('/auth/register', {
      username,
      password,
      display_name: displayName,
    }),
  logout: () => post<{ ok: boolean }>('/auth/logout'),
  me: () => request<AuthSession>('/auth/me'),
  changePassword: (currentPassword: string, newPassword: string) =>
    post<AuthLogin>('/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  users: () => request<AuthUser[]>('/users'),
  createUser: (body: UserCreate) => post<AuthUser>('/users', body),
  updateUser: (id: string, body: UserUpdate) =>
    request<AuthUser>(`/users/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  deleteUser: (id: string) =>
    request<{ user_id: string; deleted: boolean }>(`/users/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  modelSettings: () => request<ModelsInfo>('/settings/models'),
  saveLLM: (body: LLMInput) =>
    request<ModelsInfo>('/settings/models/llm', { method: 'PUT', body: JSON.stringify(body) }),
  saveEmbedding: (body: EmbeddingInput) =>
    request<ModelsInfo>('/settings/models/embedding', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  resetModel: (kind: 'llm' | 'embedding') =>
    request<ModelsInfo>(`/settings/models/${kind}`, { method: 'DELETE' }),
  knowledgeBases: () => request<KnowledgeBase[]>('/knowledge-bases'),
  createKnowledgeBase: (name: string) => post<KnowledgeBase>('/knowledge-bases', { name }),
  deleteKnowledgeBase: (id: string) =>
    request<{ kb_id: string; cleanup_pending: number }>(
      `/knowledge-bases/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  health: () => request<Health>('/health'),
  conversations: (offset = 0) =>
    request<Conversation[]>(`/conversations?limit=50&offset=${offset}`),
  createConversation: () => post<Conversation>('/conversations'),
  renameConversation: (id: string, title: string) =>
    request<Conversation>(`/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (id: string) =>
    request<{ cleanup_pending: number }>(`/conversations/${id}`, {
      method: 'DELETE',
    }),
  turns: (id: string, offset = 0) =>
    request<Turns>(`/conversations/${id}/turns?limit=50&offset=${offset}`),
  conversation: (id: string) => request<{ conversation: Conversation }>(`/conversations/${id}`),
  createRun: (
    id: string,
    message: string,
    key: string,
    mode: Mode,
    kbId: string | null,
    fileIds: string[] | null = null,
  ) =>
    post<Run>(`/conversations/${id}/runs`, {
      message,
      idempotency_key: key,
      retrieval_mode: mode,
      knowledge_base_id: kbId,
      file_ids: fileIds,
    }),
  run: (id: string) => request<Run>(`/runs/${id}`),
  control: (id: string, action: 'pause' | 'resume' | 'cancel') =>
    post<Run>(`/runs/${id}/${action}`),
  files: (kbId: string, offset = 0) =>
    request<FileList>(
      `/knowledge-bases/${encodeURIComponent(kbId)}/files?limit=20&offset=${offset}`,
    ),
  allFiles: async (kbId: string) => {
    const items: FileList['items'] = []
    let offset = 0
    while (true) {
      const page = await api.files(kbId, offset)
      items.push(...page.items)
      offset += page.items.length
      if (offset >= page.total || !page.items.length) break
    }
    return items
  },
  upload: (kbId: string, file: File, key: string) => {
    const form = new FormData()
    form.append('file', file)
    return request(`/knowledge-bases/${encodeURIComponent(kbId)}/files`, {
      method: 'POST',
      body: form,
      headers: { 'Idempotency-Key': key },
    })
  },
  downloadFile: async (kbId: string, id: string, name: string) => {
    const response = await fetch(
      `${base}/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(id)}/download`,
      {
        headers: authHeaders(),
        signal: AbortSignal.timeout(60000),
      },
    )
    if (!response.ok) {
      if (response.status === 401) sessionExpired()
      throw await readError(response)
    }
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = name
    document.body.append(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
  deleteFile: (kbId: string, id: string) =>
    request<{ cleanup_pending: number }>(
      `/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  collectOrphans: () => post<{ queued: number; cleanup_pending: number }>('/maintenance/files/gc'),
  index: (kbId: string) =>
    post<{ count: number }>(`/knowledge-bases/${encodeURIComponent(kbId)}/index`),
  search: (kbId: string, body: unknown, signal?: AbortSignal) =>
    request<SearchResult>(`/knowledge-bases/${encodeURIComponent(kbId)}/search`, {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    }),
}
export const date = (value: string) =>
  new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
export const size = (bytes: number) =>
  bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 ** 2
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 ** 2).toFixed(1)} MB`
