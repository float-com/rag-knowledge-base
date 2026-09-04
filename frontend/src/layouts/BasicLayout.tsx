import { Layout, Menu } from 'antd'
import type { MenuProps } from 'antd'
import {
  DashboardOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  MessageOutlined,
  SafetyOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { UserMenu } from '@/components/UserMenu'
import { useAuthStore } from '@/stores/authStore'

const { Header, Sider, Content } = Layout

const baseMenuItems: NonNullable<MenuProps["items"]> = [
  { key: '/', icon: <DashboardOutlined />, label: <Link to="/">首页</Link> },
  {
    key: '/documents',
    icon: <FileTextOutlined />,
    label: <Link to="/documents">文档管理</Link>,
  },
  {
    key: '/chat',
    icon: <MessageOutlined />,
    label: <Link to="/chat">知识问答</Link>,
  },
]

const adminMenuItems: NonNullable<MenuProps["items"]> = [
  {
    key: '/evaluation',
    icon: <ExperimentOutlined />,
    label: <Link to="/evaluation">评测分析</Link>,
  },
  {
    key: '/users',
    icon: <TeamOutlined />,
    label: <Link to="/users">用户管理</Link>,
  },
  {
    key: '/roles',
    icon: <SafetyOutlined />,
    label: <Link to="/roles">角色管理</Link>,
  },
]

function resolveSelectedKey(pathname: string): string {
  // 子路径保持父项高亮
  if (pathname.startsWith('/documents')) return '/documents'
  if (pathname.startsWith('/chat')) return '/chat'
  if (pathname.startsWith('/evaluation')) return '/evaluation'
  if (pathname.startsWith('/users')) return '/users'
  if (pathname.startsWith('/roles')) return '/roles'
  return '/'
}

export function BasicLayout() {
  const location = useLocation()
  const selectedKey = resolveSelectedKey(location.pathname)
  const isAdmin = useAuthStore((s) => Boolean(s.user?.isAdmin))

  const menuItems = isAdmin
    ? [...baseMenuItems, ...adminMenuItems]
    : baseMenuItems

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider breakpoint="lg" collapsedWidth={64}>
        <div
          style={{
            color: '#fff',
            fontWeight: 600,
            textAlign: 'center',
            padding: '16px 0',
            fontSize: 16,
          }}
        >
          RAG 知识库
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            paddingLeft: 24,
            paddingRight: 24,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: 16,
          }}
        >
          <span>企业级 RAG 知识库</span>
          <UserMenu />
        </Header>
        <Content style={{ margin: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
