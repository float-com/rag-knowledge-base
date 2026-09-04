/**
 * 用户管理 API 薄包装与 hooks（admin 用）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  assignUserRoles as sdkAssignUserRoles,
  createUser as sdkCreateUser,
  deleteUser as sdkDeleteUser,
  listUsers as sdkListUsers,
  updateUser as sdkUpdateUser,
} from '@/client/sdk.gen'
import type {
  AssignRolesRequest,
  UserCreate,
  UserUpdate,
} from '@/client/types.gen'
import { usersListKey } from '@/api/queryKeys'

export function useUsers(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: usersListKey(page, pageSize),
    queryFn: async () =>
      (
        await sdkListUsers({ query: { page, page_size: pageSize } })
      ).data,
  })
}

export function useCreateUserMutation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (payload: UserCreate) =>
      (await sdkCreateUser({ body: payload })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
    },
  })
}

export function useUpdateUserMutation(userId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (payload: UserUpdate) =>
      (await sdkUpdateUser({ path: { user_id: userId }, body: payload })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
    },
  })
}

export function useAssignRolesMutation(userId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (payload: AssignRolesRequest) =>
      (
        await sdkAssignUserRoles({ path: { user_id: userId }, body: payload })
      ).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
    },
  })
}

export function useDeleteUserMutation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (userId: string) => {
      await sdkDeleteUser({ path: { user_id: userId } })
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
    },
  })
}
