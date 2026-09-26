/**
 * 登录态守卫。
 *
 * - hydrate 未完成时：渲染加载占位（避免白屏后又突然重定向）
 * - 无 token → 重定向到 /login，并把当前路径 query 带回
 * - 有 token → 启动时调一次 /auth/me 把最新角色 / 权限同步到 store；
 *   失败（401 / 用户被删 / 被禁用）由 client 拦截器自动跳登录页
 *
 * 【第 11 期 · 为什么必须手写 ready 判断】
 * React 首次渲染时 main.tsx 里的 hydrate() 还没执行完，token 仍是 null。
 * 若直接判定"没有 token → 跳登录页"，刷新页面就会先闪一下登录页再跳回原页。
 * 所以要用 ready 先渲染 loading，等 hydrate 执行完再决定去留。
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

  // 【为什么每次进应用都要拉一次 /auth/me】
  // localStorage 里的 user 只是"登录那一刻的快照"。后端改了角色 / 权限标签之后，
  // 前端若不主动同步，就会一直用旧权限渲染菜单与按钮。
  // 这里拉一次最新值写回 store，配合后端 get_current_user 每请求查库，
  // 才能做到"权限变更立即生效"。
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
