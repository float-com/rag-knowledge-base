/**
 * 全局 HTTP client 初始化。
 *
 * 由 main.tsx 顶部 `import '@/api/client'` 触发执行：
 * - 请求拦截器：把 token 注入 Authorization header
 * - 响应拦截器：401 → 清登录态 + 跳 /login；其余非 2xx 弹 message.error
 *
 * 业务代码无需感知此文件，直接从 @/client/sdk.gen 导入生成的 SDK 函数即可。
 */

import { message } from 'antd'
import { client } from '@/client/client.gen'
import { getAuthToken, useAuthStore } from '@/stores/authStore'
import { formatApiError } from '@/utils/errors'

client.setConfig({
  baseUrl: '',
  // 全局开启抛错：非 2xx 直接抛异常，业务代码可以 try/catch
  // 或交给 react-query 的 isError，不必到处解构 { data, error }
  throwOnError: true,
})

client.interceptors.request.use((request) => {
  // 登录 / 健康检查这类公共接口不应该带 token；如果没有就跳过
  const token = getAuthToken()
  if (token && !request.headers.has('Authorization')) {
    request.headers.set('Authorization', `Bearer ${token}`)
  }
  return request
})

// 让 /login 跳转只发生一次，避免拦截到多个并发 401 后疯狂 replace
let redirectingToLogin = false

client.interceptors.response.use(async (response) => {
  if (response.status === 401) {
    useAuthStore.getState().logout()
    if (!redirectingToLogin && window.location.pathname !== '/login') {
      redirectingToLogin = true
      const back = window.location.pathname + window.location.search
      window.location.replace(`/login?back=${encodeURIComponent(back)}`)
    }
    // 在登录页时展示后端返回的错误（如"用户名或密码错误"）
    if (window.location.pathname === '/login') {
      message.error(await formatApiError(response))
    }
    return response
  }
  if (!response.ok) {
    message.error(await formatApiError(response))
  }
  return response
})
