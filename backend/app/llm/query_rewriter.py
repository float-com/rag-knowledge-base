"""Query 优化统一入口（QueryRewriter）。

【模块职责说明】：
1. 策略统一入口（Unified Entry Point）：
   把上一节准备好的四组提示词真正用起来，对外只暴露一个 `optimize()` 方法。
   上层（route_query 节点）不需要了解"怎么调 LLM、怎么解析返回值、失败了怎么办"，
   这些复杂性全部被封装在本模块内部。

2. 输出结构化契约（Structured Result Contract）：
   以 frozen dataclass `QueryRouteResult` 承载四种策略的产出，
   将「策略名 + 最终检索词 + 各策略明细」打包成一个不可变对象返回，
   避免调用方到处处理散落的 dict 键。

3. 失败降级（Fail-Safe Degradation）：
   LLM 调用比数据库查询不稳定得多——模型抖动、返回格式不合规、上游 5xx 都可能发生。
   因此每一条策略路径都必须独立兜底，任何异常或非法返回值一律降级为 original，
   绝不向上抛出：Query 优化只是"锦上添花"，失败时必须退回优化前的基线行为。

4. 引用一致性校验（Route Validation）：
   策略名必须严格落在 QueryRoute 的四个合法值内。
   即便提示词已强令"只输出策略名"，仍要在代码侧做白名单校验——
   模型输出不可完全信任，校验失败即降级。
"""

import logging
from dataclasses import dataclass
from typing import get_args

from app.core.logging import get_logger
from app.llm.models import get_chat_model
from app.llm.prompts import (
    build_hyde_messages,
    build_multi_query_messages,
    build_rewrite_messages,
    build_route_messages,
)
from app.workflows.rag_state import QueryRoute

logger: logging.Logger = get_logger(__name__)

# 从类型别名反解出合法策略名元组：
# 后续所有路由返回值都拿它做白名单校验，避免模型输出污染 state。
# 用元组而非集合，是为了让校验顺序与 QueryRoute 定义顺序一致，便于日志阅读。
VALID_ROUTES: tuple[str, ...] = get_args(QueryRoute)


# 将数据类实例设为不可变（只读保护），并自动生成 __hash__ 方法以支持作为字典键或集合元素
@dataclass(frozen=True)
class QueryRouteResult:
    """Query 优化结果（不可变）。

    :param route: 实际采用的优化策略
    :param query: 真正用于检索的查询词；rewrite 路径下是改写文本，其余是原问题
    :param rewritten_query: rewrite 策略产出的独立完整问句
    :param hyde_answer: hyde 策略产出的假设答案
    :param multi_queries: multi_query 策略产出的多条子查询
    """

    route: QueryRoute
    query: str
    rewritten_query: str | None = None
    hyde_answer: str | None = None
    multi_queries: list[str] | None = None


def _extract_text(content: str | list[str | dict]) -> str:
    """把 LangChain ChatModel 的 content 统一归一化为纯文本。

    LangChain 中 content 可能是：
    - 普通字符串（纯文本模型的常见情况）
    - 多模态 / 复合内容块列表（如 [{"type": "text", "text": "..."}]）

    这里统一抽取文本，保证上层拿到的永远是 str。

    :param content: AIMessage.content
    :return: 归一化后的纯文本
    """
    # 纯文本分支：直接返回
    if isinstance(content, str):
        return content
    # 复合块分支：抽取所有 text 片段拼接
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


