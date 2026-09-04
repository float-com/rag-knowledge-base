import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  List,
  Modal,
  Pagination,
  Popconfirm,
  Progress,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  RedoOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { gfmComponents } from '@/components/markdownComponents'
import {
  deleteDocument,
  getDocument,
  getDocumentChunk,
  listDocumentChunks,
  reindexDocument,
  retryDocument,
  updateDocumentPermissionTags,
} from '@/client/sdk.gen'
import type {
  DocumentChunkDetail,
  DocumentChunkRead,
  DocumentRead,
  IngestionTaskRead,
} from '@/client/types.gen'
import { PermissionTagsField } from '@/components/PermissionTagsField'
import {
  getStatusColor,
  getStatusLabel,
  isTerminalStatus,
} from '@/utils/documentStatus'
import {
  buildDocumentFileUrl,
  canPreviewInline,
  isHtmlMime,
  isMarkdownMime,
  isPdfMime,
} from '@/utils/documentFile'
import { useAuthStore } from '@/stores/authStore'

const { Title, Text, Paragraph } = Typography

const CHUNK_PAGE_SIZE = 20

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

function MarkdownPreview({ url }: { url: string }) {
  const [content, setContent] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setContent(null)
    setError(null)
    fetch(url, {
      headers: useAuthStore.getState().token
        ? { Authorization: `Bearer ${useAuthStore.getState().token!}` }
        : undefined,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.text()
      })
      .then((text) => {
        if (!cancelled) setContent(text)
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [url])

  if (error) return <Alert type="error" message="加载 Markdown 失败" description={error} />
  if (content === null) return <Skeleton active />
  return (
    <div style={{ padding: 16, maxHeight: 600, overflow: 'auto' }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={gfmComponents}>
      {content}
    </ReactMarkdown>
    </div>
  )
}

export function DocumentDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isAdmin = useAuthStore((s) => Boolean(s.user?.isAdmin))

  const [chunkPage, setChunkPage] = useState(1)
  const [activeChunkId, setActiveChunkId] = useState<string | null>(null)
  const [tagsModalOpen, setTagsModalOpen] = useState(false)

  const docQuery = useQuery({
    queryKey: ['documents', 'detail', id],
    queryFn: async () => {
      const res = await getDocument({ path: { document_id: id } })
      return res.data!
    },
    enabled: !!id,
    refetchInterval: (q) => {
      const data = q.state.data
      if (!data) return false
      return isTerminalStatus(data.status) ? false : 3000
    },
  })

  const doc = docQuery.data

  const chunksQuery = useQuery({
    queryKey: ['documents', 'detail', id, 'chunks', chunkPage],
    queryFn: async () => {
      const res = await listDocumentChunks({
        path: { document_id: id },
        query: { page: chunkPage, page_size: CHUNK_PAGE_SIZE },
      })
      return res.data!
    },
    enabled: !!id && doc?.status === 'ready',
  })

  const chunkDetailQuery = useQuery({
    queryKey: ['documents', 'detail', id, 'chunk', activeChunkId],
    queryFn: async () => {
      const res = await getDocumentChunk({
        path: { document_id: id, chunk_id: activeChunkId! },
      })
      return res.data!
    },
    enabled: !!activeChunkId,
  })

  const retryMutation = useMutation({
    mutationFn: async () => {
      const res = await retryDocument({ path: { document_id: id } })
      return res.data!
    },
    onSuccess: () => {
      message.success('已重新提交解析')
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })

  const reindexMutation = useMutation({
    mutationFn: async (file: File) => {
      const res = await reindexDocument({
        path: { document_id: id },
        body: { file },
      })
      return res.data!
    },
    onSuccess: () => {
      message.success('已提交重新索引，正在解析与增量更新')
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })

  const reindexInputRef = useRef<HTMLInputElement>(null)
  const onPickReindexFile: React.ChangeEventHandler<HTMLInputElement> = (e) => {
    const file = e.target.files?.[0]
    if (file) {
      reindexMutation.mutate(file)
    }
    // 清空 input，让相同文件名也能再次触发 onChange
    e.target.value = ''
  }

  const deleteMutation = useMutation({
    mutationFn: async () => {
      await deleteDocument({ path: { document_id: id } })
    },
    onSuccess: () => {
      message.success('文档已删除')
      queryClient.invalidateQueries({ queryKey: ['documents'] })
      navigate('/documents')
    },
  })

  const tagsMutation = useMutation({
    mutationFn: async (tags: string[]) => {
      const res = await updateDocumentPermissionTags({
        path: { document_id: id },
        body: { permission_tags: tags },
      })
      return res.data!
    },
    onSuccess: () => {
      message.success('权限标签已更新')
      queryClient.invalidateQueries({ queryKey: ['documents'] })
      setTagsModalOpen(false)
    },
  })

  const previewUrl = useMemo(() => buildDocumentFileUrl(id, { download: false }), [id])
  const downloadUrl = useMemo(() => buildDocumentFileUrl(id, { download: true }), [id])

  if (docQuery.isLoading) return <Skeleton active />
  if (!doc) return null

  const canDelete = DELETABLE_STATUSES.has(doc.status)
  const canRetry = doc.status === 'failed'
  const supportsInlinePreview = canPreviewInline(doc.mime_type)

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Link to="/documents">
          <Button icon={<ArrowLeftOutlined />}>返回文档列表</Button>
        </Link>
        <Button
          icon={<DownloadOutlined />}
          href={downloadUrl}
          target="_blank"
          rel="noreferrer"
        >
          下载
        </Button>
        {supportsInlinePreview ? (
          <Button
            icon={<EyeOutlined />}
            href={previewUrl}
            target="_blank"
            rel="noreferrer"
          >
            新窗口打开
          </Button>
        ) : null}
        {isAdmin && canRetry ? (
          <Button
            icon={<RedoOutlined />}
            loading={retryMutation.isPending}
            onClick={() => retryMutation.mutate()}
          >
            重试解析
          </Button>
        ) : null}
        {isAdmin ? (
          <>
            <input
              ref={reindexInputRef}
              type="file"
              accept=".pdf,.docx,.md,.markdown,.html,.htm"
              style={{ display: 'none' }}
              onChange={onPickReindexFile}
            />
            <Button
              icon={<CloudUploadOutlined />}
              disabled={!isTerminalStatus(doc.status)}
              loading={reindexMutation.isPending}
              onClick={() => reindexInputRef.current?.click()}
            >
              上传新版本
            </Button>
          </>
        ) : null}
        {isAdmin ? (
          <Popconfirm
            title="确认删除该文档？"
            description="将同时删除文档内容、所有切片以及云端原文件，无法恢复。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            disabled={!canDelete}
            onConfirm={() => deleteMutation.mutate()}
          >
            <Button
              danger
              icon={<DeleteOutlined />}
              disabled={!canDelete}
              loading={deleteMutation.isPending}
            >
              删除
            </Button>
          </Popconfirm>
        ) : null}
      </Space>

      <Title level={3} style={{ marginBottom: 16 }}>
        {doc.name}
      </Title>

      {doc.status === 'failed' && doc.error_message ? (
        <Alert
          type="error"
          message="入库失败"
          description={doc.error_message}
          showIcon
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Descriptions bordered column={1} size="middle" style={{ marginBottom: 24 }}>
        <Descriptions.Item label="状态">
          <Tag color={getStatusColor(doc.status)}>{getStatusLabel(doc.status)}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="版本">
          <Tag color="purple">v{doc.version}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="ID">{doc.id}</Descriptions.Item>
        <Descriptions.Item label="文件 hash">{doc.file_hash}</Descriptions.Item>
        <Descriptions.Item label="MIME 类型">{doc.mime_type}</Descriptions.Item>
        <Descriptions.Item label="大小">{formatSize(doc.size)}</Descriptions.Item>
        <Descriptions.Item label="权限标签">
          <Space size={6} wrap>
            {(doc.permission_tags ?? []).length === 0 ? (
              <Tag>公开</Tag>
            ) : (
              (doc.permission_tags ?? []).map((t) => (
                <Tag color={t === '*' ? 'gold' : 'blue'} key={t}>
                  {t}
                </Tag>
              ))
            )}
            {isAdmin ? (
              <Button
                size="small"
                type="link"
                icon={<EditOutlined />}
                onClick={() => setTagsModalOpen(true)}
              >
                编辑
              </Button>
            ) : null}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="上传时间">
          {new Date(doc.created_at).toLocaleString('zh-CN')}
        </Descriptions.Item>
        <Descriptions.Item label="更新时间">
          {new Date(doc.updated_at).toLocaleString('zh-CN')}
        </Descriptions.Item>
      </Descriptions>

      {doc.latest_task ? (
        <Card title="最近一次入库任务" style={{ marginBottom: 24 }}>
          <IngestionTaskCard task={doc.latest_task} />
        </Card>
      ) : null}

      <Card title="原文预览" style={{ marginBottom: 24 }}>
        <PreviewArea mimeType={doc.mime_type} previewUrl={previewUrl} />
      </Card>

      <Card title="切分结果">
        <ChunksSection
          docStatus={doc.status}
          chunksQuery={chunksQuery}
          page={chunkPage}
          onPageChange={setChunkPage}
          onPickChunk={setActiveChunkId}
        />
      </Card>

      <Modal
        title="Chunk 完整内容"
        open={!!activeChunkId}
        onCancel={() => setActiveChunkId(null)}
        footer={null}
        width={720}
      >
        {chunkDetailQuery.isLoading ? (
          <Skeleton active />
        ) : chunkDetailQuery.data ? (
          <ChunkDetailBody chunk={chunkDetailQuery.data} />
        ) : null}
      </Modal>

      {isAdmin ? (
        <EditTagsModal
          open={tagsModalOpen}
          initial={doc.permission_tags ?? []}
          loading={tagsMutation.isPending}
          onCancel={() => setTagsModalOpen(false)}
          onSubmit={(tags) => tagsMutation.mutate(tags)}
        />
      ) : null}
    </div>
  )
}

function EditTagsModal({
  open,
  initial,
  loading,
  onCancel,
  onSubmit,
}: {
  open: boolean
  initial: string[]
  loading: boolean
  onCancel: () => void
  onSubmit: (tags: string[]) => void
}) {
  const [form] = Form.useForm<{ permission_tags: string[] }>()
  useEffect(() => {
    if (open) {
      form.setFieldsValue({ permission_tags: initial })
    }
  }, [open, initial, form])
  return (
    <Modal
      title="编辑权限标签"
      open={open}
      onCancel={onCancel}
      onOk={() => form.submit()}
      confirmLoading={loading}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(values) => onSubmit(values.permission_tags ?? [])}
      >
        <Form.Item
          name="permission_tags"
          label="权限标签"
          extra="留空视为公开（所有登录用户可见）"
        >
          <PermissionTagsField />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function PreviewArea({ mimeType, previewUrl }: { mimeType: string; previewUrl: string }) {
  if (isPdfMime(mimeType) || isHtmlMime(mimeType)) {
    return (
      <iframe
        title="document-preview"
        src={previewUrl}
        style={{ width: '100%', height: 600, border: '1px solid #f0f0f0' }}
      />
    )
  }
  if (isMarkdownMime(mimeType)) {
    return <MarkdownPreview url={previewUrl} />
  }
  return (
    <Alert
      type="info"
      showIcon
      message="该格式不支持内联预览"
      description="DOCX 等富文本格式请下载后用本地编辑器查看。"
    />
  )
}

interface ChunksQueryResult {
  isLoading: boolean
  data:
    | {
        items: DocumentChunkRead[]
        total: number
        stats?:
          | {
              total: number
              avg_length: number
              min_length: number
              max_length: number
            }
          | null
      }
    | undefined
}

function ChunksSection({
  docStatus,
  chunksQuery,
  page,
  onPageChange,
  onPickChunk,
}: {
  docStatus: DocumentRead['status']
  chunksQuery: ChunksQueryResult
  page: number
  onPageChange: (p: number) => void
  onPickChunk: (id: string) => void
}) {
  if (docStatus !== 'ready') {
    return (
      <Empty
        description={`当前状态：${getStatusLabel(docStatus)}，切分结果就绪后将自动展示`}
      />
    )
  }
  if (chunksQuery.isLoading) return <Skeleton active />
  const data = chunksQuery.data
  if (!data) return <Empty />

  const { items, total, stats } = data
  return (
    <>
      {stats ? (
        <Space size="large" wrap style={{ marginBottom: 16 }}>
          <Statistic title="Chunk 数量" value={stats.total} />
          <Statistic title="平均长度" value={stats.avg_length} suffix="字符" />
          <Statistic title="最短" value={stats.min_length} suffix="字符" />
          <Statistic title="最长" value={stats.max_length} suffix="字符" />
        </Space>
      ) : null}
      <List<DocumentChunkRead>
        bordered
        dataSource={items}
        renderItem={(chunk) => (
          <List.Item
            actions={[
              <Button
                key="detail"
                type="link"
                onClick={() => onPickChunk(chunk.id)}
              >
                查看完整内容
              </Button>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <Text>#{chunk.chunk_index}</Text>
                  {chunk.page_no ? <Tag>页 {chunk.page_no}</Tag> : null}
                  {chunk.section_path ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {chunk.section_path}
                    </Text>
                  ) : null}
                </Space>
              }
              description={
                <Paragraph
                  style={{ marginBottom: 0, color: 'rgba(0,0,0,.65)' }}
                  ellipsis={{ rows: 2 }}
                >
                  {chunk.content_excerpt}
                </Paragraph>
              }
            />
          </List.Item>
        )}
      />
      <div style={{ marginTop: 12, textAlign: 'right' }}>
        <Pagination
          current={page}
          pageSize={CHUNK_PAGE_SIZE}
          total={total}
          showSizeChanger={false}
          onChange={onPageChange}
        />
      </div>
    </>
  )
}

const TASK_TYPE_LABEL: Record<IngestionTaskRead['task_type'], string> = {
  ingest: '首次入库',
  reindex: '增量重建',
}

const TASK_STATUS_COLOR: Record<IngestionTaskRead['status'], string> = {
  pending: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
}

const TASK_STATUS_LABEL: Record<IngestionTaskRead['status'], string> = {
  pending: '排队中',
  running: '执行中',
  success: '已完成',
  failed: '失败',
}

function IngestionTaskCard({ task }: { task: IngestionTaskRead }) {
  // pending 阶段还没确定 progress_total，按 0% 展示
  const percent =
    task.progress_total > 0
      ? Math.min(100, Math.round((task.progress_done / task.progress_total) * 100))
      : 0
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap>
        <Tag>{TASK_TYPE_LABEL[task.task_type]}</Tag>
        <Tag color={TASK_STATUS_COLOR[task.status]}>
          {TASK_STATUS_LABEL[task.status]}
        </Tag>
        <Text type="secondary">创建于 {new Date(task.created_at).toLocaleString('zh-CN')}</Text>
      </Space>
      <Progress
        percent={percent}
        status={
          task.status === 'failed'
            ? 'exception'
            : task.status === 'success'
              ? 'success'
              : 'active'
        }
        format={() =>
          task.progress_total > 0
            ? `${task.progress_done} / ${task.progress_total}`
            : task.status === 'success'
              ? '完成'
              : '等待中'
        }
      />
      {task.error_message ? (
        <Alert type="error" showIcon message="任务失败" description={task.error_message} />
      ) : null}
    </Space>
  )
}

function ChunkDetailBody({ chunk }: { chunk: DocumentChunkDetail }) {
  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Tag>chunk_index: {chunk.chunk_index}</Tag>
        {chunk.page_no ? <Tag>页 {chunk.page_no}</Tag> : null}
        {chunk.section_path ? <Tag>{chunk.section_path}</Tag> : null}
        <Tag>{chunk.char_count} 字符</Tag>
        <Text type="secondary">hash: {chunk.chunk_hash}</Text>
      </Space>
      <Paragraph
        style={{
          whiteSpace: 'pre-wrap',
          maxHeight: 480,
          overflow: 'auto',
          padding: 12,
          background: '#fafafa',
          borderRadius: 4,
        }}
      >
        {chunk.content}
      </Paragraph>
    </div>
  )
}
