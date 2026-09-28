# 07_FastMCP 实例与主应用挂载

> 期：**Day13 · MCP Server 集成**
> 章：**第 8 章 创建 FastMCP 实例 / 第 9 章 挂载到 FastAPI 主应用**
> 模块：`backend/app/mcp_server/server.py`（新增）
> 　　　`backend/app/mcp_server/__init__.py`（修改）
> 　　　`backend/app/main.py`（修改：lifespan 一行 + 挂载一行）
> 记录日期：2026.09.28
> 本篇定位：**复习材料** —— 代码逐段讲解 + 真机握手实测 + 排错速查。
> 归档目录：`Day13_MCP Server 集成/02_2026.9.28/06_backend_app_mcp_server_server`

---

## 〇、三分钟速览（先读这一节）

```
这两章要解决的一件事：
        把已经写好的 5 个工具，变成一个"外部 Agent 能通过 HTTP 调用"的服务。

三段工作：
        第 8 章  server.py      建 FastMCP 实例 + 调 register_tools 完成注册
        第 8 章  __init__.py    只对外暴露这一个实例
        第 9 章  main.py        lifespan 里启动 session_manager + app.mount("/mcp", ...)

本章最该记住的三句：
        ① streamable_http_path="/" 是为了让最终路径是 /mcp，而不是 /mcp/mcp
        ② session_manager.run() 必须放进 lifespan —— 少一行，所有工具调用都抛 RuntimeError
        ③ 挂载用 mount 而不是 include_router：MCP 子应用自带路径体系与生命周期
```

---

## 一、第 8 章：`server.py` 的三步

### 1.1 步骤 1：为什么实例建在这里，而不是 `tools.py` 里

```
tools.py          只负责"有哪些工具"（register_tools(mcp) 接收实例）
server.py         ★ 唯一创建实例的地方
__init__.py       只把实例转出去
```

`tools.py` 对"挂在哪个实例、叫什么名字"一无所知（第 5 章的设计），
本模块是**唯一**创建实例的地方，并在模块级完成注册 ——
于是 `from app.mcp_server import knowledge_mcp` 一次就拿到"工具已经挂好"的实例。

### 1.2 步骤 2：三个开关各自解决什么

```python
knowledge_mcp = FastMCP(
    name="rag-knowledge-base",
    instructions=(...),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)
```

| 开关 | 作用 | 为什么本项目要它 |
| --- | --- | --- |
| `stateless_http=True` | 每次工具调用独立处理，不维护跨请求 session | 与 RAG 一次性问答语义天然契合（第 4 章甚至强制 `chat_history = []`），且方便横向扩容。⚠️ 代价：MCP 的 session 级能力（服务端主动推送、进度订阅）不可用 —— 本项目不需要 |
| `json_response=True` | 响应直接 JSON，不走 SSE | 5 个工具都是"一次调用一个结果"；流式只会给客户端增加解析成本（浏览器 fetch / Agent SDK 都要处理 event-stream） |
| `streamable_http_path="/"` | ★ 让**内层**端点挂在根路径 | FastMCP 默认把 streamable HTTP 端点挂在 `/mcp`，而第 9 节要 `app.mount("/mcp", ...)`；两者一叠就是 **`/mcp/mcp`**。改成 `/` 后，外层 mount 前缀才是唯一决定路径的地方 |

`instructions` 是**写给模型看的说明书**（客户端连上后模型据此判断该先调哪个工具），
所以写成动作指南而不是功能清单：

```
知识库工具集：先调 ask_knowledge_base 直接问答；
调 list_documents / get_document_status / get_knowledge_base_stats 了解库内文档；
管理员可调 upload_document 上传新文件。
```

### 1.3 步骤 3：为什么这里不配 `session_maker`

工具函数各自 `async with AsyncSessionLocal()` 开短生命周期会话（第 2/5 章的约定），
FastMCP 不持有数据库连接，也就不需要注入会话工厂。

> 📌 实测确认：本项目装的 fastmcp 2.14.7 里，`FastMCP.__init__` **没有** `session_maker` 参数
> （参数表里是 name/instructions/stateless_http/json_response/streamable_http_path/…），
> 所以"不配"既是不需要，也是这一版本来就没有。

### 1.4 `__init__.py`：只暴露一个实例

```python
from app.mcp_server.server import knowledge_mcp

__all__ = ["knowledge_mcp"]
```

**为什么只导出实例本身而不是外加几个函数**：第 9 节要用它做两件事 ——
`session_manager.run()`（生命周期里）与 `streamable_http_app()`（装配时取子应用）。
把实例作为唯一出口，这两件事都能做；若额外导出包装函数，就会鼓励别处"再包一层"。

---

## 二、第 9 章：挂到 FastAPI 上的两处改动

### 2.1 改动一：lifespan 里启动 session manager

