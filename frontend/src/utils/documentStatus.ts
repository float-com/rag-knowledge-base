import type { DocumentRead } from '@/client/types.gen'

type DocumentStatus = DocumentRead['status']

const TERMINAL_STATUSES: ReadonlySet<DocumentStatus> = new Set(['ready', 'failed'])

const STATUS_LABELS: Record<DocumentStatus, string> = {
  uploading: '上传中',
  parsing: '解析中',
  indexing: '索引中',
  ready: '已就绪',
  failed: '失败',
}

const STATUS_COLORS: Record<DocumentStatus, string> = {
  uploading: 'blue',
  parsing: 'cyan',
  indexing: 'geekblue',
  ready: 'green',
  failed: 'red',
}

export function isTerminalStatus(status: DocumentStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function getStatusLabel(status: DocumentStatus): string {
  return STATUS_LABELS[status]
}

export function getStatusColor(status: DocumentStatus): string {
  return STATUS_COLORS[status]
}
