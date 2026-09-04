import { createBrowserRouter } from 'react-router-dom'
import { BasicLayout } from '@/layouts/BasicLayout'
import { HomePage } from '@/pages/HomePage'
import { DocumentsPage } from '@/pages/DocumentsPage'
import { DocumentDetailPage } from '@/pages/DocumentDetailPage'
import { ChatPage } from '@/pages/ChatPage'
import { EvaluationListPage } from '@/pages/EvaluationListPage'
import { EvaluationDetailPage } from '@/pages/EvaluationDetailPage'
import { LoginPage } from '@/pages/LoginPage'
import { UsersPage } from '@/pages/UsersPage'
import { RolesPage } from '@/pages/RolesPage'
import { RequireAuth } from '@/components/RequireAuth'
import { RequireAdmin } from '@/components/RequireAdmin'

export const router = createBrowserRouter([
  // /login 单独路由，不进 BasicLayout / RequireAuth
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: <RequireAuth />,
    children: [
      {
        element: <BasicLayout />,
        children: [
          { index: true, element: <HomePage /> },
          { path: 'documents', element: <DocumentsPage /> },
          { path: 'documents/:id', element: <DocumentDetailPage /> },
          { path: 'chat', element: <ChatPage /> },
          // admin only
          {
            element: <RequireAdmin />,
            children: [
              { path: 'evaluation', element: <EvaluationListPage /> },
              { path: 'evaluation/runs/:id', element: <EvaluationDetailPage /> },
              { path: 'users', element: <UsersPage /> },
              { path: 'roles', element: <RolesPage /> },
            ],
          },
        ],
      },
    ],
  },
])
