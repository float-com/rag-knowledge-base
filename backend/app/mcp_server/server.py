"""FastMCP 实例与工具注册入口。

部署模式：

- `stateless_http=True`：每次工具调用独立处理，不维护跨请求 session；
  与 RAG 一次性问答语义天然契合，方便横向扩容
- `json_response=True`：响应直接 JSON，不走 SSE，降低浏览器 / Agent 客户端集成成本
- `streamable_http_path="/"`：让最终挂载路径为 `/mcp` 而非 `/mcp/mcp`（默认行为反直觉）

================================================================================
【步骤 1】为什么实例建在这里，而不是 tools.py 里
================================================================================

tools.py 只负责"有哪些工具"，它对"工具挂在哪个实例、叫什么名字"一无所知 ——
`register_tools(mcp)` 把实例当参数收进去（第 5 章的设计）。
本模块则是**唯一**创建实例的地方，并把注册动作在模块级完成，
于是 `import` 一次就得到一个"工具已经挂好"的实例。

================================================================================
【步骤 2】三个开关各自解决什么（都是部署形态问题，不是业务问题）
================================================================================

stateless_http=True
    每次 POST 独立成一次调用，服务端不保留跨请求会话状态。
    对本项目尤其合适：问答是"一次调用一个结果"，本来就没有多轮服务端状态
    （第 4 章 `answer_for_mcp` 甚至强制把 chat_history 置空）。
    ⚠️ 代价：MCP 的 session 级能力（如服务端主动推送、进度订阅）不可用 —— 本项目不需要。

json_response=True
    Streamable HTTP 允许用 SSE 流式回包，但我们的 5 个工具都是"一次调用一个结果"，
    流式只会给客户端增加解析成本（浏览器 fetch / Agent SDK 都要处理 event-stream）。
    所以直接返回 JSON。

streamable_http_path="/"
    ★ 这是最容易踩的一个默认值。FastMCP 默认把 streamable HTTP 端点挂在 `/mcp`，
    而我们要 `app.mount("/mcp", ...)`；两者一叠就变成 **`/mcp/mcp`**。
    把内层路径改成 `/` 之后，外层 mount 前缀才是唯一决定路径的地方 → 最终就是 `/mcp`。
    （第 9 节挂载时还有一行注释专门标注这一点。）

================================================================================
【步骤 3】为什么不需要在这里配 session_maker
================================================================================

工具函数各自 `async with AsyncSessionLocal()` 开短生命周期会话（第 2/5 章的约定），
FastMCP 不持有数据库连接，也就不需要注入会话工厂。
"""

from mcp.server.fastmcp import FastMCP

from app.mcp_server.tools import register_tools

knowledge_mcp = FastMCP(
    name="rag-knowledge-base",
    # instructions 是写给【模型】看的说明书：客户端连上后，模型据此判断
    # "这个 server 能干什么、该先调哪个工具"。所以它要写成动作指南，而不是功能清单。
    instructions=(
        "知识库工具集：先调 ask_knowledge_base 直接问答；"
        "调 list_documents / get_document_status / get_knowledge_base_stats 了解库内文档；"
        "管理员可调 upload_document 上传新文件。"
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)

register_tools(knowledge_mcp)
