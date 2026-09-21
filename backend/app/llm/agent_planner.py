"""Agentic RAG 的 LLM 决策器。

【模块职责】：
把第 4 章写好的决策器 prompt（`AGENT_PLAN_PROMPT`）封装成一个可调用对象，
负责「调模型 → 解析单行 JSON → 校验合法性 → 降级」这条完整的决策链路。

【分层定位】：
本模块只负责「根据历史观察决定下一步」，不碰 state、不碰数据库、不碰检索。
把决策从节点里拆出来，节点只做「拿决策 → 写 state」的编排工作，
因此本模块可以脱离 LangGraph 单独测试（传字符串进、拿 AgentDecision 出）。

【与上一期 QueryRewriter 的关系】：
调用风格完全一致 —— 单例工厂 + LLM 调用 + 内部兜底降级。
区别在于 `QueryRewriter` 的降级目标是「原查询」，而本模块的降级目标是
「继续执行」（proceed），因为决策失败不应该阻断整条问答链路。
"""

# 引入日志工厂
from app.core.logging import get_logger
# 引入对话模型工厂
from app.llm.models import get_chat_model
# 引入第 4 章写好的决策器 messages 装配函数
from app.llm.prompts import build_agent_plan_messages
# 引入检索策略字面量类型（与前端 QueryRouteRead.route 契约对齐）
from app.workflows.rag_state import QueryRoute
# 引入 dataclass 装饰器（定义不可变的决策结果载体）
from dataclasses import dataclass
# 引入 JSON 解析（决策器输出是单行 JSON）
import json
# 引入 BaseMessage 用于消息列表的类型标注
from langchain_core.messages import BaseMessage
# get_args 用于从 Literal 类型里取出合法取值元组，避免把合法值抄两遍
from typing import Literal, get_args

logger = get_logger(__name__)

# 决策器可以输出的四种动作：
#   proceed       当前候选已够回答，跳出循环去生成
#   rewrite_query 换一个查询表达再检索（必须给出 new_query）
#   switch_route  换一种检索策略（必须给出 new_route）
#   refuse        多轮都召回不到，提前拒答
AgentAction = Literal["proceed", "rewrite_query", "switch_route", "refuse"]

# 合法检索策略元组，从 QueryRoute 字面量直接推导，保证两处永远一致
VALID_ROUTES: tuple[str, ...] = get_args(QueryRoute)

# 合法动作元组，同样从 Literal 推导 —— 校验时用元组比用集合更直观（成员判断语义）
VALID_ACTIONS: tuple[str, ...] = get_args(AgentAction)

# frozen=True 表示实例创建后属性不可修改（不可变对象），防止数据在管道传递过程中被意外篡改
@dataclass(frozen=True)
class AgentDecision:
    """一轮决策结果。

    new_query / new_route 仅在对应 action 下有义；其余 action 下为 None。
    """

    action: AgentAction
    # 决策理由：仅用于日志与 agent_steps 留痕，不参与任何逻辑判断
    reason: str | None = None
    # 仅 action=rewrite_query 时有值
    new_query: str | None = None
    # 仅 action=switch_route 时有值
    new_route: QueryRoute | None = None


class AgentPlanner:
    """LLM 决策的薄封装。

    【为什么是"薄封装"】：
    本类不持有 state、不做检索、不写数据库，只做三件事：
    ① 把 state 里的片段拼成 prompt 能用的文本；
    ② 调模型并解析 JSON；
    ③ 校验合法性并给出安全的降级结果。
    这样它既能被节点调用，也能在单元测试里脱离 LangGraph 单独跑。
    """

    async def plan(
        self,
        question: str,
        current_query: str,
        current_route: QueryRoute,
        previous_steps: list[dict],
    ) -> AgentDecision:
        """让 LLM 看历史检索观察，输出下一步决策。

        :param question: 用户原始提问（决策的锚点，改写跑偏时以它为准）
        :param current_query: 当前这一轮实际用于检索的查询词
        :param current_route: 当前生效的检索策略
        :param previous_steps: 前几轮的 agent_steps 记录（决策器据此判断"试过什么了"）
        :return: 已通过合法性校验的决策；任何异常都降级为 proceed
        """
        # 1. 把前几轮的记录压成一段紧凑文本，塞进 prompt 的 {history} 槽位
        history = _format_history(previous_steps)

        # 2. 组装 messages（模板与占位符在第 4 章已定义好）
        messages = build_agent_plan_messages(
            question=question,
            current_query=current_query,
            current_route=current_route,
            history=history,
        )

        # 3. 调用模型并解析
        #    try 包住"调模型 + 解析"整段：网络异常、超时、返回结构不符都在这里降级
        try:
            # await 一次对话模型调用（实测约 2 秒）
            response = await get_chat_model().ainvoke(messages)
            # 从响应里抽出纯文本（不同厂商 content 的结构不一致，见 _extract_text）
            raw = _extract_text(response.content)
            # 解析 + 校验 + 二次校验（new_query / new_route 是否补齐）
            return _parse_decision(raw)
        except Exception:
            # 决策失败不能阻断整条问答链路 —— 降级为"继续执行"，让下游照常生成
            logger.exception("agent planner 调用失败，降级 proceed: question=%r", question)
            return AgentDecision(action="proceed", reason="planner_exception")


