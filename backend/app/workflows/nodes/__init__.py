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
from app.workflows.nodes.judge_context import judge_context
from app.workflows.nodes.load_context import load_context
from app.workflows.nodes.normalize_query import normalize_query
from app.workflows.nodes.observe_context import observe_context
from app.workflows.nodes.plan_retrieval import plan_retrieval
from app.workflows.nodes.refuse import refuse
from app.workflows.nodes.rerank import rerank
from app.workflows.nodes.retrieve import retrieve
from app.workflows.nodes.route_query import route_query

# 显式声明模块公开导出的符号清单
# 【顺序说明】按【图的执行顺序】排并编号，而非字母序：
# 它同时也充当读者理解整条链路的目录，按执行顺序排信息量更大。
# 注意 rerank 排在 retrieve 之后、judge_context 之前，与第 8 期的新链路一致。
__all__ = [
    "load_context",     # 1. 历史消息加载节点
    "normalize_query",  # 2. Query 标准化与意图透传节点
    "route_query",      # 3. 查询优化策略路由节点（判定并按策略产出最终检索词）
    "plan_retrieval",   # 4. Agentic 循环决策节点（决定本轮用什么 query / route）
    "retrieve",         # 5. 混合检索节点（第 8 期起只负责召回，不再判拒答）
    "observe_context",  # 6. Agentic 循环观察节点（判"要不要再试"，产出 context_sufficient）
    "rerank",           # 7. 精排节点（成对打分后截断到 retrieval_top_k）
    "judge_context",    # 8. 上下文裁判节点（判"能不能回答"，产出 context_is_enough）
    "refuse",           # 9. 统一拒答出口（所有拒答边收口到这里）
    "stream_generate",  # 10. 大模型流式输出生成节点
]