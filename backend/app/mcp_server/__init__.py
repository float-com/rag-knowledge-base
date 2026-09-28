"""MCP Server 入口模块。

把知识库的核心能力（问答 / 文档上传 / 列表 / 状态查询 / 统计）按 MCP 标准
工具协议暴露给外部 Agent。鉴权复用上一期的 JWT，工具实现一律调
`app.services.*` 与 `app.workflows.*`，不重写业务逻辑。

【本模块只做一件事：对外交出一个实例】
其余模块一律 `from app.mcp_server import knowledge_mcp`，
不直接接触 `app.mcp_server.server`，也不自己建第二个 FastMCP 实例 ——
多一个实例就多一份工具表，两边会漂移。

【为什么 `__all__` 只列 knowledge_mcp】
第 9 节的 `app/main.py` 要用两种方式使用这个实例：
    knowledge_mcp.session_manager.run()       生命周期里管会话管理器
    knowledge_mcp.streamable_http_app()       装配时取 ASGI 子应用
把实例本身作为唯一出口，这两件事都能做；
若这里额外导出 `streamable_http_app` 之类的函数，就会鼓励别处去"再包装一层"。
"""

from app.mcp_server.server import knowledge_mcp

__all__ = ["knowledge_mcp"]
