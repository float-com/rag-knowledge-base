/**
 * 角色管理 API 薄包装与 hooks（admin 用）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createRole as sdkCreateRole,
  deleteRole as sdkDeleteRole,
  listRoles as sdkListRoles,
  updateRole as sdkUpdateRole,
} from '@/client/sdk.gen'
import type { RoleCreate, RoleUpdate } from '@/client/types.gen'
import { rolesListKey } from '@/api/queryKeys'

export function useRoles(enabled = true) {
  return useQuery({
    queryKey: rolesListKey,
    queryFn: async () => (await sdkListRoles()).data,
    enabled,
  })
}

export function useCreateRoleMutation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (payload: RoleCreate) =>
      (await sdkCreateRole({ body: payload })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rolesListKey })
    },
  })
}

export function useUpdateRoleMutation(roleId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (payload: RoleUpdate) =>
      (await sdkUpdateRole({ path: { role_id: roleId }, body: payload })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rolesListKey })
    },
  })
}

export function useDeleteRoleMutation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (roleId: string) => {
      await sdkDeleteRole({ path: { role_id: roleId } })
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rolesListKey })
    },
  })
}
