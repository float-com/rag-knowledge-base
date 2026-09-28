# 冒烟测试脚本（部署前回归用）

> 期：**Day13 · MCP Server 集成（收尾）**
> 归档日期：2026.09.28
> 来源：本目录原本在 `backend/.smoke/`，为避免测试产物散落在代码目录里，已移入归档。
> 说明：这 5 个脚本是**真实对运行中的服务发请求**的冒烟测试，不是单元测试。

---

## 一、前提条件

```
① 后端已在 http://127.0.0.1:8000 运行（uvicorn app.main:app）
② PostgreSQL 与 Redis 容器 healthy（docker ps）
③ 根目录 .env 已填：DATABASE_URL / JWT_SECRET / DEFAULT_ADMIN_PASSWORD
   以及 COS_* / EMBEDDING_* / CHAT_*（第 2、3 段会真实调用它们）
④ 用后端虚拟环境的 Python 跑（需要 httpx / mcp / redis 等依赖）
⑤ 库内至少有一篇 status=ready 的文档（第 1、3 段要看列表与检索）
```

跑法（在 `backend` 目录下，或把脚本拷回去）：

```powershell
cd backend
.\.venv\Scripts\python.exe -B -X utf8 -W ignore <脚本路径>
```

更省事的写法（直接从归档目录跑，路径自带）：

```powershell
backend\.venv\Scripts\python.exe -B -X utf8 -W ignore `
  "项目阶段性总结\Day13_MCP Server 集成\02_2026.9.28\07_上线前检查单\001_冒烟测试脚本\test_1_static_auth_read.py"
```

> 脚本用 `Path(__file__).resolve().parents[5]` 定位项目根来读 `.env`。
> **这两件事都实测过**：归档目录的深度是
> `项目阶段性总结/Day13_MCP Server 集成/02_2026.9.28/07_上线前检查单/001_冒烟测试脚本/`，
> 从脚本文件往上数第 5 层才是项目根（第 2 层只到 `02_2026.9.28`）。
> ⚠️ 若你把它移到别的深度，**必须同步改这一行**，否则读不到 `.env`（表现为用默认值连接）。

---

## 二、五段脚本分别做什么

| 脚本 | 覆盖内容 | 副作用 / 成本 |
| --- | --- | --- |
| `test_1_static_auth_read.py` | 健康三探针、OpenAPI 契约（paths/operations/operationId）、登录与四种无效凭证、文档列表/详情/切片、会话列表、管理面可访问性 | **会创建一个探针用户** `smoke_probe`（无角色）→ 跑完请清理（见第四节） |
| `test_2_upload_permission.py` | 上传小文件 → Celery → 轮询到 ready；**权限过滤真对照**（造 `tags=['smoke_secret']` 的文档，验 admin 见 / alice 不见 / 404 / 403 / 放宽后 200）；删除复原 | 消耗一点点 embedding 额度；**会临时重置 alice 的口令**（见下） |
| `test_3_qa_mcp.py` | REST SSE 事件契约、语义缓存、限流顺序、**MCP 五个工具真实调用** + 三条拒绝路径 + 越权路径 | **消耗 LLM 与 embedding 额度**（问答 2 次 + MCP 问答 2 次 + MCP 上传 1 次） |
| `test_4_leads.py` | 追查前三段暴露的疑点：citations 载荷形状、缓存写入条件、统计口径与原始 SQL 对账；并清理临时文档与冒烟会话 | 问答 3 次（含 1 次缓存命中） |
| `test_5_fixes.py` | **本轮 5 项上线前修复的验证**：JWT 生产硬失败 6 场景、MCP 限流（阈值打桩 + 分桶隔离 + key 前缀）、生产编排渲染（端口未发布/服务名/ENVIRONMENT/模型卷）、`.env.example` 与 `.gitignore` 规则 | 不烧额度；会临时写 `rag:rate_limit:mcp-*` 的 key 并在结束时清理 |

---

## 三、⚠️ 两条必须知道的副作用

### 3.1 `test_2` 会重置 alice 的口令

脚本里硬编码了 Alice 的口令 `AliceSmoke!2026`（因为原口令未知，不重置就没法做权限对照）。
跑之前需要先重置 alice 的密码（用后端自己的哈希函数）：

```powershell
cd backend
$h = .\.venv\Scripts\python.exe -B -c "from app.core.security import hash_password; print(hash_password('AliceSmoke!2026'))"
docker exec rag-kb-postgres psql -U rag -d rag_kb -c "update users set password_hash='$($h.Trim())' where username='alice';"
```

**跑完记得改回你原来的口令**（同上命令，换掉口令字符串）。

> 若 alice 登录失败，`test_2` 会走 WARN 分支跳过权限对照，**不会误报 FAIL** ——
> 但那样最关键的一项就没测到，建议按上面步骤先准备好。

### 3.2 `test_1` 会留下 `smoke_probe` 用户

第 1 段为了验证"管理员可访问 /api/users"发了一次创建请求。

```powershell
docker exec rag-kb-postgres psql -U rag -d rag_kb -c "delete from users where username='smoke_probe';"
```

---

## 四、跑完的清理清单

```powershell
# ① 探针用户
docker exec rag-kb-postgres psql -U rag -d rag_kb -c "delete from users where username='smoke_probe';"

# ② 核对是否复原（正常应与跑前一致；本次基线为 21 篇 / 338 切片 / 5 会话 / 2 用户）
docker exec rag-kb-postgres psql -U rag -d rag_kb -t -c "select 'docs=' || count(*) from documents union all select 'chunks=' || count(*) from document_chunks union all select 'convs=' || count(*) from conversations union all select 'users=' || count(*) from users;"

# ③ 残留冒烟文档（脚本自身也会删，这是双保险）
docker exec rag-kb-postgres psql -U rag -d rag_kb -t -c "select count(*) from documents where name ilike '%smoke%';"

# ④ alice 口令改回
#    （见 3.1，换成你自己的口令再执行一次）
```

---

## 五、这 5 个脚本**没有**覆盖的

```
✗ 前端 tsc -b / 页面联调
✗ 并发与压测（多用户同时问答、同时上传）
✗ 大文件 / 超限文件 / MIME 不符等边界
✗ Celery 失败与重试路径（只走成功路径）
✗ SSE 断线重连 / 客户端中途断开
✗ 备份恢复
```

> 全项目目前**没有单元测试**，这 5 个冒烟脚本是唯一的自动化回归手段。
> 建议每次上线前按 1→2→3→5 的顺序跑（第 4 段是追查用的，日常可跳过）。
