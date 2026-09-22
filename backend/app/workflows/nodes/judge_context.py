"""judge_context：上下文质量裁决节点（裁判员角色）。

【模块核心职责】：
提取精排/召回候选集中 Top1（最相关）切片的分数与预设安全阈值比对，
输出系统级布尔判据 `context_is_enough`，作为最终是否直接触发拒答的硬门槛。

【为什么设计为纯规则节点？】：
本节点**不发起任何大模型调用**，仅做纯数学分值比对。
原因在于：精排模型的打分已经是“Query 与 Chunk 成对 Cross-Encoder 语义相关性”的最高质量量化结果，
用确定性阈值硬截断最安全，若再引入 LLM 判定只会凭空增加响应耗时与幻觉不确定性。

【与 observe_context 节点的关键职责边界】：
- observe_context（探索推进器）：判定“当前信息够不够，要不要继续循环检索/重写 Query”
  -> 输出 `context_sufficient`，用于驱动工作流图中的动态循环条件边。
- judge_context（最终裁决者）：判定“最终交付的内容能不能回答，要不要直接安全拒答”
  -> 输出 `context_is_enough`，作为流向大模型生成或是覆盖为安全模版的终审凭证。

【双分值、双阈值回退链体系】：
- 优先选择：`rerank_score` 与 `rerank_min_score` 比较（绝对相关度，判据最可靠）。
- 容错降级：缺失精排分时，回退到 `vector_score` 与 `retrieval_min_score` 比较（余弦相似度）。
- 避坑原则：两类分数模型量纲与分布不同，**绝不可交叉错位比对**。
"""

# 引入全局系统配置单例（读取两套分立的最低置信度阈值）
from app.core.config import settings
# 引入 RAG 工作流增量状态契约规范
from app.workflows.rag_state import RAGState


async def judge_context(state: RAGState) -> RAGState:
    """评估切片上下文是否足以支撑生成，输出最终裁决标识 context_is_enough。

    三级递进降级判定流水线（if / elif / else）：
    ① 优先级 1（高置信度模式）：存在精排打分 -> 比对精排置信度阈值（首选基准）
    ② 优先级 2（容错回退模式）：无精排打分但有向量分 -> 回退比对向量相似度阈值（降级兜底）
    ③ 优先级 3（安全保守模式）：两者皆无（如仅靠词频/BM25命中的脏数据） -> 直接判为不足

    :param state: 工作流当前上下文字典，要求包含 retrieved_chunks 列表
    :return: 状态增量字典，仅返回修改后的 {"context_is_enough": bool}
    """
    chunks = state.get("retrieved_chunks", [])

    # -------------------------------------------------------------------------
    # 【守卫分支 0：空候选快速熔断】
    # 检索层未捞到任何切片 -> 无任何事实依据，毫无疑问直接裁定不足
    # -------------------------------------------------------------------------
    if not chunks:
        return {"context_is_enough": False}

    # 上游 rerank 节点已完成降序排序，取首项（Index 0）即代表置信度最高的切片
    top = chunks[0]

    # -------------------------------------------------------------------------
    # 【三级置信度裁判梯队】
    # -------------------------------------------------------------------------
    if top.rerank_score is not None:
        # 分支 ①：精排链路正常生效（最信任模式）
        # 提取 top.rerank_score 与 settings.rerank_min_score（如 0.3）做布尔比较。
        # ⚠️ 注意：绝不能拿它去比向量阈值，两者的数值范围与分布曲线截然不同。
        is_enough = top.rerank_score >= settings.rerank_min_score

    elif top.vector_score is not None:
        # 分支 ②：精排不可用时的降级模式（短路触发或服务抖动）
        # 触发场景：候选切片不足2条被短路、未开启精排开关、或者远端精排请求超时异常。
        # 此时退回使用向量余弦相似度 top.vector_score 与 settings.retrieval_min_score（如 0.6）比较。
        # 策略：缺失了精排复核，该阈值通常设定得更为收敛严格。
        is_enough = top.vector_score >= settings.retrieval_min_score

    else:
        # 分支 ③：无可用数值语义分数（最低安全兜底）
        # 触发场景：纯全文关键词/BM25初筛命中但未走向量评分，缺乏可靠语义量化支撑。
        # 策略：宁缺毋滥，采取防御性编程策略，直接判定不足以回答，防止幻觉。
        is_enough = False

    # 按照 LangGraph 增量更新规范仅返回目标状态字段，避免覆写无关数据
    return {"context_is_enough": is_enough}