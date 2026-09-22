"""DashScope qwen3-rerank 精排客户端。

【模块职责】：
把「一堆候选切片 + 一个问题」交给精排模型，拿回每条候选的【成对相关性分】，
并按分数把候选重新排序。

【为什么单独写一个客户端，而不复用 app/llm/models.py】：
`models.py` 里封装的是 `langchain_openai.ChatOpenAI` 与 Embeddings —— 它们走的是
**OpenAI 兼容协议**（/chat/completions、/embeddings）。而百炼的 rerank 不在这个协议里，
端点是独立的 `{base}/reranks`，请求体与响应体结构也不同。
强行用 ChatOpenAI 去调它只会得到 404 / 400，因此这里【直接用 httpx 发 HTTP 请求】。

【为什么用 httpx 而不是官方 SDK】：
只需一次 POST，httpx 已经在本项目依赖里（第 3 期起就用于其它出网调用）；
引入 langchain-community 只为包一层 rerank 不划算。
"""

# 引入 dataclasses 模块本身（用 dataclasses.replace 产出"改了分的新切片"）
import dataclasses
# 引入 Any 用于标注解析出来的响应字段（JSON 结构不受我们控制）
from typing import Any

# 引入 httpx 异步客户端（精排是主链路上的同步调用，用异步客户端避免阻塞事件循环）
import httpx

# 引入全局系统配置单例（读 rerank 相关配置）
from app.core.config import settings
# 引入配置错误异常：API Key 缺失属于"部署配置问题"，应当明确抛出而不是静默降级
from app.core.exceptions import ConfigurationError
# 引入统一日志工厂
from app.core.logging import get_logger
# 引入检索切片数据契约
from app.retrieval.vector_retriever import RetrievedChunk

logger = get_logger(__name__)


