/**
 * 评测相关 API 薄包装与 hooks。
 *
 * - 业务侧只 import 这里，不直接碰 @/client（项目工程约定）
 * - running 状态的 run 自动 5 秒轮询，对齐第 3 章文档非终态轮询风格
 */

import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createEvaluationRun as sdkCreateEvaluationRun,
  deleteEvaluationRun as sdkDeleteEvaluationRun,
  getEvaluationRun as sdkGetEvaluationRun,
  listEvaluationDatasets as sdkListEvaluationDatasets,
  listEvaluationItems as sdkListEvaluationItems,
  listEvaluationRuns as sdkListEvaluationRuns,
  updateEvaluationItem as sdkUpdateEvaluationItem,
} from '@/client/sdk.gen'
import type {
  EvaluationItemRead,
  EvaluationItemUpdate,
  EvaluationRunRead,
} from '@/client/types.gen'
import {
  evaluationDatasetsKey,
  evaluationItemsKey,
  evaluationRunKey,
  evaluationRunsKey,
} from '@/api/queryKeys'

type BadCaseCategory = NonNullable<EvaluationItemRead['bad_case_category']>

export function useEvaluationDatasets() {
  return useQuery({
    queryKey: evaluationDatasetsKey,
    queryFn: async () => (await sdkListEvaluationDatasets()).data,
  })
}

export function useEvaluationRuns(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: [...evaluationRunsKey, page, pageSize],
    queryFn: async () =>
      (await sdkListEvaluationRuns({ query: { page, page_size: pageSize } })).data,
    // running 状态的 run 进度会变化，让列表自动刷新
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? []
      return items.some((r) => r.status === 'running') ? 5000 : false
    },
  })
}

/**
 * 兜底补刷：浏览器会限制【后台标签页】的定时器（Chrome 对不可见页面
 * 把 setInterval 压到 ~1 分钟甚至冻结），长跑评测期间一旦切走标签页，
 * refetchInterval 就等于停了，切回来看到的是停之前那一帧（仍是「执行中」）。
 *
 * 为什么不靠 TanStack Query 自带的 refetchOnWindowFocus：
 * 本项目的 QueryClient 默认把 refetchOnWindowFocus 关成了 false（见 src/main.tsx），
 * 所以这里用显式的 focus 监听补一轮，只影响评测列表页，不改变全局行为。
 *
 * invalidateQueries 用 ['evaluation-runs'] 前缀匹配，
 * 因此不管当前在第几页（key 尾部带 page/pageSize）都会被一起失效。
 */
export function useEvaluationRunsFocusRefresh() {
  const queryClient = useQueryClient()
  useEffect(() => {
    const onFocus = () => {
      queryClient.invalidateQueries({ queryKey: evaluationRunsKey })
    }
    window.addEventListener('focus', onFocus)
    // 卸载时摘掉监听，避免离开列表页后还在触发无谓请求
    return () => window.removeEventListener('focus', onFocus)
  }, [queryClient])
}

export function useEvaluationRun(runId: string | undefined) {
  return useQuery({
    queryKey: runId ? evaluationRunKey(runId) : ['evaluation-run', 'none'],
    enabled: Boolean(runId),
    queryFn: async () =>
      (await sdkGetEvaluationRun({ path: { run_id: runId! } })).data,
    refetchInterval: (query) => {
      const run = query.state.data as EvaluationRunRead | undefined
      return run?.status === 'running' ? 5000 : false
    },
  })
}

export function useEvaluationItems(
  runId: string | undefined,
  filters: {
    page: number
    pageSize: number
    badCaseOnly: boolean
    category: BadCaseCategory | null
  },
) {
  return useQuery({
    queryKey: runId
      ? evaluationItemsKey(runId, {
          badCaseOnly: filters.badCaseOnly,
          category: filters.category,
          page: filters.page,
        })
      : ['evaluation-items', 'none'],
    enabled: Boolean(runId),
    queryFn: async () =>
      (
        await sdkListEvaluationItems({
          path: { run_id: runId! },
          query: {
            page: filters.page,
            page_size: filters.pageSize,
            bad_case_only: filters.badCaseOnly,
            category: filters.category ?? undefined,
          },
        })
      ).data,
  })
}

export function useCreateEvaluationRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string; dataset_name: string }) =>
      (await sdkCreateEvaluationRun({ body })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: evaluationRunsKey })
    },
  })
}

export function useDeleteEvaluationRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (runId: string) =>
      sdkDeleteEvaluationRun({ path: { run_id: runId } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: evaluationRunsKey })
    },
  })
}

export function useUpdateEvaluationItem(runId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (params: { itemId: string; body: EvaluationItemUpdate }) =>
      (
        await sdkUpdateEvaluationItem({
          path: { item_id: params.itemId },
          body: params.body,
        })
      ).data,
    onSuccess: () => {
      // items 列表的 query key 包含 filters，统一前缀 invalidate
      queryClient.invalidateQueries({ queryKey: ['evaluation-items', runId] })
      // run 聚合指标本身不变，但 PATCH 后用户期望详情立即刷新
      queryClient.invalidateQueries({ queryKey: evaluationRunKey(runId) })
    },
  })
}

export type { BadCaseCategory }
