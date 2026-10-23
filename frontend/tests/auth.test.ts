import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, authHeaders, sessionToken, setSessionToken } from '../src/api/client'
import {
  auth,
  bootstrap,
  canIngest,
  isAdmin,
  logout,
  registrationEnabled,
  role,
} from '../src/features/auth/store'
import type { AuthUser, Permissions } from '../src/api/types'

function user(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    user_id: 'u1',
    username: 'tester',
    display_name: '测试账号',
    role: 'viewer',
    is_active: true,
    created_at: '',
    updated_at: '',
    anonymous: false,
    service: false,
    ...overrides,
  }
}

function permissions(overrides: Partial<Permissions> = {}): Permissions {
  return {
    ingest_files: false,
    manage_knowledge_bases: false,
    manage_models: false,
    manage_users: false,
    ...overrides,
  }
}

beforeEach(() => {
  auth.user = null
  auth.permissions = null
  auth.status = null
  auth.ready = false
  auth.error = null
  setSessionToken(null)
})
afterEach(() => vi.restoreAllMocks())

describe('启动与恢复登录状态', () => {
  const status = {
    has_users: true,
    setup_required: false,
    registration_enabled: true,
    password_min_length: 8,
  }
  it('未初始化时显示管理员创建页，不接受匿名管理员会话', async () => {
    vi.spyOn(api, 'authStatus').mockResolvedValue({
      ...status,
      has_users: false,
      setup_required: true,
    })
    const me = vi.spyOn(api, 'me')
    await bootstrap()
    expect(auth.ready).toBe(true)
    expect(auth.status?.setup_required).toBe(true)
    expect(auth.user).toBeNull()
    expect(me).not.toHaveBeenCalled()
    await logout()
    expect(auth.status?.setup_required).toBe(true)
  })
  it.each([new TypeError('Failed to fetch'), new ApiError(503, 'storage_unavailable', '暂不可用')])(
    '连接故障保留令牌，重试成功后恢复会话：%s',
    async (error) => {
      setSessionToken('valid-token')
      vi.spyOn(api, 'authStatus').mockResolvedValue(status)
      const me = vi
        .spyOn(api, 'me')
        .mockRejectedValueOnce(error)
        .mockResolvedValueOnce({ user: user(), permissions: permissions() })
      await bootstrap()
      expect(sessionToken.value).toBe('valid-token')
      expect(auth.error).toBeTruthy()
      await bootstrap()
      expect(me).toHaveBeenCalledTimes(2)
      expect(auth.user?.user_id).toBe('u1')
      expect(auth.error).toBeNull()
    },
  )
  it('明确的 401 才清除令牌并回到登录页', async () => {
    setSessionToken('expired-token')
    vi.spyOn(api, 'authStatus').mockResolvedValue(status)
    vi.spyOn(api, 'me').mockRejectedValue(new ApiError(401, 'unauthorized', '登录已失效'))
    await bootstrap()
    expect(sessionToken.value).toBe('')
    expect(auth.user).toBeNull()
    expect(auth.error).toBeNull()
  })
  it('读取初始化状态失败时也保留令牌，并允许重试', async () => {
    setSessionToken('valid-token')
    vi.spyOn(api, 'authStatus').mockRejectedValue(new TypeError('Failed to fetch'))
    const me = vi.spyOn(api, 'me')
    await bootstrap()
    expect(sessionToken.value).toBe('valid-token')
    expect(auth.error).toBeTruthy()
    expect(me).not.toHaveBeenCalled()
  })
})

describe('权限来自后端 permissions', () => {
  it('普通用户只读', () => {
    auth.user = user({ role: 'viewer' })
    auth.permissions = permissions()
    expect(role.value).toBe('viewer')
    expect(canIngest.value).toBe(false)
    expect(isAdmin.value).toBe(false)
  })

  it('员工可以入库但不能管理用户', () => {
    auth.user = user({ role: 'staff' })
    auth.permissions = permissions({ ingest_files: true, manage_knowledge_bases: true })
    expect(canIngest.value).toBe(true)
    expect(isAdmin.value).toBe(false)
  })

  it('管理员拥有全部权限', () => {
    auth.user = user({ role: 'admin' })
    auth.permissions = permissions({
      ingest_files: true,
      manage_knowledge_bases: true,
      manage_models: true,
      manage_users: true,
    })
    expect(canIngest.value).toBe(true)
    expect(isAdmin.value).toBe(true)
  })

  it('缺少登录态时按无权限处理', () => {
    expect(role.value).toBe('')
    expect(canIngest.value).toBe(false)
    expect(isAdmin.value).toBe(false)
  })
})

describe('会话令牌', () => {
  it('仅在存在令牌时附加 Authorization 头', () => {
    expect(authHeaders()).toEqual({})
    setSessionToken('token-123')
    expect(authHeaders()).toEqual({ Authorization: 'Bearer token-123' })
    setSessionToken(null)
    expect(authHeaders()).toEqual({})
  })
})

describe('注册开关', () => {
  it('根据后端状态显示注册入口', () => {
    auth.status = {
      has_users: true,
      setup_required: false,
      registration_enabled: true,
      password_min_length: 8,
    }
    expect(registrationEnabled.value).toBe(true)
    auth.status = { ...auth.status, registration_enabled: false }
    expect(registrationEnabled.value).toBe(false)
  })
})
