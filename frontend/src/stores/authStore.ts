/**
 * 登录态：token + 当前用户基本信息 + 有效权限标签 + 是否管理员。
 *
 * 持久化策略：
 * - token / user 基本信息进 localStorage，刷新页不必重新登录
 * - 启动时 hydrate() 从 localStorage 读 token；hasToken() 决定是否进登录页
 * - 由顶层 RequireAuth 组件在 mount 时调 /auth/me 拉一次最新用户信息（保证角色变更立即生效）
 */

import { create } from 'zustand'

const TOKEN_KEY = 'rag-kb.auth.token'
const USER_KEY = 'rag-kb.auth.user'

export interface AuthUser {
  id: string
  username: string
  displayName: string
  permissionTags: string[]
  isAdmin: boolean
  roleNames: string[]
}

interface AuthState {
  token: string | null
  user: AuthUser | null
  ready: boolean // hydrate 完成后置 true
  setAuth: (token: string, user: AuthUser) => void
  setUser: (user: AuthUser) => void
  logout: () => void
  hasToken: () => boolean
  hydrate: () => void
}

function loadUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as AuthUser
  } catch {
    return null
  }
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  ready: false,

  setAuth: (token, user) => {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
    set({ token, user })
  },

  setUser: (user) => {
    localStorage.setItem(USER_KEY, JSON.stringify(user))
    set({ user })
  },

  logout: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    set({ token: null, user: null })
  },

  hasToken: () => Boolean(get().token),

  hydrate: () => {
    const token = localStorage.getItem(TOKEN_KEY)
    const user = loadUser()
    set({ token, user, ready: true })
  },
}))

/** 给 SSE / 拦截器用：直接读最新 token，不订阅 store。 */
export function getAuthToken(): string | null {
  return useAuthStore.getState().token
}