```python
async with knowledge_mcp.session_manager.run():
    yield
```

| 要点 | 说明 |
| --- | --- |
| **为什么必需** | `session_manager.run()` 是 FastMCP streamable HTTP 必需的后台任务组；少了它，任何工具调用都会因 ASGI scope 缺失抛 `RuntimeError` |
| **为什么要绑进 lifespan** | 它负责清理跨请求的超时连接；`async with` 让它与"应用启动 → 关闭"同生命周期，退出时才有收尾时机 |
| **为什么 `yield` 要挪进 with 内部** | 原代码 `yield` 是裸的；现在整个服务运行期都在 manager 的上下文里 —— 这正是"启动期间维护"的含义 |

### 2.2 ★ 一个只有实测才知道的细节：`session_manager` 是懒创建的

实测（fastmcp 2.14.7 / mcp 1.30.0）：

```
在 streamable_http_app() 被调用之前访问 session_manager
    → RuntimeError: Session manager can only be accessed after calling streamable_http_app().
      The session manager is created lazily to avoid unnecessary initialization.
```

**为什么本项目不会踩到**：

```
import app.main
    → from app.mcp_server import knowledge_mcp     （导入实例）
    → create_app()
        → knowledge_mcp.streamable_http_app()      ← 这里创建 session_manager
    → 服务启动 → lifespan 执行
        → knowledge_mcp.session_manager.run()      ← 此时早已就绪
```

**顺序天然是对的**：`streamable_http_app()` 在**模块导入期**（`create_app` 内）就被调用，
而 lifespan 要等服务开始启动才执行。

### 2.3 ★ 第二个实测细节：多次取子应用 = 多个 app，但只有一个 manager

```
knowledge_mcp.streamable_http_app() is knowledge_mcp.streamable_http_app()
    → False        （每次返回新的 Starlette 实例）
knowledge_mcp.session_manager is knowledge_mcp.session_manager
    → True         （同一个缓存对象）
```

所以"装配处取 app、lifespan 处取 manager"这种分工是**安全**的 ——
不会出现两个 manager 各管一半连接的情况。
**但这条约束值得记住**：若将来有人把 `streamable_http_app()` 的返回值缓存起来复用，
拿到的会是"另一个 app、同一个 manager"，行为仍然正确；反之若 manager 不缓存，
本节这套写法立刻就会崩。

### 2.4 改动二：挂载子应用

```python
app.mount("/mcp", knowledge_mcp.streamable_http_app(), name="mcp")
```

| 决策 | 理由 |
| --- | --- |
| **为什么 `mount` 而不是 `include_router`** | `mount` 挂的是**完整 ASGI 子应用**（自带路由表与生命周期）；`include_router` 挂的是本应用内的路由表。MCP 子应用有自己的路径体系，所以必须 mount |
| **为什么最终路径是 `/mcp`** | 内层已被第 8 节设为 `/`，前缀只由这里的 `/mcp` 决定。（两处要一起想，否则很容易出现 `/mcp/mcp` 这种"看起来能连、实际 404"） |
| **为什么挂在所有 `/api` 路由之后** | 第 1 章定的回归底线 R1：挂载 MCP 不得影响 `/api/*` 的 OpenAPI 文档。放最后一眼就能看出"/api 一条没动，只是多挂了一个独立子应用" |

---

## 三、★ 真机验证（这是第 1 章验收标准里的一条硬要求）

第 1 章 §7.4 写过："协议层的东西，自己造的假客户端不算数。"所以本次**起了真实服务、用真实客户端连**：

```
uvicorn 起在 127.0.0.1:8932（lifespan=on）
客户端：mcp.client.streamable_http.streamablehttp_client + mcp.ClientSession

握手成功 | server = rag-knowledge-base | protocol = 2025-11-25
tools/list 返回 5 个：
    ask_knowledge_base        知识库问答
    upload_document           上传文档
    list_documents            列出文档
    get_document_status       查询文档状态
    get_knowledge_base_stats  知识库概览

不带 token 调 list_documents
    → isError = True
    → 内容: Error executing tool list_documents: 请先登录
```

**这一条同时验证了四件事**：

| # | 验证点 | 结果 |
| --- | --- | --- |
| 1 | 挂载路径正确（不是 `/mcp/mcp`） | ✅ 客户端连 `/mcp` 即握手成功 |
| 2 | `session_manager.run()` 真的生效 | ✅ 没有抛 "ASGI scope 缺失" 的 RuntimeError |
| 3 | 5 个工具都注册成功且 schema 可被客户端解析 | ✅ tools/list 拿到完整 5 条 |
| 4 | 鉴权链路闭环（第 2 章 → 第 8/9 章） | ✅ 无 token 被 `resolve_current_user` 挡下，文案是"请先登录" |

