/**
 * admin 角色守卫。
 *
 * 套在 RequireAuth 内层使用：进入此组件时 token 必然存在，
 * 但仍需校验 isAdmin（普通用户直接访问 /users 等路径应被弹回首页）。
 */

import { Result } from 'antd'
import { Outlet } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'

export function RequireAdmin() {
  const user = useAuthStore((s) => s.user)
  if (!user?.isAdmin) {
    return (
      <Result
        status="403"
        title="403"
        subTitle="此页面仅管理员可访问"
      />
    )
  }
  return <Outlet />
}
