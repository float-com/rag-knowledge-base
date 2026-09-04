/**
 * 用户管理页（admin）：列表 + 新建 + 改密 / 改状态 / 改角色 + 删除。
 */

import { useState } from 'react'
import {
  App as AntdApp,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import type { RoleRead, UserCreate, UserRead } from '@/client/types.gen'
import {
  useAssignRolesMutation,
  useCreateUserMutation,
  useDeleteUserMutation,
  useUpdateUserMutation,
  useUsers,
} from '@/api/users'
import { useRoles } from '@/api/roles'
import { useAuthStore } from '@/stores/authStore'

interface CreateFormValues {
  username: string
  password: string
  display_name: string
  role_ids: string[]
}

interface EditFormValues {
  display_name: string
  status: 'active' | 'disabled'
  password?: string
  role_ids: string[]
}

export function UsersPage() {
  const { message } = AntdApp.useApp()
  const currentUserId = useAuthStore((s) => s.user?.id)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const { data, isLoading } = useUsers(page, pageSize)
  const { data: roles } = useRoles()
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<UserRead | null>(null)

  const deleteMutation = useDeleteUserMutation()

  const roleOptions =
    roles?.map((r: RoleRead) => ({ label: r.name, value: r.id })) ?? []

  const columns = [
    { title: '用户名（账号）', dataIndex: 'username', key: 'username' },
    { title: '昵称', dataIndex: 'display_name', key: 'display_name' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: string) =>
        s === 'active' ? <Tag color="green">启用</Tag> : <Tag>已禁用</Tag>,
    },
    {
      title: '角色',
      dataIndex: 'roles',
      key: 'roles',
      render: (rs: RoleRead[]) => (
        <Space size={4} wrap>
          {rs.length === 0 ? (
            <Tag>无</Tag>
          ) : (
            rs.map((r) => (
              <Tag color={r.name === 'admin' ? 'gold' : 'blue'} key={r.id}>
                {r.name}
              </Tag>
            ))
          )}
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: unknown, record: UserRead) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => setEditTarget(record)}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该用户？"
            okType="danger"
            disabled={record.id === currentUserId}
            onConfirm={async () => {
              try {
                await deleteMutation.mutateAsync(record.id)
                message.success('已删除')
              } catch {
                // 拦截器已处理
              }
            }}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={record.id === currentUserId}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="用户管理"
      extra={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreateOpen(true)}
        >
          新建用户
        </Button>
      }
    >
      <Table<UserRead>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
      />

      <CreateUserModal
        open={createOpen}
        roleOptions={roleOptions}
        onClose={() => setCreateOpen(false)}
      />
      <EditUserModal
        target={editTarget}
        roleOptions={roleOptions}
        onClose={() => setEditTarget(null)}
      />
    </Card>
  )
}

function CreateUserModal({
  open,
  onClose,
  roleOptions,
}: {
  open: boolean
  onClose: () => void
  roleOptions: { label: string; value: string }[]
}) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm<CreateFormValues>()
  const mutation = useCreateUserMutation()

  async function onFinish(values: CreateFormValues) {
    try {
      const payload: UserCreate = {
        username: values.username,
        password: values.password,
        display_name: values.display_name,
        role_ids: values.role_ids,
      }
      await mutation.mutateAsync(payload)
      message.success('已创建用户')
      form.resetFields()
      onClose()
    } catch {
      // 拦截器处理
    }
  }

  return (
    <Modal
      title="新建用户"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={mutation.isPending}
      destroyOnHidden
    >
      <Form<CreateFormValues>
        form={form}
        layout="vertical"
        onFinish={onFinish}
        initialValues={{ role_ids: [] }}
      >
        <Form.Item
          name="username"
          label="用户名（账号）"
          rules={[{ required: true, min: 2 }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="password"
          label="初始密码"
          rules={[{ required: true, min: 4 }]}
        >
          <Input.Password />
        </Form.Item>
        <Form.Item
          name="display_name"
          label="昵称"
          rules={[{ required: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="role_ids" label="角色">
          <Select mode="multiple" options={roleOptions} placeholder="可多选" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function EditUserModal({
  target,
  onClose,
  roleOptions,
}: {
  target: UserRead | null
  onClose: () => void
  roleOptions: { label: string; value: string }[]
}) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm<EditFormValues>()
  const updateMutation = useUpdateUserMutation(target?.id ?? '')
  const assignMutation = useAssignRolesMutation(target?.id ?? '')

  const open = target !== null
  if (target && form.getFieldValue('display_name') === undefined) {
    form.setFieldsValue({
      display_name: target.display_name,
      status: target.status,
      password: '',
      role_ids: (target.roles ?? []).map((r) => r.id),
    })
  }

  async function onFinish(values: EditFormValues) {
    if (!target) return
    try {
      // 1) 基础字段；password 留空时不更新
      await updateMutation.mutateAsync({
        display_name: values.display_name,
        status: values.status,
        password: values.password ? values.password : null,
      })
      // 2) 角色单独 PUT，避免和 password 校验干扰
      await assignMutation.mutateAsync({ role_ids: values.role_ids })
      message.success('已保存')
      form.resetFields()
      onClose()
    } catch {
      // 拦截器处理
    }
  }

  return (
    <Modal
      title={target ? `编辑用户 - ${target.username}` : '编辑用户'}
      open={open}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      onOk={() => form.submit()}
      confirmLoading={updateMutation.isPending || assignMutation.isPending}
      destroyOnHidden
    >
      <Form<EditFormValues> form={form} layout="vertical" onFinish={onFinish}>
        <Form.Item
          name="display_name"
          label="昵称"
          rules={[{ required: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { label: '启用', value: 'active' },
              { label: '禁用', value: 'disabled' },
            ]}
          />
        </Form.Item>
        <Form.Item
          name="password"
          label="重置密码（留空不修改）"
          rules={[
            { min: 4, message: '密码至少 4 位' },
          ]}
        >
          <Input.Password placeholder="留空则不修改" />
        </Form.Item>
        <Form.Item name="role_ids" label="角色">
          <Select mode="multiple" options={roleOptions} placeholder="可多选" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
