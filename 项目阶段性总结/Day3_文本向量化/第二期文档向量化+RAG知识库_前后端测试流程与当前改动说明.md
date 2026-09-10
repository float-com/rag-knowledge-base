# RAG 知识库前后端测试流程与当前改动说明

更新时间：2026 年 9 月 10 日

项目路径：

```text
D:\code\rag-knowledge-base
```

## 一、当前项目状态

当前项目已经完成文档模块的基础前后端联调准备，包含：

- 前端文档管理页面
- 文档上传功能
- 文档列表查询
- 文档详情查询
- 文档文件下载与预览
- 文档切片列表与切片详情查询
- 文档解析、切分、向量化和入库流水线
- PostgreSQL 与 pgvector 数据库迁移
- 腾讯云 COS 文件存储
- 百炼 Embedding 向量化服务

当前项目暂时没有完整实现后端登录认证接口，因此本地测试阶段使用了临时权限绕过方式。

## 二、当前临时改动

### 1. 临时绕过登录守卫

文件：

```text
D:\code\rag-knowledge-base\frontend\src\components\RequireAuth.tsx
```

当前将原登录校验逻辑整体保留为注释，并临时直接返回页面内容：

```tsx
return <Outlet />
```

作用：

- 访问 `/documents` 时不再强制跳转到登录页
- 方便直接进行前端页面和文档接口联调

注意：

- 该方式仅用于本地开发测试
- 正式环境必须恢复登录校验

### 2. 临时开启管理员权限

文件：

```text
D:\code\rag-knowledge-base\frontend\src\pages\DocumentsPage.tsx
```

当前原始权限判断已保留并注释：

```tsx
// const isAdmin = useAuthStore((s) => Boolean(s.user?.isAdmin))
```

当前临时使用：

```tsx
const isAdmin = true
```

作用：

- 显示上传文档按钮
- 显示删除按钮
- 显示失败文档的重试按钮
- 显示管理员相关操作区域

### 3. 修复权限标签为空导致的前端崩溃

文件：

```text
D:\code\rag-knowledge-base\frontend\src\pages\DocumentsPage.tsx
```

原始逻辑要求后端一定返回 `permission_tags`，当后端没有返回该字段时，执行 `tags.length` 会导致页面报错。

当前临时逻辑为：

```tsx
render: (tags: string[] = []) =>
```

作用：

- 当后端没有返回权限标签时，自动使用空数组
- 页面显示为公开文档
- 避免出现 Cannot read properties of undefined 相关错误

原始渲染逻辑已经注释保留，没有删除。

### 4. 调整向量批量大小

文件：

```text
D:\code\rag-knowledge-base\.env
```

当前配置：

```env
EMBEDDING_BATCH_SIZE=10
```

原因：

百炼接口返回过批量数量错误，要求单次请求数量不能超过 10。原来的 16 会导致上传后的向量化阶段失败。

## 三、启动前准备

### 1. 检查软件环境

需要准备：

- Docker Desktop
- Python 3.12 或更高版本
- uv
- Node.js
- npm

检查命令：

```powershell
docker --version
python --version
uv --version
node --version
npm --version
```

### 2. 检查环境变量

确认项目根目录存在：

```text
D:\code\rag-knowledge-base\.env
```

至少需要确认以下配置：

```env
DATABASE_URL=postgresql+asyncpg://rag:rag@localhost:5432/rag_kb
COS_SECRET_ID=已配置
COS_SECRET_KEY=已配置
COS_REGION=已配置
COS_BUCKET=已配置
EMBEDDING_API_KEY=已配置
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_DIM=1024
EMBEDDING_BATCH_SIZE=10
UPLOAD_MAX_SIZE_MB=50
CHUNK_SIZE=600
CHUNK_OVERLAP=60
```

不要把 `.env` 提交到 GitHub。真实的 COS 密钥和 Embedding 密钥不要出现在截图、提交记录或公开仓库中。

## 四、启动数据库

在项目根目录打开终端：

```powershell
cd D:\code\rag-knowledge-base
docker compose up -d postgres
```

检查容器：

```powershell
docker ps
```

应该能看到：

```text
rag-kb-postgres
```

如果需要查看数据库日志：

```powershell
docker logs rag-kb-postgres
```

## 五、执行数据库迁移

打开第二个终端：

