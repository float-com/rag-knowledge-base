/**
 * Bad Case 12+1 类归因的中文标签映射。
 *
 * 拆出来不放组件文件：组件文件按 react-refresh 约定只导出组件。
 */

import type { BadCaseCategory } from '@/api/evaluation'

export const BAD_CASE_CATEGORY_LABELS: Record<BadCaseCategory, string> = {
  document_parse_failed: '文档解析失败',
  chunk_split_bad: 'chunk 切分不合理',
  embedding_recall_miss: 'embedding 召回失败',
  keyword_recall_miss: '关键词召回缺失',
  rrf_fusion_error: 'RRF 融合异常',
  rerank_order_error: 'rerank 排序错误',
  context_judge_too_loose: '上下文判断过松',
  context_judge_too_strict: '上下文判断过严',
  prompt_constraint_weak: 'Prompt 约束不足',
  generation_off_context: '模型生成偏离上下文',
  citation_parse_failed: '引用解析失败',
  permission_filter_error: '权限过滤错误',
  other: '其他',
}
