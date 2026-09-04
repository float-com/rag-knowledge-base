/**
 * 角色管理页（admin）：列表 + 新建 + 编辑 + 删除。
 *
 * 内置角色 admin / user 不允许删除，由后端 RoleService 兜底；前端按 name 隐藏删除按钮以减少误触。
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
  Space,
  Table,
  Tag,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import type { RoleRead } from '@/client/types.gen'
import {
  useCreateRoleMutation,
  useDeleteRoleMutation,
  useRoles,
  useUpdateRoleMutation,
} from '@/api/roles'
import { PermissionTagsField } from '@/components/PermissionTagsField'

const PROTECTED = new Set(['admin', 'user'])

interface FormValues {
  name: string
  description: string
  permission_tags: string[]
}

export function RolesPage() {
  const { message } = AntdApp.useApp()
  const { data: roles, isLoading } = useRoles()
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<RoleRead | null>(null)
  const deleteMutation = useDeleteRoleMutation()

  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '描述', dataIndex: 'description', key: 'description' },
    {
      title: '权限标签',
      dataIndex: 'permission_tags',
      key: 'permission_tags',
      render: (tags: string[]) => (
        <Space size={4} wrap>
          {tags.length === 0 ? (
            <Tag>无</Tag>
          ) : (
            tags.map((t) => (
              <Tag color={t === '*' ? 'gold' : 'blue'} key={t}>
                {t}
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
      render: (_: unknown, record: RoleRead) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => setEditTarget(record)}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该角色？"
            okType="danger"
            disabled={PROTECTED.has(record.name)}
            onConfirm={async () => {
              try {
                await deleteMutation.mutateAsync(record.id)
                message.success('已删除')
              } catch {
                // 拦截器处理
              }
            }}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={PROTECTED.has(record.name)}
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
      title="角色管理"
      extra={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreateOpen(true)}
        >
          新建角色
        </Button>
      }
    >
      <Table<RoleRead>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={roles ?? []}
        pagination={false}
      />

      <CreateRoleModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
      />
      <EditRoleModal
        target={editTarget}
        onClose={() => setEditTarget(null)}
      />
    </Card>
  )
}

function CreateRoleModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm<FormValues>()
  const mutation = useCreateRoleMutation()

  async function onFinish(values: FormValues) {
    try {
      await mutation.mutateAsync({
        name: values.name,
        description: values.description ?? '',
        permission_tags: values.permission_tags ?? [],
      })
      message.success('已创建')
      form.resetFields()
      onClose()
    } catch {
      // 拦截器处理
    }
  }

  return (
    <Modal
      title="新建角色"
      open={open}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      onOk={() => form.submit()}
      confirmLoading={mutation.isPending}
      destroyOnHidden
    >
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={onFinish}
        initialValues={{ name: '', description: '', permission_tags: [] }}
      >
        <Form.Item
          name="name"
          label="名称"
          rules={[{ required: true }]}
          extra="内置角色 admin / user 已存在；推荐用业务标签（hr / sales 等）"
        >
          <Input />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input />
        </Form.Item>
        <Form.Item
          name="permission_tags"
          label="权限标签"
          extra={'特殊值 "*" 表示通配（admin 专用）；空数组表示无任何业务权限'}
        >
          <PermissionTagsField />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function EditRoleModal({
  target,
  onClose,
}: {
  target: RoleRead | null
  onClose: () => void
}) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm<FormValues>()
  const mutation = useUpdateRoleMutation(target?.id ?? '')

  const open = target !== null
  if (target && form.getFieldValue('name') === undefined) {
    form.setFieldsValue({
      name: target.name,
      description: target.description,
      permission_tags: target.permission_tags ?? [],
    })
  }

  async function onFinish(values: FormValues) {
    if (!target) return
    try {
      await mutation.mutateAsync({
        description: values.description ?? '',
        permission_tags: values.permission_tags ?? [],
      })
      message.success('已保存')
      form.resetFields()
      onClose()
    } catch {
      // 拦截器处理
    }
  }

  return (
    <Modal
      title={target ? `编辑角色 - ${target.name}` : '编辑角色'}
      open={open}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      onOk={() => form.submit()}
      confirmLoading={mutation.isPending}
      destroyOnHidden
    >
      <Form<FormValues> form={form} layout="vertical" onFinish={onFinish}>
        <Form.Item name="name" label="名称（不可改）">
          <Input disabled />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input />
        </Form.Item>
        <Form.Item name="permission_tags" label="权限标签">
          <PermissionTagsField />
        </Form.Item>
      </Form>
    </Modal>
  )
}
