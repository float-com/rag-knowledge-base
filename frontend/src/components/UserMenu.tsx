/**
 * 顶栏右上角用户菜单：头像 + 昵称 + 角色 Tag + 「退出」。
 *
 * 角色 Tag 标识当前身份；非管理员无管理菜单项。
 */

import { useQueryClient } from '@tanstack/react-query'
import { App as AntdApp, Avatar, Dropdown, Space, Tag, Typography } from 'antd'
import { LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'

export function UserMenu() {
  const { message } = AntdApp.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  if (!user) return null

  const handleLogout = () => {
    logout()
    queryClient.clear()
    message.success('已退出登录')
    navigate('/login', { replace: true })
  }

  return (
    <Dropdown
      menu={{
        items: [
          {
            key: 'logout',
            icon: <LogoutOutlined />,
            label: '退出登录',
            onClick: handleLogout,
          },
        ],
      }}
      placement="bottomRight"
    >
      <Space style={{ cursor: 'pointer' }}>
        <Avatar size="small" icon={<UserOutlined />} />
        <Typography.Text>{user.displayName}</Typography.Text>
        {user.isAdmin ? (
          <Tag color="gold">管理员</Tag>
        ) : (
          <Tag>用户</Tag>
        )}
      </Space>
    </Dropdown>
  )
}
