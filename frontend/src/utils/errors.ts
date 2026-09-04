/**
 * 后端约定的统一错误响应格式（{code, message}）。
 *
 * FastAPI 默认错误体是 {detail: ...}，我们的 error_handlers 改成了 {code, message}，
 * OpenAPI schema 没有显式描述这个结构，所以这里手写。
 */
export interface ApiError {
  code: string
  message: string
}

/** 把 fetch Response 统一转成可读错误文案。 */
export async function formatApiError(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as Partial<ApiError>
    if (body?.message) return body.message
  } catch {
    // 非 JSON 响应（如网关 502 HTML），回退到状态码文案
  }
  return `${response.status} ${response.statusText || '请求失败'}`
}
