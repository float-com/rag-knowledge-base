"""RAG 答案事实性与忠实度校验器（AnswerVerifier）。

【模块核心职责】：
在生成模型完成回答后，启动一个专职“质检员模型”执行单任务审查：
输入【用户问题 + 参考文档片段 + 模型回答】，逐条比对回答中的关键结论是否
**严格且直接被参考片段所支撑（Grounding / Faithfulness 检验）**。

【为什么生成 Prompt 里已声明“找不到就拒答”，仍必须增设独立校验？】：
1. 任务复杂度与认知过载：
   大模型“承担的任务越多，产生幻觉的概率就呈指数上升”。
   在生成阶段，模型需同时处理语义理解、文风组织、逻辑润色、段落格式及精确引用等多项任务；
2. 讨好型人格与指令漂移：
   模型倾向于补全对话，容易强行推论或调用自身预训练泛化知识进行脑补；
3. 审查与生成解耦（独立裁判）：
   将校验阶段独立出来，使模型在这一步**仅担任死板、严格的合规质检员**，
   不赋予任何自由发挥空间，从工程上构筑防幻觉安全底线。

【⚠️ 核心容错哲学：反直觉的“假定通过”降级策略（Fail-Open / 宽进严出）】：
普通业务模块发生异常时，通常降级为“最保守拦截”（如拒绝服务）；
但本质检模块在**发生网络抖动、模型超限或 JSON 解析异常时，一律强制降级为 `verified=True`**。
为什么？
- 校验器属于“品质防御加分项”，而非“阻断性硬依赖”；
- 若校验器自身崩溃就将回答硬性改判为拒答，会导致“外部质检抖动拖垮整个原本正确的问答服务”；
- 工程设计铁律：**校验机制本身的偶发失败，绝不能比被校验主链路的失败更具破坏性。**
"""

# 引入不可变数据类装饰器，产出线程安全、不可篡改的校验结果契约
from dataclasses import dataclass
# 引入标准 JSON 库，用于解析大模型结构化输出
import json

# 引入系统统一日志工厂
from app.core.logging import get_logger
# 引入通用对话大模型工厂
from app.llm.models import get_chat_model
# 引入校验提示词构建与上下文片段序号格式化工具
from app.llm.prompts import build_verify_answer_messages, format_context
# 引入标准化检索切片数据实体
from app.retrieval.vector_retriever import RetrievedChunk

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# 校验结果契约（不可变数据容器）
# -----------------------------------------------------------------------------
# frozen=True 声明为只读不可变对象：防范跨协程/管道传递中字段被恶意隐式篡改
@dataclass(frozen=True)
class VerifyResult:
    """AnswerVerifier 答案校验结果输出契约。

    :param verified: 是否通过真实性校验（True: 证据充分 / 触发降级；False: 确认存在幻觉/无证据编造）
    :param reason: 判定原因说明（通过时允许为空串；未通过或降级时记录具体原因标签）
    """

    verified: bool
    reason: str


class AnswerVerifier:
    """LLM 答案可信度独立质检服务。"""

    async def verify(
        self,
        question: str,
        answer: str,
        chunks: list[RetrievedChunk],
    ) -> VerifyResult:
        """比对生成的回答与检索上下文，判断回答是否具备充分的事实支撑。

        :param question: 用户原始问题
        :param answer: 模型生成的完整最终文本（流式响应场景需待拼接完成后传入）
        :param chunks: 检索并精排后注入上下文的参考切片列表
        :return: 校验结果对象；任何非业务阻断异常一律静默兜底为 verified=True
        """
        # ---------------------------------------------------------------------
        # 【步骤 1：前置短路守卫】
        # 针对空候选或空白回答，不发起昂贵的外部模型请求：
        # - 业务已拒答场景（例如服务层已根据 refused 标识拦截），压根不会流转至此；
        # - 若出现切片为空或回答为空，属于上游异常已截断链路，无需做合规质检，直接通过。
        # ---------------------------------------------------------------------
        if not chunks or not answer.strip():
            return VerifyResult(verified=True, reason="")

        # ---------------------------------------------------------------------
        # 【步骤 2：组装对齐的审查 Prompt】
        # ⚠️ 格式对齐要点：
        # 必须复用与 generate 节点相同的 format_context 函数（统一渲染「片段 1」、「片段 2」）。
        # 确保审查员看到的上下文序号，与生成回答中引用的标注标号完全一一对应，防止因序号错位产生误杀。
        # ---------------------------------------------------------------------
        messages = build_verify_answer_messages(
            question=question,
            answer=answer,
            chunks_text=format_context(chunks),
        )

        # ---------------------------------------------------------------------
        # 【步骤 3：调用质检模型并执行宽放降级（Fail-Open）】
        # ---------------------------------------------------------------------
        try:
            # 发起异步 LLM 判定调用
            response = await get_chat_model().ainvoke(messages)
            # 提取大模型文本（抹平流式/块格式差异）
            raw = _extract_text(response.content).strip()
            # 解析模型输出的 JSON 结构
            return _parse_result(raw)
        except Exception:
            # 高可用降级点：
            # 记录完整报错堆栈，将 verified 强行兜底为 True，确保模型服务偶发抖动时不阻塞终端用户获取答案
            logger.exception(
                "answer verifier 调用失败，降级 verified=True: question=%r", question
            )
            return VerifyResult(verified=True, reason="verifier_exception")


