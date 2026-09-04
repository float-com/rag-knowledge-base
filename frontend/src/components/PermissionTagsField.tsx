/**
 * 权限标签输入框：`Select mode="tags"`，admin 表单复用。
 *
 * 用法（受控）：
 *   <PermissionTagsField value={tags} onChange={setTags} />
 *
 * 用法（Form.Item 内）：
 *   <Form.Item name="permission_tags"><PermissionTagsField /></Form.Item>
 */

import { Select } from 'antd'

interface PermissionTagsFieldProps {
  value?: string[]
  onChange?: (value: string[]) => void
  placeholder?: string
  disabled?: boolean
}

export function PermissionTagsField({
  value,
  onChange,
  placeholder = '输入标签后回车，留空视为公开',
  disabled,
}: PermissionTagsFieldProps) {
  return (
    <Select
      mode="tags"
      tokenSeparators={[',', ' ']}
      placeholder={placeholder}
      value={value}
      onChange={onChange}
      disabled={disabled}
      style={{ width: '100%' }}
    />
  )
}
