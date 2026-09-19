"""RAG 工作流节点导出包。

【模块职责说明】：
1. 节点模块聚合与门面导出（Facade & Aggregation）：
   作为 `app.workflows.nodes` 子包的统一导出入口，收敛并对外暴露工作流所必需的各个独立节点函数，
   消除外部对深层子模块文件的碎片化引用路径（例如避免在外部编写 `from app.workflows.nodes.load_context import ...`）。
2. 公开符号显式约定（Public API Contract via `__all__`）：
   显式定义 `__all__` 清单，严格限定该子包对外公开的函数集合，
   防止内部未导出的私有工具函数或冗余导入被误引入，优化 IDE 自动补全与类型推断体验。
"""

# 从各个独立节点模块中引入对应的节点执行函数
from app.workflows.nodes.generate import stream_generate
from app.workflows.nodes.load_context import load_context
from app.workflows.nodes.normalize_query import normalize_query
from app.workflows.nodes.retrieve import retrieve
from app.workflows.nodes.route_query import route_query

# 显式声明模块公开导出的符号清单
__all__ = [
    "load_context",     # 1. 历史消息加载节点
    "normalize_query",  # 2. Query 标准化与意图透传节点
    "route_query",      # 3. 查询优化策略路由节点（判定并按策略产出最终检索词）
    "retrieve",         # 4. 向量检索与拒答熔断节点
    "stream_generate",  # 5. 大模型流式输出生成节点
]