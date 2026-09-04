/**
 * 登录页：独立于 BasicLayout，无侧栏 / 顶栏。
 *
 * 登录成功后回到 ?back= 指定的路径；缺失时回 /。
 * 默认账号 admin/admin 在登录页底部展示。
 */

import { useState } from 'react'
import { App as AntdApp, Button, Card, Form, Input, Typography } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { login } from '@/api/auth'
import { useAuthStore } from '@/stores/authStore'

interface LoginFormValues {
  username: string
  password: string
}

export function LoginPage() {
  const { message } = AntdApp.useApp()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [submitting, setSubmitting] = useState(false)

  async function onFinish(values: LoginFormValues) {
    setSubmitting(true)
    try {
      const result = await login(values.username, values.password)
      setAuth(result.token, result.user)
      message.success(`欢迎回来，${result.user.displayName}`)
      navigate(searchParams.get('back') || '/', { replace: true })
    } catch {
      // 拦截器已经弹过 message.error，这里只兜异常防止抛到 UI
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        background:
          'linear-gradient(135deg, #f5f7fb 0%, #e6efff 100%)',
        padding: 24,
      }}
    >
      <Card style={{ width: 360 }} variant="borderless">
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 24 }}>
          RAG 知识库
        </Typography.Title>
        <Form<LoginFormValues>
          layout="vertical"
          onFinish={onFinish}
          initialValues={{ username: '', password: '' }}
          autoComplete="off"
        >
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" autoFocus />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 8 }}>
            <Button type="primary" htmlType="submit" loading={submitting} block>
              登录
            </Button>
          </Form.Item>
        </Form>
        <Typography.Paragraph
          type="secondary"
          style={{ marginTop: 8, marginBottom: 0, fontSize: 12, textAlign: 'center' }}
        >
          首次部署默认账号 admin / admin，登录后请尽快修改
        </Typography.Paragraph>
      </Card>
    </div>
  )
}