# 进程级单例：与 get_chat_model / get_query_rewriter 同理，
# 本类无状态，复用实例可避免每次请求重复构造。
_planner: AgentPlanner | None = None


def get_agent_planner() -> AgentPlanner:
    """获取决策器单例。

    :return: 进程内复用的 AgentPlanner 实例
    """
    global _planner
    if _planner is None:
        _planner = AgentPlanner()
    return _planner


def _format_history(steps: list[dict]) -> str:
    """把 agent_steps 压成一段可读文本，省 Token。

    【为什么不用 json.dumps 直接序列化】：
    记录里含 reason / query 等长文本，整段 JSON 序列化会把 Token 预算吃掉大半，
    而决策器真正需要的只是「第几轮、什么动作、什么策略、检了几条、多少分」这几个数。
    因此只挑关键字段压成一行一行的紧凑格式。

    :param steps: agent_steps 列表（可能为空）
    :return: 每行一条的紧凑文本；无记录时返回明确的占位说明
    """
    # 首轮：还没有任何历史，给一个明确占位，避免 prompt 里出现空槽位
    if not steps:
        return "（无）"

    # 每轮压成一行，字段顺序固定，便于模型对齐阅读
    lines: list[str] = []
    for step in steps:
        # 全部用 .get 取值：agent_steps 是 dict，字段可能缺失（如 observe 尚未回填）
        lines.append(
            f"round={step.get('round')} action={step.get('action')} "
            f"route={step.get('route')} query={step.get('query')} "
            f"retrieved_count={step.get('retrieved_count')} "
            f"top_score={step.get('top_score')} "
            f"sufficient={step.get('sufficient')}"
        )
    # 用换行拼接成一段文本，对应 prompt 里的 {history}
    return "\n".join(lines)


