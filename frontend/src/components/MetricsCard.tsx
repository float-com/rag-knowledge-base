/**
 * 单个指标卡：label + 数值 + 简短说明，详情页指标行复用。
 */

import { Card, Statistic, Tooltip } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'

interface MetricsCardProps {
  label: string
  value: number | null | undefined
  /** 显示精度：默认按比例（0-1）显示两位百分比；passthrough=true 时按原值显示 */
  passthrough?: boolean
  hint?: string
  suffix?: string
}

function formatValue(value: number | null | undefined, passthrough: boolean) {
  if (value === null || value === undefined) return '—'
  if (passthrough) return value.toFixed(2)
  return (value * 100).toFixed(1)
}

function colorOf(value: number | null | undefined, passthrough: boolean) {
  if (value === null || value === undefined) return undefined
  // passthrough（如 avg_latency_ms）不染色
  if (passthrough) return undefined
  if (value >= 0.8) return '#52c41a'
  if (value >= 0.6) return '#faad14'
  return '#ff4d4f'
}

export function MetricsCard({ label, value, passthrough = false, hint, suffix }: MetricsCardProps) {
  const display = formatValue(value, passthrough)
  return (
    <Card size="small" variant="outlined">
      <Statistic
        title={
          <span>
            {label}
            {hint && (
              <Tooltip title={hint}>
                <QuestionCircleOutlined style={{ marginLeft: 6, color: '#8c8c8c' }} />
              </Tooltip>
            )}
          </span>
        }
        value={display}
        suffix={value === null || value === undefined ? '' : suffix ?? (passthrough ? '' : '%')}
        valueStyle={{ color: colorOf(value, passthrough) }}
      />
    </Card>
  )
}