### 3.1 ⚠️ 实测中发现的一处细节：307 重定向

客户端日志里每次调用都有一次跳转：

```
POST http://127.0.0.1:8932/mcp   → 307 Temporary Redirect
POST http://127.0.0.1:8932/mcp/  → 200 OK
```

**原因**：内层子应用挂在 `/`，Starlette 的 mount 对"根路径"的规范形式是带尾斜杠（`/mcp/`），
所以访问 `/mcp` 会先 307 到 `/mcp/`。客户端（httpx / MCP SDK）会自动跟随，**功能不受影响**。

**要留意的是**：如果将来有网关 / 反向代理不跟随重定向，或客户端被配置成不跟随，
就会看到"连不上"却找不到原因。真要避免，可以把实例的内层路径改成 `/mcp`
（即去掉 `streamable_http_path="/"`）再 `mount` 到 `/` 之外的前缀上 ——
但那样 `mount` 的前缀规则要重新算一遍，**本项目按教程保留现状并在此留档**。

---

## 四、回归验证

```
py_compile                 通过（main.py / server.py / __init__.py）
主应用 mounts              [('/mcp', 'mcp')]     ← 名称与路径都对
实例名                     rag-knowledge-base
子应用内层路由             ['/']                 ← 所以最终是 /mcp 而不是 /mcp/mcp
OpenAPI                   paths 29 / operations 40（未变，第 1 章 R1）
```

---

## 五、与教程的对照

| 教程内容 | 落地情况 |
| --- | --- |
| `server.py` 的模块 docstring（部署模式三条） | ✅ 一致，并逐条补了"为什么要它" |
| 三个构造参数与 `instructions` 文案 | ✅ 逐字一致 |
| `register_tools(knowledge_mcp)` | ✅ 一致 |
| `__init__.py` 只导出 `knowledge_mcp` | ✅ 一致 |
| lifespan 里 `async with knowledge_mcp.session_manager.run(): yield` | ✅ 一致 |
| `app.mount("/mcp", knowledge_mcp.streamable_http_app(), name="mcp")` | ✅ 一致 |
| 教程未展开、我补上的 | ① 为什么实例建在 server.py 而不是 tools.py；② 三个开关各自的作用；③ **session_manager 懒创建的实测**；④ **多 app 单 manager 的实测**；⑤ mount vs include_router；⑥ 307 重定向的成因与影响 |

---

## 六、排错速查表

| 现象 | 最可能的原因 | 先查哪里 |
| --- | --- | --- |
| 工具调用抛 `RuntimeError`（提到 ASGI scope） | lifespan 里少了 `session_manager.run()` | `app/main.py` 的 lifespan |
| 访问 `/mcp` 404，但 `/mcp/mcp` 能通 | `streamable_http_path` 没设成 `"/"` | `server.py` 的构造参数 |
| 启动时报 `RuntimeError: Session manager can only be accessed after...` | 在 `streamable_http_app()` 之前访问了 `session_manager`（例如别处提前 import 了 manager） | 谁先碰的 `session_manager` |
| `/api/*` 的 OpenAPI 文档变了 | 挂载影响了路由（理论上不会，mount 是独立子应用） | 第 1 章 R1：paths 应为 29 |
| 客户端连上但工具列表为空 | `register_tools` 没被调用，或调在了别的实例上 | `server.py` 末尾那一行 |
| 网关后连不上，直连正常 | 307 重定向被网关吞了 | 第三节 3.1 |
| 工具调用返回英文报错 | 异常不是 `AppException`，走了 `_to_tool_error` 的兜底 | 第 5 章的错误出口 |

---

## 七、自查 5 题

1. 为什么 FastMCP 实例建在 `server.py` 而不是 `tools.py`？`__init__.py` 为什么只导出一个名字？
2. `streamable_http_path="/"` 是在解决什么问题？不设会发生什么？
3. `session_manager.run()` 为什么必须放进 lifespan？少了它会怎样？
4. `session_manager` 为什么是懒创建的？本项目的导入/装配顺序为什么天然安全？
5. 为什么用 `app.mount` 而不是 `app.include_router`？挂载位置为什么放最后？

---

## 八、本章实际新增或修改文件

```
后端
├── backend/app/mcp_server/server.py          【新增】★ 第 8 章正文
│                                                      FastMCP 实例 + register_tools 调用
├── backend/app/mcp_server/__init__.py        【修改】改为只导出 knowledge_mcp
└── backend/app/main.py                       【修改】+ knowledge_mcp 导入
                                                      + lifespan 里 session_manager.run()
                                                      + app.mount("/mcp", ...)

项目阶段性总结
└── Day13_MCP Server 集成/02_2026.9.28/06_backend_app_mcp_server_server/
    ├── 07_FastMCP实例与主应用挂载.md          【本文件】
    └── 上传日志.md
```