class QueryRewriter:
    """Query 优化的统一协同入口，所有 LLM 调用都是非流式 ChatOpenAI。"""

    async def decide_route(self, question: str) -> QueryRoute:
        """判定该走哪种优化策略，并把非法输出降级为 original。

        :param question: 用户原始提问
        :return: 四种策略之一；任何异常或非法值都返回 original
        """
        messages = build_route_messages(question)
        # 路由判定用 low temperature 之外的普通调用：ChatOpenAI 已全局锁 temperature=0
        response = await get_chat_model().ainvoke(messages)

        # 归一化：去首尾空白 + 转小写 + 去掉可能被模型带上的引号包裹
        raw = _extract_text(response.content).strip().lower()
        route = raw.strip('"').strip("'").strip()

        if route not in VALID_ROUTES:
            # 模型可能输出 "rewrite。"、"the route is rewrite" 等非法值，
            # 此时不抛错、不猜，直接降级 original（宁可不优化，也不要用错策略）
            logger.warning("query_route 模型返回非法结果，降级 original: raw=%r", raw)
            return "original"

        # 白名单校验通过后收窄类型
        return route  # type: ignore[return-value]

    async def rewrite(self, question: str) -> str:
        """把问题改写成独立完整的检索问句。

        【已知缺口（教程第六节「扩展」已记录，计划第八期实现）】：
        本方法只接收本轮问题，**没有把 chat_history 传给改写 prompt**，
        因此模型看不到上下文。实测效果分层：
          - 补全省略（如"那考核方式呢" → "那考核方式是什么呢"）：有效
          - 消解指代（如"它的学分是多少"）：**无效**，模型会原样返回
            （A/B 对照：补上历史后同一模型可正确改写为"JavaEE 课程的学分是多少"）

        这是设计上的阶段性取舍，不是缺陷遗漏；后续把 history 接入
        build_rewrite_messages 即可闭环，届时本注释应一并删除。

        :param question: 可能含指代或省略的用户提问
        :return: 改写后的单行问句
        """
        messages = build_rewrite_messages(question)
        response = await get_chat_model().ainvoke(messages)
        return _extract_text(response.content).strip()

    async def hyde(self, question: str) -> str:
        """生成用于召回的假设答案。

        :param question: 用户原始提问
        :return: 陈述式假设答案文本
        """
        messages = build_hyde_messages(question)
        response = await get_chat_model().ainvoke(messages)
        return _extract_text(response.content).strip()

    async def multi_query(self, question: str, n: int) -> list[str]:
        """把提问扩展成 n 条不同角度的子查询。

        :param question: 用户原始提问
        :param n: 期望生成的子查询条数（来自 settings.multi_query_count）
        :return: 子查询列表（已剔除空行）
        """
        messages = build_multi_query_messages(question, n)
        response = await get_chat_model().ainvoke(messages)

        # 逐行解析：剔除空行后再返回，避免空字符串流进 embedding（第 2 章的降级约定）
        return [q for q in _extract_text(response.content).splitlines() if q.strip()]

        # 等价于以下代码：
        # clean_queries = []
        # # 1. 抽取并切行
        # for q in _extract_text(response.content).splitlines():
        #     # 2. 判断是否非空
        #     if q.strip():
        #         # 3. 收集有效元素
        #         clean_queries.append(q)
        # return clean_queries

    async def optimize(self, question: str, multi_query_count: int) -> QueryRouteResult:
        """**统一入口**：按「路由 → 调用对应策略 → 返回结果」串起四种策略。

        任何一步失败都降级为 original，绝不向调用方抛异常。

        :param question: 用户原始提问
        :param multi_query_count: multi_query 策略的子查询条数
        :return: 结构化优化结果（不可变）
        """
        try:
            # 第一步：先判定策略
            route = await self.decide_route(question)

            if route == "rewrite":
                rewritten = await self.rewrite(question)
                if not rewritten:
                    # 改写结果为空 → 若拿去 embedding 会报错或返回无意义向量
                    # → 降级 original（第 2 章的降级约定）
                    logger.warning("rewrite 返回空结果，降级 original: question=%r", question)
                    return QueryRouteResult(route="original", query=question)
                return QueryRouteResult(
                    route="rewrite", query=rewritten, rewritten_query=rewritten
                )

            if route == "hyde":
                hyde_answer = await self.hyde(question)
                if not hyde_answer:
                    logger.warning("hyde 返回空结果，降级 original: question=%r", question)
                    return QueryRouteResult(route="original", query=question)
                return QueryRouteResult(
                    route="hyde", query=hyde_answer, hyde_answer=hyde_answer
                )

            if route == "multi_query":
                queries = await self.multi_query(question, multi_query_count)
                # 子查询不足 2 条 → 多路退化成单路，没有意义，降级 original
                if len(queries) < 2:
                    logger.warning(
                        "multi_query 子查询不足 2 条，降级 original: question=%r count=%d",
                        question,
                        len(queries),
                    )
                    return QueryRouteResult(route="original", query=question)
                # 注意：query 仍保留原问题（作为不依赖模型生成的保底检索路径），
                # 子查询单独放在 multi_queries，由 retrieve 节点做多路召回
                return QueryRouteResult(
                    route="multi_query", query=question, multi_queries=queries
                )

            return QueryRouteResult(route="original", query=question)

        except Exception:
            # 任何异常（模型抖动、网络错误、上游 5xx、解析失败）都收敛到 original
            logger.exception("query_optimize 失败，降级 original: question=%r", question)
            return QueryRouteResult(route="original", query=question)


# =============================================================================
# 最后包一层单例工厂
# =============================================================================
# 与 get_chat_model 同理：QueryRewriter 自身无状态，但复用实例可避免重复实例化；
# 后续若要给它注入配置或模型，只需改这里一处。
_rewriter: QueryRewriter | None = None


def get_query_rewriter() -> QueryRewriter:
    """获取 QueryRewriter 单例。"""
    global _rewriter
    if _rewriter is None:
        _rewriter = QueryRewriter()
    return _rewriter
