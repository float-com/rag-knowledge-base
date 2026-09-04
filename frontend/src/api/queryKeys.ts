/**
 * TanStack Query 共享 query key 集中定义。
 *
 * 拆出来的目的：组件文件保持只导出组件（满足 react-refresh/only-export-components），
 * 同时让 invalidate / removeQueries 跨组件用同一份 key 引用，避免硬编码字符串数组到处散落。
 */

export const conversationsQueryKey = ['conversations'] as const

export const evaluationRunsKey = ['evaluation-runs'] as const
export const evaluationRunKey = (runId: string) => ['evaluation-run', runId] as const
export const evaluationItemsKey = (
  runId: string,
  filters: { badCaseOnly: boolean; category: string | null; page: number },
) => ['evaluation-items', runId, filters] as const
export const evaluationDatasetsKey = ['evaluation-datasets'] as const

export const currentUserKey = ['auth', 'me'] as const
export const usersListKey = (page: number, pageSize: number) =>
  ['users', { page, pageSize }] as const
export const rolesListKey = ['roles'] as const
