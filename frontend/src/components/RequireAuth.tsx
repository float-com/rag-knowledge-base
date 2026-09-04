/**
 * 登录态守卫。
 *
 * - hydrate 未完成时：渲染加载占位（避免白屏后又突然重定向）
 * - 无 token → 重定向到 /login，并把当前路径 query 带回
 * - 有 token → 启动时调一次 /auth/me 把最新角色 / 权限同步到 store；
 *   失败（401 / 用户被删 / 被禁用）由 client 拦截器自动跳登录页
 */

import { useEffect } from 'react'
import { Spin } from 'antd'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchCurrentUser } from '@/api/auth'
import { currentUserKey } from '@/api/queryKeys'
import { useAuthStore } from '@/stores/authStore'

export function RequireAuth() {
  const location = useLocation()
  const ready = useAuthStore((s) => s.ready)
  const token = useAuthStore((s) => s.token)
  const setUser = useAuthStore((s) => s.setUser)

  const { data: freshUser } = useQuery({
    queryKey: currentUserKey,
    queryFn: fetchCurrentUser,
    enabled: ready && Boolean(token),
    staleTime: 60_000,
    retry: false,
  })

  useEffect(() => {
    if (freshUser) {
      setUser(freshUser)
    }
  }, [freshUser, setUser])

  if (!ready) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <Spin />
      </div>
    )
  }

  if (!token) {
    const back = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?back=${back}`} replace />
  }

  return <Outlet />
}