def _parse_decision(raw: str) -> AgentDecision:
    """解析 LLM 输出的单行 JSON，任何不合法都降级 proceed。

    【为什么所有异常路径都返回 proceed 而不是抛异常】：
    决策器是"锦上添花"的环节 —— 它给出更聪明的下一步，但给不出也不该让整条链路失败。
    降级为 proceed 的语义是「就按当前候选去生成」，这与不开 agent 循环时的行为一致，
    是一个安全、可预期、且用户无感的退路。

    :param raw: 模型返回的原始文本（可能含 ```json 围栏或前后缀）
    :return: 合法决策；任何一步失败都返回 AgentDecision(action="proceed")
    """

    # =========================================================================
    # 第一步：文本前置清洗（Markdown 代码围栏与空白字符剥离）
    # 目的：大模型即使被严格要求输出单行 JSON，也经常惯性输出 ```json ... ```。
    #      在此阶段将其剥离为纯粹的 JSON 格式文本，防止底层反序列化直接报错。
    # =========================================================================
    # 【语法说明】：str.strip() 去除首尾空白符（包含换行符 \n、制表符 \t）
    text = raw.strip()
    if text.startswith("```"):
        # 【语法说明】：text.lstrip("`") 仅剔除左侧（开头）所有连续的反引号 `
        text = text.lstrip("`")
        if text.lower().startswith("json"):
            # 【语法说明】：text[4:] 切片语法（从索引 4 截到末尾），即跳过并剥除前 4 个字符 "json"
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            # 【语法说明】：text[:-3] 负索引切片，丢弃末尾倒数后 3 个字符，即剥除闭合的 ```
            text = text[:-3]
        text = text.strip()

    # =========================================================================
    # 第二步：JSON 反序列化（解析为 Python 内存对象）
    # 目的：尝试将清洗后的纯文本解析为 Python 原生对象；若模型输出残缺或非 JSON，
    #      捕获异常做平滑降级，保证服务绝对不向外抛出未处理异常。
    # =========================================================================
    try:
        # 【语法说明】：json.loads() 将字符串转为 Python dict/list 等数据结构
        data = json.loads(text)
    except json.JSONDecodeError:
        # 【语法说明】：%r 为 repr() 占位符，打印时带转义符与引号，便于在日志排查大模型吐出的特殊乱码
        logger.warning("agent planner JSON 解析失败，降级 proceed: raw=%r", raw)
        return AgentDecision(action="proceed", reason="planner_parse_failed")

    # =========================================================================
    # 第三步：数据契约顶层类型防御
    # 目的：校验反序列化后的顶层结构。大模型偶尔会返回 "[{...}]"（数组）或纯字面量，
    #      若非字典对象，后续调用 .get() 会抛 AttributeError 崩溃，因此必须拦截。
    # =========================================================================
    # 【语法说明】：isinstance(obj, type) 运行时类型断言
    if not isinstance(data, dict):
        return AgentDecision(action="proceed", reason="planner_parse_failed")

    # =========================================================================
    # 第四步：各独立字段的格式提取与清洗过滤
    # 目的：对 action、reason、new_query、new_route 分别进行类型兜底、空格剔除
    #      与白名单检验，将模型幻觉输出或类型脏数据过滤为合法的 Python 对象/None。
    # =========================================================================

    # 4.1 提取并校验 action
    # 【语法说明】：data.get("key", default) 安全取值，规避直接方括号索引引发的 KeyError；
    #             str(...).strip().lower() 强制转为规范的小写无首尾空格字符串
    action = str(data.get("action", "")).strip().lower()
    # 【语法说明】：VALID_ACTIONS 通常是 set 集合，使用 `in` 操作时间复杂度为 O(1)
    if action not in VALID_ACTIONS:
        logger.warning("agent planner 返回非法 action，降级 proceed: action=%r", action)
        return AgentDecision(action="proceed", reason="planner_invalid_action")

    # 4.2 提取 reason（留痕与可观测性）
    # 【语法说明】：Python 短路求值，左边为 None 或空串（Falsy）时直接取右侧默认值
    reason = str(data.get("reason") or "").strip() or "no reason"

    # 4.3 提取并清洗 new_query
    new_query_raw = data.get("new_query")
    # 【语法说明】：三元运算符 [A] if [条件] else [B]，确保仅保留有有效字符的字符串，其他情况一律为 None
    new_query = (
        str(new_query_raw).strip()
        if isinstance(new_query_raw, str) and new_query_raw.strip()
        else None
    )

    # 4.4 提取并清洗 new_route
    new_route_raw = data.get("new_route")
    # 【语法说明】：必须为字符串且去除空格小写后在允许的策略枚举中（如 {"original", "rewrite", "hyde", "multi_query"}）
    if isinstance(new_route_raw, str) and new_route_raw.strip().lower() in VALID_ROUTES:
        # 【语法说明】：# type: ignore[assignment] 提示静态类型检查器（如 Mypy）已人工做完白名单窄化，忽略赋值类型告警
        new_route = new_route_raw.strip().lower()
    else:
        new_route = None

    # =========================================================================
    # 第五步：业务层联动一致性校验（Action 与参数匹配）
    # 目的：防止出现“口惠而实不至”的状态机错误。如模型决定改写提问却没给改写后的文本，
    #      或者决定换策略却没指定新策略。此类决策属于不可执行动作，强制降级。
    # =========================================================================
    if action == "rewrite_query" and not new_query:
        logger.warning("agent planner action=rewrite_query 缺 new_query，降级 proceed")
        return AgentDecision(action="proceed", reason="planner_missing_query")

    if action == "switch_route" and not new_route:
        logger.warning("agent planner action=switch_route 缺 new_route，降级 proceed")
        return AgentDecision(action="proceed", reason="planner_missing_route")

    # =========================================================================
    # 第六步：构造并返回合法的领域决策对象
    # 目的：所有数据清洗和逻辑门禁均已通过，构建不可变/强类型的 AgentDecision 实体，
    #      安全交付给下游状态机节点执行。
    # =========================================================================
    return AgentDecision(
        action=action,  # type: ignore[arg-type]
        reason=reason,
        new_query=new_query,
        new_route=new_route,
    )



def _extract_text(content: str | list[str] | dict) -> str:
    """从 langchain ChatModel 的 content 里取文本。

    不同厂商 / 不同 langchain 版本的 content 结构不一致：
    - 有的直接给 str；
    - 有的给 list[dict]（多模态分块，如 [{"type": "text", "text": "..."}]）；
    - 少数给 list[str]。
    这里统一收敛成 str，避免决策器因为厂商差异而解析失败。

    :param content: ChatModel 返回的原始 content
    :return: 拼接后的纯文本
    """
    # 最简单情况：已经是字符串
    if isinstance(content, str):
        return content
    # 分块列表：逐个取 text 字段后拼接
    if isinstance(content, list):
        return "".join(
            # 兼容 {"text": ...} 与 {"content": ...} 两种键名
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    # 兜底：其他类型（dict / None 等）直接字符串化，绝不抛异常
    return str(content or "")
