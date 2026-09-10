import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Button,
  Form,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd'
import type { TableProps, UploadProps, UploadFile } from 'antd'
import {
  DeleteOutlined,
  InboxOutlined,
  ReloadOutlined,
  RedoOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  deleteDocument,
  listDocuments,
  retryDocument,
  uploadDocument,
} from '@/client/sdk.gen'
import type { DocumentRead } from '@/client/types.gen'
import { PermissionTagsField } from '@/components/PermissionTagsField'
import {
  getStatusColor,
  getStatusLabel,
  isTerminalStatus,
} from '@/utils/documentStatus'
//import { useAuthStore } from '@/stores/authStore'

const { Title, Paragraph } = Typography

const ACCEPTED = '.pdf,.docx,.md,.markdown,.html,.htm'

type StatusFilter = DocumentRead['status'] | 'all'

const STATUS_OPTIONS: { label: string; value: StatusFilter }[] = [
  { label: '全部状态', value: 'all' },
  { label: '上传中', value: 'uploading' },
  { label: '解析中', value: 'parsing' },
  { label: '索引中', value: 'indexing' },
  { label: '已就绪', value: 'ready' },
  { label: '失败', value: 'failed' },
]

// 与后端 DocumentService._DELETABLE_STATUSES 同步
const DELETABLE_STATUSES: ReadonlySet<DocumentRead['status']> = new Set([
  'ready',
  'failed',
  'uploading',
])

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}

interface UploadFormValues {
  files: UploadFile[]
  permission_tags: string[]
}

export function DocumentsPage() {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [uploadOpen, setUploadOpen] = useState(false)
  const queryClient = useQueryClient()
  //const isAdmin = useAuthStore((s) => Boolean(s.user?.isAdmin))
  const isAdmin = true // 临时 Mock 管理员权限，放行页面管理功能，方便本地接口联调

  const listQuery = useQuery({
    queryKey: ['documents', page, pageSize, statusFilter],
    queryFn: async () => {
      const res = await listDocuments({
        query: {
          page,
          page_size: pageSize,
          status: statusFilter === 'all' ? undefined : statusFilter,
        },
      })
      return res.data!
    },
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      const hasInflight = data.items.some((d) => !isTerminalStatus(d.status))
      return hasInflight ? 3000 : false
    },
  })

  const invalidateList = () =>
    queryClient.invalidateQueries({ queryKey: ['documents'] })

  const uploadMutation = useMutation({
    mutationFn: async ({ file, tags }: { file: File; tags: string[] }) => {
      const res = await uploadDocument({
        body: {
          file,
          permission_tags: tags.length > 0 ? JSON.stringify(tags) : null,
        },
      })
      return res.data!
    },
    onSuccess: (doc) => {
      message.success(`${doc.name} 已提交，后台处理中`)
      invalidateList()
    },
  })

  const retryMutation = useMutation({
    mutationFn: async (id: string) => {
      const res = await retryDocument({ path: { document_id: id } })
      return res.data!
    },
    onSuccess: () => {
      message.success('已重新提交解析')
      invalidateList()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => {
      await deleteDocument({ path: { document_id: id } })
      return id
    },
    onSuccess: () => {
      message.success('文档已删除')
      invalidateList()
    },
  })

  const columns: TableProps<DocumentRead>['columns'] = [
    {
      title: '文档名',
      dataIndex: 'name',
      ellipsis: true,
      render: (name: string, record) => <Link to={`/documents/${record.id}`}>{name}</Link>,
    },
    {
      title: '类型',
      dataIndex: 'mime_type',
      width: 220,
      ellipsis: true,
    },
    {
      title: '大小',
      dataIndex: 'size',
      width: 110,
      render: formatSize,
    },
    {
      title: '权限标签',
      dataIndex: 'permission_tags',
      width: 200,
      // 原逻辑保留：后端完整返回 permission_tags 后可恢复
      // render: (tags: string[]) =>
      //   tags.length === 0 ? (
      //     <Tag>公开</Tag>
      //   ) : (
      //     <Space size={4} wrap>
      //       {tags.map((t) => (
      //         <Tag color={t === "*" ? "gold" : "blue"} key={t}>
      //           {t}
      //         </Tag>
      //       ))}
      //     </Space>
      //   ),
      render: (tags: string[] = []) =>
        tags.length === 0 ? (
          <Tag>公开</Tag>
        ) : (
          <Space size={4} wrap>
            {tags.map((t) => (
              <Tag color={t === '*' ? 'gold' : 'blue'} key={t}>
                {t}
              </Tag>
            ))}
          </Space>
        ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (status: DocumentRead['status']) => (
        <Tag color={getStatusColor(status)}>{getStatusLabel(status)}</Tag>
      ),
    },
    {
      title: '版本',
      dataIndex: 'version',
      width: 80,
      render: (version: number) => <Tag color="purple">v{version}</Tag>,
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      width: 200,
      render: (value: string) => new Date(value).toLocaleString('zh-CN'),
    },
  ]

  if (isAdmin) {
    columns.push({
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_, record) => {
        const canDelete = DELETABLE_STATUSES.has(record.status)
        const canRetry = record.status === 'failed'
        return (
          <Space>
            {canRetry ? (
              <Button
                type="link"
                size="small"
                icon={<RedoOutlined />}
                loading={retryMutation.isPending && retryMutation.variables === record.id}
                onClick={() => retryMutation.mutate(record.id)}
              >
                重试
              </Button>
            ) : null}
            <Tooltip title={canDelete ? '' : '文档处理中，无法删除'}>
              <Popconfirm
                title="确认删除该文档？"
                description="将同时删除文档内容、所有切片以及云端原文件，无法恢复。"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                disabled={!canDelete}
                onConfirm={() => deleteMutation.mutate(record.id)}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={!canDelete}
                  loading={
                    deleteMutation.isPending && deleteMutation.variables === record.id
                  }
                >
                  删除
                </Button>
              </Popconfirm>
            </Tooltip>
          </Space>
        )
      },
    })
  }

  return (
    <div>
      <Title level={3}>文档管理</Title>
      <Paragraph type="secondary">
        {isAdmin
          ? '支持 PDF、DOCX、Markdown、HTML。上传时可指定权限标签控制可见范围；留空视为公开。'
          : '展示有权限访问的文档列表。无操作权限请联系管理员。'}
      </Paragraph>

      <Space style={{ marginBottom: 16 }} wrap>
        {isAdmin ? (
          <Button
            type="primary"
            icon={<InboxOutlined />}
            onClick={() => setUploadOpen(true)}
            loading={uploadMutation.isPending}
          >
            上传文档
          </Button>
        ) : null}
        <Button
          icon={<ReloadOutlined />}
          onClick={() => listQuery.refetch()}
          loading={listQuery.isFetching}
        >
          刷新
        </Button>
        <Select<StatusFilter>
          value={statusFilter}
          onChange={(v) => {
            setStatusFilter(v)
            setPage(1)
          }}
          options={STATUS_OPTIONS}
          style={{ width: 140 }}
        />
      </Space>

      <Table<DocumentRead>
        rowKey="id"
        loading={listQuery.isLoading}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: listQuery.data?.total ?? 0,
          showSizeChanger: true,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
      />

      {isAdmin ? (
        <UploadModal
          open={uploadOpen}
          onClose={() => setUploadOpen(false)}
          onUpload={(file, tags) => uploadMutation.mutate({ file, tags })}
        />
      ) : null}
    </div>
  )
}

