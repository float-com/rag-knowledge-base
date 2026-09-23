/**
 * 会话历史侧栏：展示按 updated_at 倒序的会话列表，支持切换 / 删除 / 新建。
 *
 * 设计要点：
 * - 上层（ChatPage）传入 currentId / onSelect / onDelete / onCreate，组件本身只
 *   负责拉数据 + 渲染 + 触发回调；这样切换时取消正在进行的 SSE 等副作用全部留在 ChatPage
 * - 删除动作用 antd Popconfirm 二次确认，避免误删历史
 * - 新建对话按钮放在侧栏顶部，符合主流 ChatGPT 风格
 */

import { Button, List, Popconfirm, Spin, Tooltip, Typography, message } from 'antd'
import { DeleteOutlined, MessageOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { conversationsQueryKey } from '@/api/queryKeys'
import { deleteConversation, listConversations } from '@/client/sdk.gen'
import type { ConversationListItem } from '@/client/types.gen'

const { Text } = Typography

interface ConversationSidebarProps {
  currentId: string | null
  onSelect: (id: string) => void
  /** 当前会话被删时回调，让 ChatPage 重置当前 id 并清空 pending */
  onDeleted: (deletedId: string) => void
  /** 新建对话；ChatPage 内部已有 createMutation，这里只触发，避免双重 mutation */
  onCreateNew: () => void
  /** 新建按钮 loading；与 ChatPage 的 createMutation 联动 */
  isCreating?: boolean
}

export function ConversationSidebar({
  currentId,
  onSelect,
  onDeleted,
  onCreateNew,
  isCreating,
}: ConversationSidebarProps) {
  const queryClient = useQueryClient()

  const conversationsQuery = useQuery({
    queryKey: conversationsQueryKey,
    queryFn: async () => {
      // 拉一页足够；侧栏不做无限滚动，超过 100 条的场景留到后续章节
      const res = await listConversations({ query: { page: 1, page_size: 100 } })
      return res.data!
    },
  })

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => {
      await deleteConversation({ path: { conversation_id: id } })
      return id
    },
    onSuccess: async (id) => {
      message.success('已删除会话')
      // 失效列表缓存，让侧栏重新拉一次（消息数、排序都可能变）
      await queryClient.invalidateQueries({ queryKey: conversationsQueryKey })
      // 删掉的正是当前会话 → 交给上层重置 currentId 并清空 pending
      if (id === currentId) onDeleted(id)
    },
  })

  const items = conversationsQuery.data?.items ?? []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ padding: 12, borderBottom: '1px solid #f0f0f0' }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={onCreateNew}
          loading={isCreating}
        >
          新建对话
        </Button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {conversationsQuery.isLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : items.length === 0 ? (
          <Text
            type="secondary"
            style={{ display: 'block', textAlign: 'center', padding: 24 }}
          >
            暂无会话，点上方"新建对话"开始
          </Text>
        ) : (
          <List
            size="small"
            dataSource={items}
            renderItem={(item) => (
              <ConversationItem
                item={item}
                isActive={item.id === currentId}
                isDeleting={
                  deleteMutation.isPending && deleteMutation.variables === item.id
                }
                onSelect={() => onSelect(item.id)}
                onDelete={() => deleteMutation.mutate(item.id)}
              />
            )}
          />
        )}
      </div>
    </div>
  )
}

interface ConversationItemProps {
  item: ConversationListItem
  isActive: boolean
  isDeleting: boolean
  onSelect: () => void
  onDelete: () => void
}

function ConversationItem({
  item,
  isActive,
  isDeleting,
  onSelect,
  onDelete,
}: ConversationItemProps) {
  return (
    <List.Item
      style={{
        cursor: 'pointer',
        background: isActive ? '#e6f4ff' : 'transparent',
        padding: '8px 12px',
        borderRadius: 4,
        margin: '2px 8px',
        border: 'none',
      }}
      onClick={onSelect}
      actions={[
        <Popconfirm
          key="del"
          title="删除该会话？"
          description="将一并删除会话内的所有消息和引用，无法恢复。"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          // ⚠️ 每个 List.Item 都要 stopPropagation 处理删除按钮的点击事件，
          //    否则点删除按钮会同时触发整行的 onClick（切会话 + 弹确认框一起发生）
          onConfirm={(e) => {
            e?.stopPropagation()
            onDelete()
          }}
          onCancel={(e) => e?.stopPropagation()}
        >
          <Tooltip title="删除会话">
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              loading={isDeleting}
              onClick={(e) => e.stopPropagation()}
            />
          </Tooltip>
        </Popconfirm>,
      ]}
    >
      <List.Item.Meta
        avatar={<MessageOutlined style={{ color: isActive ? '#1677ff' : '#999' }} />}
        title={
          <Text
            ellipsis={{ tooltip: item.title }}
            style={{
              fontSize: 13,
              fontWeight: isActive ? 600 : 400,
              color: isActive ? '#1677ff' : undefined,
            }}
          >
            {item.title}
          </Text>
        }
        description={
          <Text type="secondary" style={{ fontSize: 11 }}>
            {item.message_count} 条 · {formatRelativeTime(item.updated_at)}
          </Text>
        }
      />
    </List.Item>
  )
}

/**
 * 把 ISO 时间格式化成"X 分钟前 / X 小时前 / YYYY-MM-DD"。
 * 侧栏空间窄，不展示完整 datetime；超过 7 天降级到日期。
 */
function formatRelativeTime(iso: string): string {
  const date = new Date(iso)
  const diffMs = Date.now() - date.getTime()
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (diffMs < hour) return `${Math.max(1, Math.floor(diffMs / minute))} 分钟前`
  if (diffMs < day) return `${Math.floor(diffMs / hour)} 小时前`
  if (diffMs < 7 * day) return `${Math.floor(diffMs / day)} 天前`
  return date.toLocaleDateString('zh-CN')
}
