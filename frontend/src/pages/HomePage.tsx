import { Card, Col, Row, Tag, Typography, Spin } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { healthApp, healthCos, healthDb } from '@/client/sdk.gen'
import type { HealthStatus } from '@/client/types.gen'

const { Title, Paragraph, Text } = Typography

interface CheckCardProps {
  title: string
  description: string
  data: HealthStatus | undefined
  isLoading: boolean
}

function statusColor(status: HealthStatus['status'] | undefined): string {
  if (status === 'ok') return 'green'
  if (status === 'not_configured') return 'orange'
  return 'red'
}

function statusLabel(status: HealthStatus['status'] | undefined): string {
  if (status === 'ok') return '正常'
  if (status === 'not_configured') return '未配置'
  if (status === 'error') return '异常'
  return '未知'
}

function CheckCard({ title, description, data, isLoading }: CheckCardProps) {
  return (
    <Card title={title} bordered>
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        {description}
      </Paragraph>
      {isLoading ? (
        <Spin />
      ) : (
        <>
          <Tag color={statusColor(data?.status)} style={{ fontSize: 14 }}>
            {statusLabel(data?.status)}
          </Tag>
          {data?.detail ? (
            <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
              <Text type="secondary">{data.detail}</Text>
            </Paragraph>
          ) : null}
        </>
      )}
    </Card>
  )
}

export function HomePage() {
  // client.ts 已开 throwOnError，非 2xx 会直接抛错被 react-query 捕获
  const appQuery = useQuery({
    queryKey: ['health', 'app'],
    queryFn: async () => (await healthApp()).data,
  })
  const dbQuery = useQuery({
    queryKey: ['health', 'db'],
    queryFn: async () => (await healthDb()).data,
  })
  const cosQuery = useQuery({
    queryKey: ['health', 'cos'],
    queryFn: async () => (await healthCos()).data,
  })

  return (
    <div>
      <Title level={3}>第 2 章 · 健康检查</Title>
      <Paragraph type="secondary">
        三个状态卡片均显示"正常"即代表前后端、数据库、COS 全链路打通。
      </Paragraph>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <CheckCard
            title="后端服务"
            description="GET /api/health"
            data={appQuery.data}
            isLoading={appQuery.isLoading}
          />
        </Col>
        <Col xs={24} md={8}>
          <CheckCard
            title="PostgreSQL"
            description="GET /api/health/db"
            data={dbQuery.data}
            isLoading={dbQuery.isLoading}
          />
        </Col>
        <Col xs={24} md={8}>
          <CheckCard
            title="腾讯云 COS"
            description="GET /api/health/cos"
            data={cosQuery.data}
            isLoading={cosQuery.isLoading}
          />
        </Col>
      </Row>
    </div>
  )
}
