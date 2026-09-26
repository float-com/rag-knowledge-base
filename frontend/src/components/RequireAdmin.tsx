/**
 * admin 角色守卫。
 *
 * 套在 RequireAuth 内层使用：进入此组件时 token 必然存在，
 * 但仍需校验 isAdmin（普通用户直接访问 /users 等路径应被弹回首页）。
 *
 * 【第 11 期恢复说明】
 * 本组件曾在 Day10 被临时改成直通（`return <Outlet />`），
 * 原因是那时后端 `/api/evaluations/*` 还没加鉴权，而路由层却把评测页
 * 和 users / roles 一起圈成了 admin only，导致普通账号点「评测分析」403。
 * 现在第 8 章已给评测路由整组挂上 `Depends(get_current_admin)`，
 * 前后端口径一致，因此恢复真实守卫。
 */

import { Result } from 'antd'
import { Outlet } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'

export function RequireAdmin() {
  const user = useAuthStore((s) => s.user)
  if (!user?.isAdmin) {
    return <Result status="403" title="403" subTitle="此页面仅管理员可访问" />
  }
  return <Outlet />
}
