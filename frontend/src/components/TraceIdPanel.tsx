import { Space, Tag, Tooltip, Typography } from 'antd'

const { Text } = Typography

interface TraceIdPanelProps {
  traceId: string | null | undefined
  traceUrl?: string | null
}

/** 单行 trace 信息条：截断显示 trace_id + 复制按钮 + 可选 LangSmith 跳转链接。
 *
 * 没有 traceId 时整条不渲染（未启用 LangSmith 时 SSE 也不下发 trace_id）。
 * 风格与 QueryRoutePanel / AgentStepsPanel 保持一致：紧贴答案下方，淡色背景。
 */
export function TraceIdPanel({ traceId, traceUrl }: TraceIdPanelProps) {
  if (!traceId) return null
  const short = traceId.length > 8 ? `${traceId.slice(0, 8)}…` : traceId

  return (
    <div
      style={{
        marginTop: 12,
        padding: '6px 10px',
        background: '#fafafa',
        borderRadius: 6,
        fontSize: 12,
      }}
    >
      <Space size={8} wrap>
        <Tag color="default" style={{ marginInlineEnd: 0 }}>
          Trace
        </Tag>
        <Tooltip title={traceId}>
          <Text
            type="secondary"
            copyable={{ text: traceId, tooltips: ['复制 trace_id', '已复制'] }}
            style={{ fontFamily: 'monospace' }}
          >
            {short}
          </Text>
        </Tooltip>
        {traceUrl ? (
          <a href={traceUrl} target="_blank" rel="noreferrer">
            在 LangSmith 中查看 ↗
          </a>
        ) : null}
      </Space>
    </div>
  )
}
