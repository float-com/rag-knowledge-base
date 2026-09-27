/**
 * 后端约定的统一错误响应格式（{code, message}）。
 *
 * FastAPI 默认错误体是 {detail: ...}，我们的 error_handlers 改成了 {code, message}，
 * OpenAPI schema 没有显式描述这个结构，所以这里手写。
 *
 * 【本模块的职责边界】只做"把错误变成人话"，不发请求、不碰 UI。
 */
export interface ApiError {
  code: string
  message: string
}

/**
 * 把一段文本尝试解析成后端的 {code, message}；不是这个形状就返回 null。
 *
 * 【为什么需要它 —— 本项目真实踩过的坑】
 * 有些 HTTP 客户端（比如我们自己写的 SSE 客户端）在响应非 2xx 时，
 * 会把【响应体原文】直接塞进 Error.message 抛出来。
 * 后端那句 `{"code":"rate_limited","message":"请求过于频繁，…"}`
 * 就会原样出现在界面上 —— 用户看到的是一串 JSON，而不是一句人话。
 *
 * 这个函数就是那层"把原文还原成人话"的转换。
 */
export function parseApiErrorText(text: string): ApiError | null {
  if (!text) return null
  const trimmed = text.trim()
  // 只尝试解析"看起来像 JSON 对象"的文本，避免对每一段普通文本做无谓的 JSON.parse
  if (!trimmed.startsWith('{')) return null
  try {
    const body = JSON.parse(trimmed) as Partial<ApiError>
    if (body && typeof body.message === 'string' && body.message) {
      return { code: typeof body.code === 'string' ? body.code : '', message: body.message }
    }
  } catch {
    // 不是合法 JSON：按"解析失败"处理，交给调用方走兜底
  }
  return null
}

/** 把 fetch Response 统一转成可读错误文案。 */
export async function formatApiError(response: Response): Promise<string> {
  const text = await response
    .clone()
    .text()
    .catch(() => '')
  const parsed = parseApiErrorText(text)
  if (parsed) return parsed.message
  // 非 JSON 响应（如网关 502 HTML）不要原样展示，回退到状态码文案
  return `${response.status} ${response.statusText || '请求失败'}`
}

/**
 * 把【任意异常】转成可读文案 + 错误码。这是界面层最该用的那个入口。
 *
 * 同时处理三种形态：
 *   · Response                                        → 读 body 取 message
 *   · 带 code 的自定义错误（如 ApiStreamError）        → 直接用它的 message + code
 *   · 其它（含"message 里塞了一整串错误体 JSON"的）    → 尝试解析，再不行给通用文案
 *
 * 【为什么不能只判断 `err instanceof Response`】
 * 聊天走的 SSE 客户端抛的是普通 Error（不是 Response），
 * 所以 `err instanceof Response ? formatApiError(err) : err.message`
 * 那条分支永远走不到，兜底就把整串 JSON 渲染了出来。
 * 把三种形态都收在这一个函数里，界面层就不会再漏。
 */
export async function toReadableError(
  err: unknown,
): Promise<{ code: string; message: string }> {
  if (err instanceof Response) {
    const text = await err
      .clone()
      .text()
      .catch(() => '')
    const parsed = parseApiErrorText(text)
    if (parsed) return parsed
    return { code: '', message: `${err.status} ${err.statusText || '请求失败'}` }
  }

  const candidate = err as { code?: unknown; message?: unknown } | null
  const rawCode = typeof candidate?.code === 'string' ? candidate.code : ''
  const rawMessage =
    typeof candidate?.message === 'string'
      ? candidate.message
      : typeof err === 'string'
        ? err
        : ''

  // 兜底再解析一次：覆盖"message 里塞了整串错误体 JSON"的情况
  const parsed = parseApiErrorText(rawMessage)
  if (parsed) return { code: parsed.code || rawCode, message: parsed.message }

  // 有些网关/反向代理在 5xx 时会直接返回一整页 HTML。
  // 这种东西既不是写给用户看的，原样塞进气泡还会把界面撑乱 —— 换成通用文案。
  if (/^\s*</.test(rawMessage)) {
    return { code: rawCode, message: '服务暂时不可用，请稍后重试' }
  }

  return { code: rawCode, message: rawMessage.trim() || '请求失败，请稍后重试' }
}