```powershell
cd D:\code\rag-knowledge-base\backend
uv sync
uv run alembic upgrade head
```

迁移成功后，数据库中应该存在：

- documents
- document_chunks
- vector 扩展

查看当前迁移版本：

```powershell
uv run alembic current
```

## 六、启动后端

在 backend 目录执行：

```powershell
cd D:\code\rag-knowledge-base\backend
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

后端地址：

```text
http://localhost:8000
```

Swagger 接口文档：

```text
http://localhost:8000/docs
```

## 七、后端基础测试

### 1. 测试应用健康状态

```powershell
curl.exe http://localhost:8000/api/health
```

预期结果：

```json
{"status":"ok","detail":null}
```

### 2. 测试数据库连接

```powershell
curl.exe http://localhost:8000/api/health/db
```

预期结果中的状态应为正常。

### 3. 测试 COS 连接

```powershell
curl.exe http://localhost:8000/api/health/cos
```

如果 COS 配置正确，预期结果中的状态应为正常。

### 4. 测试文档列表接口

```powershell
curl.exe "http://localhost:8000/api/documents?page=1&page_size=20"
```

首次测试没有文档时，预期结果类似：

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 20
}
```

## 八、启动前端

打开第三个终端：

```powershell
cd D:\code\rag-knowledge-base\frontend
npm install
npm run dev
```

前端地址：

```text
http://localhost:5173
```

文档管理页面：

```text
http://localhost:5173/documents
```

如果修改了前端代码但浏览器没有更新：

1. 按 Ctrl + F5 强制刷新
2. 关闭当前页面后重新打开
3. 必要时停止前端终端，再重新执行 npm run dev

## 九、前端上传测试流程

### 1. 打开文档管理页面

访问：

```text
http://localhost:5173/documents
```

页面正常时应看到：

- 文档管理标题
- 上传文档按钮
- 刷新按钮
- 状态筛选框
- 文档列表

### 2. 选择文件

点击上传文档，选择以下格式之一：

- PDF
- DOCX
- Markdown
- HTML

建议第一次使用一个较小的 Markdown 文件测试，例如几 KB 到几十 KB。

### 3. 权限标签

第一次测试时可以不填写权限标签，代表公开文档。

### 4. 提交上传

点击弹窗中的确定或上传按钮。

正常情况下会看到上传成功提示，文档进入列表。

状态一般会经历：

```text
上传中
解析中
索引中
已就绪
```

页面会自动定时刷新处理中状态。

## 十、后端接口上传测试

也可以不经过前端，直接使用接口测试。

准备一个测试文件，例如：

```text
D:\test\demo.md
```

执行：

```powershell
curl.exe -X POST "http://localhost:8000/api/documents" -F "file=@D:\test\demo.md"
```

上传成功后会返回文档信息，记录返回结果中的：

```text
id
```

## 十一、查询文档状态

将 DOCUMENT_ID 替换为实际文档 ID：

```powershell
curl.exe "http://localhost:8000/api/documents/DOCUMENT_ID"
```

重点查看：

- status
- error_message
- created_at
- updated_at

状态说明：

- uploading：文件已上传，等待处理
- parsing：正在解析文件
- indexing：正在切片、向量化并写入数据库
- ready：处理完成
- failed：处理失败，需要查看 error_message

## 十二、测试切片接口

查询文档切片：

```powershell
curl.exe "http://localhost:8000/api/documents/DOCUMENT_ID/chunks?page=1&page_size=20"
```

正常处理完成后，返回内容中应包含：

- items
- total
- page
- page_size
- stats

其中：

- items 是切片摘要列表
- stats 是切片数量、平均长度、最小长度和最大长度统计

从 items 中复制一个切片 ID，再查询详情：

```powershell
curl.exe "http://localhost:8000/api/documents/DOCUMENT_ID/chunks/CHUNK_ID"
```

详情接口应返回完整切片正文。

## 十三、测试原文件预览和下载

内联预览：

```powershell
curl.exe -i "http://localhost:8000/api/documents/DOCUMENT_ID/file?download=0"
```

强制下载：

```powershell
curl.exe -i "http://localhost:8000/api/documents/DOCUMENT_ID/file?download=1"
```

预期行为：

- PDF、HTML、Markdown 可以尝试浏览器内联预览
- DOCX 强制下载
- 中文文件名不会出现乱码

