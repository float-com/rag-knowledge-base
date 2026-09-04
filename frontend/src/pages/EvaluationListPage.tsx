/**
 * 评测 run 列表页：表格 + 新建 Modal + 删除 Popconfirm。
 *
 * running 状态的 run 自动 5s 轮询（在 useEvaluationRuns hook 里）。
 */

import { useState } from 'react'
import { App, Button, Form, Input, Modal, Popconfirm, Select, Table, Tag } from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import {
  useCreateEvaluationRun,
  useDeleteEvaluationRun,
  useEvaluationDatasets,
  useEvaluationRuns,
} from '@/api/evaluation'
import type { EvaluationRunListItem } from '@/client/types.gen'

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  running: { color: 'processing', label: '执行中' },
  completed: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
}

function pct(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

export function EvaluationListPage() {
  const [page, setPage] = useState(1)
  const pageSize = 20
  const { data, isLoading } = useEvaluationRuns(page, pageSize)
  const [modalOpen, setModalOpen] = useState(false)

  const columns: ColumnsType<EvaluationRunListItem> = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (text, record) => <Link to={`/evaluation/runs/${record.id}`}>{text}</Link>,
    },
    {
      title: '评测集',
      dataIndex: 'dataset_name',
      width: 120,
      render: (text, r) => (
        <span>
          {text} <Tag>{r.dataset_size}</Tag>
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (status: string, r) => {
        const tag = STATUS_TAG[status] ?? { color: 'default', label: status }
        if (status === 'running') {
          return (
            <Tag color={tag.color}>
              {tag.label} {r.progress_completed}/{r.progress_total}
            </Tag>
          )
        }
        return <Tag color={tag.color}>{tag.label}</Tag>
      },
    },
    { title: 'Faithfulness', dataIndex: 'faithfulness', width: 110, render: pct },
    { title: 'AnswerRel.', dataIndex: 'answer_relevancy', width: 110, render: pct },
    { title: 'Ctx Prec.', dataIndex: 'context_precision', width: 110, render: pct },
    { title: 'Ctx Recall', dataIndex: 'context_recall', width: 110, render: pct },
    { title: '引用命中', dataIndex: 'citation_hit_rate', width: 100, render: pct },
    { title: '拒答正确', dataIndex: 'refusal_accuracy', width: 100, render: pct },
    {
      title: '平均耗时',
      dataIndex: 'avg_latency_ms',
      width: 110,
      render: (v: number | null) => (v === null || v === undefined ? '—' : `${Math.round(v)} ms`),
    },
    {
      title: '首 token 延迟',
      dataIndex: 'avg_first_token_latency_ms',
      width: 120,
      render: (v: number | null) => (v === null || v === undefined ? '—' : `${Math.round(v)} ms`),
    },
    {
      title: '操作',
      width: 80,
      render: (_, record) => <DeleteRunButton runId={record.id} runName={record.name} />,
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0 }}>评测分析</h2>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新建评测
        </Button>
      </div>
      <Table
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          onChange: (p) => setPage(p),
          showSizeChanger: false,
        }}
        scroll={{ x: 1400 }}
      />
      <CreateRunModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}

function DeleteRunButton({ runId, runName }: { runId: string; runName: string }) {
  const { message } = App.useApp()
  const mutation = useDeleteEvaluationRun()
  return (
    <Popconfirm
      title={`删除评测 “${runName}”？`}
      description="该 run 及其全部 case 都会被删除"
      okType="danger"
      onConfirm={async () => {
        await mutation.mutateAsync(runId)
        message.success('已删除')
      }}
    >
      <Button type="text" danger icon={<DeleteOutlined />} />
    </Popconfirm>
  )
}

function CreateRunModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = App.useApp()
  const datasets = useEvaluationDatasets()
  const mutation = useCreateEvaluationRun()
  const [form] = Form.useForm<{ name: string; dataset_name: string }>()

  return (
    <Modal
      title="新建评测"
      open={open}
      onCancel={onClose}
      destroyOnHidden
      onOk={async () => {
        const values = await form.validateFields()
        await mutation.mutateAsync(values)
        message.success('评测已创建，后台开始执行')
        form.resetFields()
        onClose()
      }}
      confirmLoading={mutation.isPending}
      okText="开始评测"
    >
      <Form form={form} layout="vertical" initialValues={{ name: '', dataset_name: 'seed' }}>
        <Form.Item
          name="name"
          label="评测名称"
          rules={[{ required: true, message: '请输入名称' }, { max: 256 }]}
        >
          <Input placeholder="如：接入 rerank 后" />
        </Form.Item>
        <Form.Item
          name="dataset_name"
          label="评测集"
          rules={[{ required: true, message: '请选择评测集' }]}
        >
          <Select
            loading={datasets.isLoading}
            options={(datasets.data?.items ?? []).map((d) => ({
              value: d.name,
              label: `${d.name}（${d.size} 条）`,
            }))}
            placeholder="选择评测集"
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
