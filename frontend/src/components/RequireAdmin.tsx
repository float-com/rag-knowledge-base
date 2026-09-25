/**
 * admin 角色守卫。
 *
 * 套在 RequireAuth 内层使用：进入此组件时 token 必然存在，
 * 但仍需校验 isAdmin（普通用户直接访问 /users 等路径应被弹回首页）。
 */

// ⚠️【临时直通期间一并注释掉，否则 tsc 报未使用变量】恢复守卫时解开即可。
// import { Result } from 'antd'
// import { useAuthStore } from '@/stores/authStore'
import { Outlet } from 'react-router-dom'

export function RequireAdmin() {
  // ===========================================================================
  // ⚠️【临时直通 · 待恢复】超前守卫，为跑通 Day10 评测控制台先停用
  // ---------------------------------------------------------------------------
  // 停用原因：后端 `/api/evaluations/*` 第 7 步并未加任何鉴权（无 Depends / 无 admin 守卫），
  //          但本组件在路由层把 evaluation 两条路由和 users / roles 一起圈成了 admin only，
  //          导致普通账号点「评测分析」直接 403，评测链路无法验证。
  //
  // 【恢复方法】删掉下面那行 `return <Outlet />` 并解开上面整段注释即可，一行都不用重写。
  //          等后续章节讲权限体系（RBAC 落地）时恢复。
  //
  // 原实现（保留备查）：
  //   const user = useAuthStore((s) => s.user)
  //   if (!user?.isAdmin) {
  //     return <Result status="403" title="403" subTitle="此页面仅管理员可访问" />
  //   }
  // ===========================================================================
  return <Outlet />
}
