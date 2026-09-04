import { forwardRef, useImperativeHandle, useState } from 'react'
import { Collapse, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { CitationRead } from '@/client/types.gen'

const { Paragraph } = Typography

export interface CitationListHandle {
  /** 展开第 n 条（从 1 开始）并滚动到视图中央。越界则忽略。 */
  expandAndScroll: (n: number) => void
}

interface CitationListProps {
  citations: CitationRead[]
  /** 用于生成 DOM 锚点 id，避免多条 assistant 消息的引用面板互相串扰。 */
  messageId: string
}

type SourceTagMeta = { color: string; label: string }

function formatSourceTag(sources?: string[]): SourceTagMeta | null {
  if (!sources || sources.length === 0) return null
  const hasVector = sources.includes('vector')
  const hasKeyword = sources.includes('keyword')
  if (hasVector && hasKeyword) return { color: 'purple', label: '混合' }
  if (hasVector) return { color: 'blue', label: '向量' }
  if (hasKeyword) return { color: 'orange', label: '关键词' }
  return null
}

export const CitationList = forwardRef<CitationListHandle, CitationListProps>(
  function CitationList({ citations, messageId }, ref) {
    // Collapse 受控 activeKey：保持用户已展开的项不被点击新引用时折叠回去
    const [activeKey, setActiveKey] = useState<string[]>([])

    useImperativeHandle(
      ref,
      () => ({
        expandAndScroll: (n: number) => {
          // 用 ordinal 而非数组下标定位：保证 markdown 里写的 [N] 永远精确指向
          // ordinal=N 的那条引用，即使 citations 数组顺序意外变化也不会串号
          const target = citations.find((c) => c.ordinal === n)
          if (!target) return
          const key = panelKey(target)
          setActiveKey((prev) => (prev.includes(key) ? prev : [...prev, key]))
          requestAnimationFrame(() => {
            document
              .getElementById(anchorId(messageId, n))
              ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
          })
        },
      }),
      [citations, messageId],
    )

    if (citations.length === 0) return null

    const items = citations.map((c) => {
      const sourceTag = formatSourceTag(c.retrieval_meta?.sources)
      const rerankScore = c.retrieval_meta?.rerank_score
      return {
      key: panelKey(c),
      label: (
        <span id={anchorId(messageId, c.ordinal)}>
          <Tag color="blue" style={{ marginInlineEnd: 8 }}>{`[${c.ordinal}]`}</Tag>
          {sourceTag ? (
            <Tag color={sourceTag.color} style={{ marginInlineEnd: 8 }}>
              {sourceTag.label}
            </Tag>
          ) : null}
          {rerankScore != null ? (
            <Tag color="gold" style={{ marginInlineEnd: 8 }}>
              {`rerank ${rerankScore.toFixed(2)}`}
            </Tag>
          ) : null}
          {c.document_id ? (
            <Link to={`/documents/${c.document_id}`}>{c.document_name}</Link>
          ) : (
            <span>{c.document_name}</span>
          )}
          {c.page_no != null ? (
            <span style={{ marginInlineStart: 8, color: '#999' }}>第 {c.page_no} 页</span>
          ) : null}
        </span>
      ),
      children: (
        <Paragraph
          style={{ whiteSpace: 'pre-wrap', marginBottom: 0, color: '#555' }}
          ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}
        >
          {c.quote}
        </Paragraph>
      ),
      }
    })

    return (
      <Collapse
        size="small"
        ghost
        items={items}
        activeKey={activeKey}
        onChange={(keys) => setActiveKey(typeof keys === 'string' ? [keys] : keys)}
        style={{ marginTop: 12, background: '#fafafa', borderRadius: 6 }}
      />
    )
  },
)

function anchorId(messageId: string, n: number): string {
  return `cite-${messageId}-${n}`
}

/** 流式过程中 SSE 载荷没有数据库 id，落库后才有；统一用 ordinal 兜底，
 * 保证同一 assistant 消息内 key 唯一且稳定。 */
function panelKey(c: CitationRead): string {
  return c.id || `ord-${c.ordinal}`
}