## 十四、处理失败文档

如果文档状态是 failed：

### 1. 先查看错误原因

```powershell
curl.exe "http://localhost:8000/api/documents/DOCUMENT_ID"
```

查看 error_message 字段。

### 2. 向量批量数量错误

如果错误内容包含 batch size is invalid：

确认 `.env` 中存在：

```env
EMBEDDING_BATCH_SIZE=10
```

修改配置后必须重启后端。

### 3. 前端点击重试

因为当前使用了临时管理员权限，文档列表中可以看到重试按钮。

点击失败文档右侧的重试按钮，等待状态重新进入：

```text
上传中 → 解析中 → 索引中 → 已就绪
```

也可以使用接口：

```powershell
curl.exe -X POST "http://localhost:8000/api/documents/DOCUMENT_ID/retry"
```

## 十五、常见问题排查

### 1. 页面显示 404 Not Found

检查后端是否启动，并访问：

```text
http://localhost:8000/docs
```

确认 Swagger 中是否存在：

```text
/api/documents
```

### 2. 页面显示 502 Bad Gateway

通常是前端代理访问后端时，后端正在重启或没有启动。

依次检查：

```powershell
curl.exe http://localhost:8000/api/health
curl.exe "http://localhost:5173/api/documents?page=1&page_size=20"
```

如果后端接口正常，刷新前端页面即可。

### 3. 页面显示 Unexpected Application Error

打开浏览器控制台查看报错位置。

如果错误包含：

```text
Cannot read properties of undefined
```

重点检查接口返回字段是否为空，以及前端是否对数组字段设置默认值。

当前权限标签字段已经使用默认空数组处理：

```tsx
render: (tags: string[] = []) =>
```

### 4. 上传按钮不显示

确认：

```tsx
const isAdmin = true
```

确认修改的是：

```text
D:\code\rag-knowledge-base\frontend\src\pages\DocumentsPage.tsx
```

保存后执行 Ctrl + F5。

### 5. 上传后马上失败

依次检查：

- COS 配置是否有效
- Embedding API 密钥是否有效
- Embedding 模型是否正确
- EMBEDDING_BATCH_SIZE 是否不大于 10
- 后端终端是否出现异常日志
- PostgreSQL 是否正常运行

## 十六、当前测试结论标准

本轮文档模块测试成功的标准：

- 前端可以打开文档管理页面
- 页面显示上传文档按钮
- 可以选择并提交测试文件
- 文档列表可以看到新文档
- 文档状态可以从处理中变为已就绪
- 文档详情可以打开
- 原文可以预览或下载
- 切片列表可以查询
- 切片详情可以查看完整正文
- 失败文档可以重试
- 数据库中可以查询 documents 和 document_chunks 数据

## 十七、后续恢复正式登录的步骤

当前测试完成后，需要恢复正式权限逻辑。

### 1. 恢复 DocumentsPage 权限导入

恢复：

```tsx
import { useAuthStore } from "@/stores/authStore"
```

### 2. 恢复管理员判断

将：

```tsx
const isAdmin = true
```

恢复为：

```tsx
const isAdmin = useAuthStore((s) => Boolean(s.user?.isAdmin))
```

### 3. 恢复 RequireAuth.tsx

恢复原登录守卫代码，不再直接返回：

```tsx
return <Outlet />
```

### 4. 补充后端认证模块

后端需要实现并注册：

- POST /api/auth/login
- GET /api/auth/me
- 用户表
- admin 默认账号
- Token 生成
- Token 校验
- 用户权限和管理员角色判断

### 5. 删除本地测试 Token

浏览器控制台执行：

```javascript
localStorage.removeItem("rag-kb.auth.token")
localStorage.removeItem("rag-kb.auth.user")
location.reload()
```

## 十八、推荐的测试顺序

每次启动项目时，建议严格按照以下顺序：

1. 启动 Docker Desktop
2. 启动 PostgreSQL 容器
3. 执行数据库迁移
4. 启动后端
5. 测试后端健康检查
6. 测试文档列表接口
7. 启动前端
8. 打开文档管理页面
9. 上传小型 Markdown 文件
10. 查看文档状态
11. 查看文档切片
12. 测试预览和下载
13. 测试失败重试
14. 记录终端错误和浏览器控制台错误