# =============================================================================
# 模块单例与内部辅助工具
# =============================================================================
_verifier: AnswerVerifier | None = None


def get_answer_verifier() -> AnswerVerifier:
    """获取 AnswerVerifier 全局唯一服务单例（惰性单例模式）。"""
    global _verifier
    if _verifier is None:
        _verifier = AnswerVerifier()
    return _verifier


def _parse_result(raw: str) -> VerifyResult:
    """从大模型原始文本中提取并解析结构化 JSON 校验结果。

    具备三重防御机制：Markdown 围栏清洗、JSON 反序列化防护、严格布尔类型断言。
    任何环节出现脏数据，统一触发宽放降级（verified=True）。
    """
    text = raw.strip()

    # -------------------------------------------------------------------------
    # 【防御 1：清除 Markdown 语法标记】
    # 大模型即使被要求仅输出 JSON，有时仍会习惯性包裹 ```json ... ``` 代码块
    # -------------------------------------------------------------------------
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # -------------------------------------------------------------------------
    # 【防御 2：反序列化异常拦截】
    # 防止大模型输出残缺 JSON 或夹杂废话解释导致 JSONDecodeError
    # -------------------------------------------------------------------------
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("answer verifier JSON 解析失败，降级 verified=True: raw=%r", raw)
        return VerifyResult(verified=True, reason="verifier_parse_failed")

    # 根节点必须是键值字典，拦截 JSON 数组或标量基本类型
    if not isinstance(data, dict):
        return VerifyResult(verified=True, reason="verifier_parse_failed")

    # -------------------------------------------------------------------------
    # 【防御 3：严格布尔类型校验（致命逻辑陷阱避坑）】
    # -------------------------------------------------------------------------
    verified_raw = data.get("verified")

    # ⚠️ 为什么必须用 `isinstance(..., bool)`，严禁写成 `if verified_raw:`？
    # 如果大模型吐出字符串 "false"，在 Python 隐式真值转换下，非空字符串 "false" 会被算作 True！
    # 这会导致原本判定【存在幻觉】的回答，被荒唐地误判为【校验通过】——此乃致命反向 Bug。
    # 因此这里实施白名单检查：非显式 bool 类型（如字符串、数值 0/1、None）均按解析故障降级处置。
    if not isinstance(verified_raw, bool):
        logger.warning(
            "answer verifier 返回 verified 字段非布尔，降级 verified=True: raw=%r", raw
        )
        return VerifyResult(verified=True, reason="verifier_invalid_verified")

    # 提取原因字段并强制转为纯字符串，防范类型不合规
    reason = str(data.get("reason") or "").strip()
    return VerifyResult(verified=verified_raw, reason=reason)


def _extract_text(content: str | list[str | dict]) -> str:
    """抹平 LangChain 对话模型 content 返回值的联合类型差异。

    根据 SDK 版本和多模态协议不同，content 可能是纯字符串，也可能是包含字典/分块的列表。
    """
    # 情况 1：纯文本响应，直接返回
    if isinstance(content, str):
        return content

    # 情况 2：复合块列表，仅过滤并拼接字典内包含的 text 字段
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))