function UploadModal({
  open,
  onClose,
  onUpload,
}: {
  open: boolean
  onClose: () => void
  onUpload: (file: File, tags: string[]) => void
}) {
  const [form] = Form.useForm<UploadFormValues>()

  const uploadProps: UploadProps = {
    multiple: false,
    accept: ACCEPTED,
    maxCount: 1,
    beforeUpload: () => false, // 用 form 控制提交
  }

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      const file = values.files?.[0]?.originFileObj as File | undefined
      if (!file) {
        message.error('请先选择文件')
        return
      }
      onUpload(file, values.permission_tags ?? [])
      form.resetFields()
      onClose()
    } catch {
      // form 自身的校验错误会高亮，不需要 message
    }
  }

  return (
    <Modal
      title="上传文档"
      open={open}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      onOk={handleOk}
      destroyOnHidden
    >
      <Form<UploadFormValues> form={form} layout="vertical">
        <Form.Item
          name="files"
          label="选择文件"
          valuePropName="fileList"
          getValueFromEvent={(e) => (Array.isArray(e) ? e : e?.fileList)}
          rules={[{ required: true, message: '请选择文件' }]}
        >
          <Upload {...uploadProps}>
            <Button icon={<InboxOutlined />}>点击选择文件</Button>
          </Upload>
        </Form.Item>
        <Form.Item
          name="permission_tags"
          label="权限标签"
          extra="留空视为公开（所有登录用户可见）；填入后仅拥有匹配标签的角色可见"
        >
          <PermissionTagsField placeholder='例如：hr、sales、"public"' />
        </Form.Item>
      </Form>
    </Modal>
  )
}
