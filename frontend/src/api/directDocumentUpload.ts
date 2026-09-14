import { getAuthToken } from '@/stores/authStore'

type UploadSession = {
  id: string
  object_key: string
  original_name: string
  mime_type: string
  expected_size: number
  status: string
  expires_at: string
  completed_at?: string | null
  error_message?: string | null
}

type InitUploadResponse = UploadSession & {
  presigned_url: string
}

function authHeaders(): HeadersInit {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = await response.json()
    return payload?.message ?? payload?.detail ?? `请求失败 (${response.status})`
  } catch {
    return `请求失败 (${response.status})`
  }
}

async function requestJson<T>(url: string, init: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      ...authHeaders(),
      'Content-Type': 'application/json',
      ...init.headers,
    },
  })
  if (!response.ok) throw new Error(await readError(response))
  return response.json() as Promise<T>
}

export async function uploadDocumentDirect(file: File, permissionTags: string[]) {
  const initialized = await requestJson<InitUploadResponse>('/api/documents/uploads/init', {
    method: 'POST',
    body: JSON.stringify({
      file_name: file.name,
      size: file.size,
      mime_type: file.type || 'application/octet-stream',
      permission_tags: permissionTags,
    }),
  })

  const putResponse = await fetch(initialized.presigned_url, {
    method: 'PUT',
    headers: { 'Content-Type': initialized.mime_type },
    body: file,
  })
  if (!putResponse.ok) {
    throw new Error(`COS 文件上传失败 (${putResponse.status})`)
  }

  return requestJson<UploadSession>(
    `/api/documents/uploads/${initialized.id}/complete`,
    { method: 'POST' },
  )
}
