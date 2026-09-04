/**
 * Bad Case 归因下拉：与 backend Literal 一致的 12+1 类。
 *
 * value=null 代表"未归因"，前端用 undefined 给 Select 让它显示 placeholder。
 */

import { Select } from 'antd'
import type { BadCaseCategory } from '@/api/evaluation'
import { BAD_CASE_CATEGORY_LABELS } from '@/utils/badCaseCategory'

interface Props {
  value: BadCaseCategory | null | undefined
  onChange: (value: BadCaseCategory | null) => void
  allowClear?: boolean
  style?: React.CSSProperties
  placeholder?: string
}

export function BadCaseCategorySelect({ value, onChange, allowClear = true, style, placeholder = '选择归因类别' }: Props) {
  return (
    <Select
      value={value ?? undefined}
      onChange={(v) => onChange((v as BadCaseCategory) ?? null)}
      onClear={() => onChange(null)}
      allowClear={allowClear}
      placeholder={placeholder}
      style={{ minWidth: 200, ...style }}
      options={(Object.keys(BAD_CASE_CATEGORY_LABELS) as BadCaseCategory[]).map((key) => ({
        value: key,
        label: BAD_CASE_CATEGORY_LABELS[key],
      }))}
    />
  )
}