class Reranker:
    """DashScope qwen3-rerank 客户端。

    单例持有 httpx.AsyncClient 复用连接池：rerank 是单次同步问答的一环，
    要求低延迟，所以超时设置得比 chat 短一些。
    """

    def __init__(self) -> None:
        # 延迟创建：不在 __init__ 里建客户端，避免模块导入阶段就建立连接池
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """获取（或惰性创建）共享的异步 HTTP 客户端。"""
        if self._client is None:
            # 【为什么在这里传 timeout 而不是每次 post 时传】：
            # 一个 AsyncClient 的超时设置对它的所有请求生效，集中一处更不容易漏改。
            self._client = httpx.AsyncClient(timeout=settings.rerank_timeout)
        return self._client

    async def rerank(
            self,
            query: str,
            candidates: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """对候选切片做精排，返回按 rerank_score 降序的新列表。

        :param query: 用户问题（与候选做成对打分的 query 一侧）
        :param candidates: 待精排的候选切片（通常来自混合检索的召回结果）
        :return: 带 rerank_score 且已按分降序的切片列表；任何失败都原样返回
        """

        # -------------------------------------------------------------------------
        # 【步骤 1：短路守卫（Short-Circuit）—— 避免不必要的远端网络开销】
        # 目的：候选数量不足以形成排序比较时直接返回。
        # 说明：
        # 1. 当候选集为空（0条）或仅有单一结果（1条）时，排序毫无意义。
        # 2. 直接原样返回，既节省远端 API 的 Token 计费，又消除了单次网络往返的延迟（RTT）。
        # -------------------------------------------------------------------------
        if len(candidates) <= 1:
            return candidates

        # -------------------------------------------------------------------------
        # 【步骤 2：环境与配置硬检查（Configuration Fail-Fast）】
        # 目的：确保精排基础设施已就绪，遇配置疏漏立即熔断报错。
        # 策略对比：
        # - 配置缺失（缺 Key）：属于“部署期失误”，必须【显式抛出异常】直接终止，
        #   避免系统静默跳过精排，导致开发者误以为重排功能已在线上生效。
        # - 运行时抖动（见步骤 3）：属于“偶发故障”，走降级策略，两者处理原则截然不同。
        # -------------------------------------------------------------------------
        api_key = settings.effective_rerank_api_key
        if not api_key:
            raise ConfigurationError(
                "Rerank API key 未配置，请在 .env 设置 RERANK_API_KEY 或 CHAT_API_KEY"
            )

        # -------------------------------------------------------------------------
        # 【步骤 3：调用评分底层并执行“高可用降级（Graceful Degradation）”】
        # 目的：获取与 candidates 严格等长同序的打分列表；当模型/网络抖动时保障主链路不中断。
        # -------------------------------------------------------------------------
        try:
            # 下沉调用：_fetch_scores 保证返回的 scores[i] 对应 candidates[i]
            scores = await self._fetch_scores(query, candidates, api_key)
        except Exception:
            # 降级逻辑：
            # RAG 链路中精排属于“效果优化层”而非“致命必选层”。
            # 若网络超时、模型限流或响应异常，记录完整堆栈日志，原样返回初筛结果，
            # 保障下游的大模型生成仍有检索上下文可用。
            logger.exception("rerank 调用失败，降级为不重排: query=%r", query)
            return candidates

        # -------------------------------------------------------------------------
        # 【步骤 4：基于不可变对象（Frozen Dataclass）生成新切片列表】
        # 目的：将打分安全注入切片，遵循函数式不可变规范，避免就地篡改引发并发副作用。
        # -------------------------------------------------------------------------
        # 设计要点：
        # 1. 为什么不用 chunk.rerank_score = score：
        #    RetrievedChunk 是 frozen dataclass（不可变对象），直接赋值会报 FrozenInstanceError。
        #    通过 dataclasses.replace 创建新副本，杜绝外部共享引用被隐式修改的隐患。
        # 2. 为什么传 strict=False：
        #    正常契约下两者严格等长；若底层异常或修改引入长度差异，
        #    显式声明 strict=False 确保代码以短的一方为基准配对，明确告知读者存在容错兜底。
        ranked = [
            dataclasses.replace(chunk, rerank_score=score)
            # 利用 zip 并行遍历两个列表，按相同下标将候选切片与其得分一一配对为 (chunk, score) 元组；
            # strict=False 显式声明允许长度不等时以较短列表为准静默截断，避免因长度不一致抛出 ValueError
            for chunk, score in zip(candidates, scores, strict=False)
        ]

        # -------------------------------------------------------------------------
        # 【步骤 5：统一客户端排序（Client-Side Deterministic Sort）】
        # 目的：以切片内的 rerank_score 为唯一信任基准，产出最终降序列表。
        # -------------------------------------------------------------------------
        # 说明：
        # 1. 不直接依赖第三方 API 返回的顺序，而是基于对象属性显式排序，实现“数据与表现分离”。
        # 2. `c.rerank_score or 0.0`：做空值防护，若存在未打上分数的切片，兜底按最低分（0.0）排在队尾。
        # 3. reverse=True：相关度越高的切片排在越前面。
        ranked.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)

        # -------------------------------------------------------------------------
        # 【步骤 6：输出重排后的候选切片列表】
        # -------------------------------------------------------------------------
        return ranked

    async def _fetch_scores(
            self,
            query: str,
            candidates: list[RetrievedChunk],
            api_key: str,
    ) -> list[float]:
        """调 DashScope rerank 端点，返回与 candidates 同序的 relevance_score 列表。

        响应里 results 按相关度降序排好，并通过 index 指回原始 documents 数组。
        所以这里把 results 用 index 重排回输入顺序，再让上层按 rerank_score 排序。
        **这样数据流更直观**：candidates[i] ↔ scores[i]。
        """

        # -------------------------------------------------------------------------
        # 【步骤 1：组装请求体 (Payload)】
        # 目的：按 DashScope 文本精排（OpenAI 兼容规范）格式构建入参，向模型提交打分任务。
        # -------------------------------------------------------------------------
        payload: dict[str, Any] = {
            "model": settings.rerank_model,
            "query": query,
            # 【格式注意】：
            # 1. 结构扁平：这里走的是文本兼容端点，query / documents 必须放在顶层（区别于多模态 qwen3-vl-rerank 嵌套在 input 内的形式）。
            # 2. 提取纯文本：模型只需要文本内容计算打分，因此通过列表推导式提取出每个 chunk 的 content。
            "documents": [c.content for c in candidates],
            # 【打分策略与可观测性】：
            # top_n 传候选全量长度（len(candidates)），要求模型对所有候选都评分。
            # 为什么不在底层就截断？
            # 如果底层截取了 top_k，后续节点和监控面板（observe_context）就无法观测到被淘汰候选的分数，
            # 导致排查“Bad Case / 为什么某篇文档落选”时缺乏数据依据。
            "top_n": len(candidates),
        }

        # -------------------------------------------------------------------------
        # 【步骤 2：设置请求头 (Headers)】
        # 目的：配置鉴权令牌与报文格式。
        # -------------------------------------------------------------------------
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # -------------------------------------------------------------------------
        # 【步骤 3：发起异步 HTTP POST 请求】
        # 目的：非阻塞地调用远端 Rerank 服务端点，提高系统在并发场景下的吞吐能力。
        # -------------------------------------------------------------------------
        client = self._get_client()
        response = await client.post(
            settings.rerank_base_url, json=payload, headers=headers
        )

        # -------------------------------------------------------------------------
        # 【步骤 4：HTTP 状态码校验】
        # 目的：保障网络及服务端正常，遇错主动暴露。
        # 说明：若遇到 4xx/5xx 等异常状态码直接抛出 HTTPStatusError，不在此处盲目吞掉，
        # 方便外层调用链中的 try...except 能够捕获并走既定的降级策略（例如回退到向量检索原分）。
        # -------------------------------------------------------------------------
        response.raise_for_status()

        # -------------------------------------------------------------------------
        # 【步骤 5：解析响应报文并进行结构防御检查】
        # 目的：将响应解析为 JSON 字典，校验业务数据是否符合契约。
        # -------------------------------------------------------------------------
        data = response.json()

        # 防御性取值：避免字段缺失或为 None 导致后续迭代报 AttributeError
        results = data.get("results") or []
        if not isinstance(results, list) or len(results) == 0:
            # 【设计考量：显式报错 vs 隐式填 0】：
            # 若此时默认返回全 0，所有候选 chunk 得分相同，排序将完全退化为“原样输出”，
            # 这种“静默失效”在生产日志中极难被监控发现，因此必须抛出异常让告警捕获。
            raise ValueError(f"rerank 响应缺少 results: {data!r}")

        # -------------------------------------------------------------------------
        # 【步骤 6：映射重排——将无序/降序的模型打分严格对齐到输入顺序】
        # 目的：模型返回的是按分数降序的结果，且通过 index 指向原文档。
        #       这里将分数放回对应的 index 位置，保证输出列表与输入 candidates 严格同序且等长。
        # -------------------------------------------------------------------------
        # 6.1 初始化全 0 分数列表，容量严格等于 candidates 长度，作为默认兜底值
        scores: list[float] = [0.0] * len(candidates)

        # 6.2 遍历模型打分结果，回填到对应索引位置
        for item in results:
            idx = item.get("index")
            score = item.get("relevance_score")

            # 6.3 字段类型防御：验证 index 是否为整数、score 是否为有效数值，防止脏数据注入
            if not isinstance(idx, int) or not isinstance(score, (int, float)):
                continue

            # 6.4 数组越界防御：确保返回的 index 在 [0, len(scores)-1] 合法闭区间内
            if 0 <= idx < len(scores):
                scores[idx] = float(score)

        # -------------------------------------------------------------------------
        # 【步骤 7：返回同序分数列表】
        # 结果满足：len(scores) == len(candidates)，且 scores[i] 对应 candidates[i] 的相关度
        # -------------------------------------------------------------------------
        return scores


# =============================================================================
# 最后包一层单例工厂
# =============================================================================
# 与 get_chat_model / get_query_rewriter / get_agent_planner 同理：
# Reranker 只在内部持有一个 httpx 客户端，单例复用即复用了连接池。
_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """获取 Reranker 单例。"""
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker
