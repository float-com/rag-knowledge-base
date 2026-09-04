/**
 * SSE 问答流式客户端封装。
 *
 * 用 @microsoft/fetch-event-source 而非原生 EventSource 的原因：
 * - 原生 EventSource 不支持 POST + JSON body，而我们的 SSE 入口是 POST
 * - 需要手动取消（用户切换页面 / 点"中止"）
 * - 需要带 Authorization header；EventSource 也不支持自定义 header
 */

import { fetchEventSource } from '@microsoft/fetch-event-source'
import type { AgentStep, CitationRead, QueryRouteRead } from '@/client/types.gen'
import { getAuthToken, useAuthStore } from '@/stores/authStore'

export interface ChatStartEvent {
  type: 'start'
  /** LangSmith trace_id；未启用观测时为 null */
  traceId: string | null
  /** LangSmith UI 跳转 URL；后端按 LANGSMITH_RUN_URL_PREFIX 拼好下发，未配置为 null */
  traceUrl: string | null
  /**语义缓存：true 表示本次回答来自缓存命中，跳过了图与 LLM */
  cacheHit: boolean
}
export interface ChatQueryRouteEvent {
  type: 'query_route'
  queryRoute: QueryRouteRead
}
export interface ChatAgentStepsEvent {
  type: 'agent_steps'
  steps: AgentStep[]
}
export interface ChatCitationsEvent {
  type: 'citations'
  citations: CitationRead[]
}
export interface ChatTokenEvent {
  type: 'token'
  delta: string
}
export interface ChatVerifyResultEvent {
  type: 'verify_result'
  verified: boolean
  reason: string | null
  /**
   * verified=false 时后端给出的替换文本（统一拒答文案）。
   * 前端按它整段覆盖流式出来的 answer，与 PRD"校验失败 → 拒答替换"对齐。
   */
  replacementAnswer: string | null
}
export interface ChatEndEvent {
  type: 'end'
  message_id: string
  refused: boolean
}
export interface ChatErrorEvent {
  type: 'error'
  code: string
  message: string
}

export type ChatStreamEvent =
  | ChatStartEvent
  | ChatQueryRouteEvent
  | ChatAgentStepsEvent
  | ChatCitationsEvent
  | ChatTokenEvent
  | ChatVerifyResultEvent
  | ChatEndEvent
  | ChatErrorEvent

interface StreamChatParams {
  conversationId: string
  question: string
  signal?: AbortSignal
  onEvent: (event: ChatStreamEvent) => void
}

class FatalSseError extends Error {}

/** 发起 SSE 问答请求；resolve 时代表流已正常结束。 */
export async function streamChat({
  conversationId,
  question,
  signal,
  onEvent,
}: StreamChatParams): Promise<void> {
  const token = getAuthToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }
  await fetchEventSource(
    `/api/conversations/${conversationId}/chat`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({ question }),
      signal,
      // 默认会在 tab 切换到后台时关闭连接，问答场景不希望中断
      openWhenHidden: true,
      async onopen(response) {
        // 401 时与全局 HTTP 拦截器对齐：清登录态 + 跳登录页
        if (response.status === 401) {
          useAuthStore.getState().logout()
          if (window.location.pathname !== '/login') {
            const back = window.location.pathname + window.location.search
            window.location.replace(`/login?back=${encodeURIComponent(back)}`)
          }
          throw new FatalSseError('请先登录')
        }
        if (response.ok && response.headers.get('content-type')?.includes('text/event-stream')) {
          return
        }
        const text = await response.text().catch(() => '')
        throw new FatalSseError(text || `HTTP ${response.status}`)
      },
      onmessage(msg) {
        if (!msg.event) return
        const data = msg.data ? JSON.parse(msg.data) : {}
        switch (msg.event) {
          case 'message_start':
            onEvent({
              type: 'start',
              traceId: data.trace_id ?? null,
              traceUrl: data.trace_url ?? null,
              cacheHit: Boolean(data.cache_hit),
            })
            break
          case 'query_route':
            onEvent({ type: 'query_route', queryRoute: data as QueryRouteRead })
            break
          case 'agent_steps':
            onEvent({ type: 'agent_steps', steps: (data.steps ?? []) as AgentStep[] })
            break
          case 'citations':
            onEvent({ type: 'citations', citations: data.citations ?? [] })
            break
          case 'token':
            onEvent({ type: 'token', delta: data.delta ?? '' })
            break
          case 'verify_result':
            onEvent({
              type: 'verify_result',
              verified: Boolean(data.verified),
              reason: data.reason ?? null,
              replacementAnswer: data.replacement_answer ?? null,
            })
            break
          case 'message_end':
            onEvent({
              type: 'end',
              message_id: data.message_id,
              refused: Boolean(data.refused),
            })
            break
          case 'error':
            onEvent({ type: 'error', code: data.code ?? 'error', message: data.message ?? '请求失败' })
            break
        }
      },
      onclose() {
        // 服务端正常关闭流；不抛错让上层走 finally 收尾
      },
      onerror(err) {
        // 抛出后 fetchEventSource 会停止重连，由上层 catch 处理
        throw err
      },
    },
  )
}
