import { computed, reactive } from 'vue'
import { api, ApiError, onUnauthorized, setSessionToken } from '../../api/client'
import type { AuthLogin, AuthStatus, AuthUser, Permissions, Role } from '../../api/types'

/** 登录态与权限的唯一来源；后端返回的 permissions 才是权限判定依据。 */
export const auth = reactive<{
  ready: boolean
  status: AuthStatus | null
  user: AuthUser | null
  permissions: Permissions | null
  error: unknown
}>({ ready: false, status: null, user: null, permissions: null, error: null })

export const authenticated = computed(() => !!auth.user)
export const role = computed<Role | ''>(() => (auth.user?.role as Role) || '')
export const setupRequired = computed(() => !!auth.status?.setup_required)
export const registrationEnabled = computed(() => !!auth.status?.registration_enabled)
export const canIngest = computed(() => !!auth.permissions?.ingest_files)
export const isAdmin = computed(() => !!auth.permissions?.manage_users)

function apply(login: AuthLogin) {
  setSessionToken(login.token)
  auth.user = login.user
  auth.permissions = login.permissions
  auth.status = {
    has_users: true,
    setup_required: false,
    registration_enabled: auth.status?.registration_enabled ?? true,
    password_min_length: auth.status?.password_min_length ?? 8,
  }
}

/** 初始化优先；连接故障保留令牌供重试，只有明确的 401 才结束会话。 */
export async function bootstrap() {
  if (auth.ready && !auth.error) return
  auth.ready = false
  auth.error = null
  try {
    auth.status = await api.authStatus()
    if (auth.status.setup_required) {
      auth.user = null
      auth.permissions = null
      return
    }
    const session = await api.me()
    auth.user = session.user
    auth.permissions = session.permissions
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      setSessionToken(null)
      auth.user = null
      auth.permissions = null
    } else {
      auth.error = error
    }
  } finally {
    auth.ready = true
  }
}

export async function login(username: string, password: string) {
  apply(await api.login(username, password))
}

export async function setup(username: string, password: string, displayName: string) {
  apply(await api.setup(username, password, displayName))
}

export async function register(username: string, password: string, displayName: string) {
  apply(await api.register(username, password, displayName))
}

/** 重新读取当前账号与权限，用于管理员修改了自己的角色之后。 */
export async function refreshSession() {
  const session = await api.me()
  auth.user = session.user
  auth.permissions = session.permissions
}

export async function logout() {
  try {
    await api.logout()
  } catch {
    /* 会话可能已过期，本地退出不受影响 */
  }
  setSessionToken(null)
  auth.user = null
  auth.permissions = null
}

onUnauthorized(() => {
  auth.user = null
  auth.permissions = null
})
