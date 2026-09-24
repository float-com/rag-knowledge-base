"""评测打分：业务指标计算 + Bad Case 归因规则引擎。

【模块核心职责】：
本模块收敛评测流程中的两类**纯计算（Pure Computation）**逻辑。
- 零外部依赖：不读写数据库、不发起网络请求、无副作用。
- 极致轻量：可做到完全脱离外部环境进行秒级单元测试（Unit Test）。

包含两大部分：
1. 自定义业务指标计算：
   - `compute_citation_hit`: 引用证据命中判定（模型回答是否引用了指定的文档/命中核心事实片段）。
   - `compute_refusal_correct`: 拒答决策对齐判定（该拒答的必须拒答，不该拒答的必须正常回答）。

2. Bad Case 根因归因分析（`classify_bad_case`）：
   - 当一个测试用例表现不佳时，从底层基础设施到上层模型生成，按照**“依赖链路越底层，优先级越高”**
     的漏斗原则自上而下匹配，**命中即停（Short-Circuit）**。
   - 目标：将杂乱的分数直接转化为算法工程师明确的“优化修整动作”（如调检索、改重排还是磨 Prompt）。

【为什么必须按优先级“命中即停”？】：
RAG 是一个经典的顺序依赖流水线：
  [网络/运行链路] -> [意图与边界判断(拒答)] -> [召回/检索] -> [重排] -> [生成/表述]
如果最底层的链路报错或者检索没查到知识库，此时模型“回答得离题”完全是表象而非根因。
因此归因必须从最底层开始短路拦截，避免产生“检索彻底丢了，却去归因大模型胡说八道”的误诊。
"""

from dataclasses import dataclass
from typing import Literal

# Bad Case 归因标准化分类字典（与 PRD 严格保持 1:1 对齐）
# 明确涵盖：解析层 -> 检索层 -> 决策层 -> 排序层 -> 生成层 -> 权限与兜底
BadCaseCategory = Literal[
    "document_parse_failed",    # 文档解析失败（文件格式、OCR、乱码等问题）
    "chunk_split_bad",         # 文本分块不合理（切碎语义或块过大超出窗口）
    "embedding_recall_miss",   # 向量检索未命中（语义匹配度低，漏召回）
    "keyword_recall_miss",     # 关键字检索未命中（稀有词/编号未召回）
    "rrf_fusion_error",        # 混合检索融合策略故障（多路权重失调）
    "rerank_order_error",      # 重排序次序错误（相关文档被排到了上下文末尾被截断）
    "context_judge_too_loose", # 边界闸门判定过松（该拒答但没拒答，导致幻觉外溢）
    "context_judge_too_strict",# 边界闸门判定过严（知识库有证据却误拒答，导致可用性降低）
    "prompt_constraint_weak",  # 提示词约束过弱（模型偏离格式、问东答西）
    "generation_off_context",  # 生成偏离上下文（知识库有内容，模型却脱缰自由发挥幻觉）
    "citation_parse_failed",   # 引用格式解析失败（模型未按规范输出角标引用）
    "permission_filter_error", # 权限过滤错误（越权访问或错判拦截）
    "other",                   # 运行环境报错、网络中断等非业务算法类硬故障
]

# # 业务及 RAGAS 评估指标阈值线（小于该阈值判定为“低分/不合格”）
# 说明：当前教学版本采用 0.5 统一打平；在严苛的生产场景中通常分别配置：
# 比如：召回率 context_recall 可适当放宽到 0.6，但忠实度 faithfulness 通常要求 >= 0.85
_LOW_SCORE_THRESHOLD = 0.5


@dataclass(frozen=True)
class BadCaseRule:
    """一条规则归因的不可变决策结论。

    【工程设计考虑 - frozen=True】：
    - 保证数据不可变性（Immutable）：算法引擎输出的机评结论一旦生成就锁定，
      禁止后续任何业务代码在内存中将其就地篡改（In-place Mutation）。
    - 人工标注打标时：前端人工若要修正归因分类，必须通过专用的 PATCH 接口提交人工修正记录，
      由版本合并策略覆盖机评，以保留机评与人评的对比轨迹。
    """

    is_bad_case: bool
    category: BadCaseCategory | None


def compute_citation_hit(
    actual_citations: list[dict],
    expected_document_names: list[str],
    expected_keywords: list[str],
) -> bool:
    """计算引用命中判定（判断模型给出的引用证据是否合规且对齐预期）。

    【多路宽松判定逻辑（满足任意一路即视为命中）】：
    1. 路径 A（文档名精准对齐）：只要模型引用的任意一篇文档名称，命中期望文档列表，即判定命中。
    2. 路径 B（关键内容兜底）：若因切片命名差异导致文档名未对上，只要期望关键词出现在引用的摘录原文中，即判定命中。
    3. 路径 C：两路均未对齐，或实际完全未提供任何引用时，判定未命中（False）。

    :param actual_citations: 模型回答中解析出的引用详情，格式形如：
           [{"document_name": "手册.pdf", "quote": "退费流程需提交工单"}]
    :param expected_document_names: 评测标注集中的标准参考文档名列表
    :param expected_keywords: 评测标注集中的关键论据文本特征词列表
    :return: 命中返回 True，未命中返回 False
    """
    # 前置边界防御：如果模型完全没有给引用，直接判负（无需浪费后续 set/join 计算）
    if not actual_citations:
        return False

    # -------------------------------------------------------------------------
    # 路径 A：文档名匹配（看看模型引用的文档名，在不在期望的文档列表里）
    # -------------------------------------------------------------------------

    # 【集合推导式 Set Comprehension】：花括号 {...} 加上 for 循环，相当于一个自动去重的数据集
    # 1. 遍历 actual_citations（模型给出的所有引用字典，如 [{"document_name": "手册.pdf"}, ...]）
    # 2. c.get("document_name", "")：取出字典里的文档名；如果没写这个字段，就默认给空字符串 ""，防止程序崩掉
    # 3. 外层花括号将所有取出来的文档名打包成一个 set（集合），集合内部查找元素是瞬间完成的（O(1) 复杂度）
    actual_doc_names = {c.get("document_name", "") for c in actual_citations}

    # 【any() 函数 + 生成器表达式】：只要有“任意一个”符合条件，立刻返回 True
    # 1. for name in expected_document_names: 遍历评测集里标准答案期望出现的文档名
    # 2. if name: 过滤掉标注数据里的空字符串或 None，避免空字符误判命中
    # 3. name in actual_doc_names: 判断该文档名是否在上面收集好的集合里
    # 4. any(...): Python 内置函数，只要里面有一个是 True，它就立刻停下来返回 True（不会傻傻算完全部）
    #
    # 【业务设计考量：为什么只要命中一个（any）就可以返回 True？】
    # 1. 同义知识源容错：标注时一个问题的答案往往可能分散或重复存在于多个文档中（如《政策2024版》、《政策2025版》、《FAQ》）。
    #    模型只要检索并引用其中任意一份有效出处，就足以证明不是凭空捏造，若要求全部引用（all）会造成严重误杀。
    # 2. 避免大模型“刷引用”：若强制要求引用所有期望文档，会反向迫使模型在回答末尾堆砌所有弱相关文档，损害用户体验。
    # 3. 粗筛闸门职责定位：该指标只是定性的“准入门槛”（确认模型回答有据可依而非纯幻觉），
    #    至于“知识点找得全不全”，由后续的 RAGAS context_recall 指标去算精细召回率得分。
    if any(name in actual_doc_names for name in expected_document_names if name):
        return True

    # -------------------------------------------------------------------------
    # 路径 B：关键词文本兜底匹配（文档名对不上时的退路：看引用原文里有没有核心关键词）
    # -------------------------------------------------------------------------

    # 如果测试用例根本没配 expected_keywords（空列表为 False），直接跳过此路径
    if expected_keywords:

        # 【字符串拼接 join + 列表/生成器】：把所有引用的摘录文本粘成一大段长文章
        # 1. (str(c.get("quote", "")) for c in actual_citations):
        #    从每一个引用字典里拿出 "quote"（模型引用的原文片段），转成字符串，防止里面存了非文本数据
        # 2. "\n".join(...): 用换行符把这些零碎的片段粘成一个长长的字符串 quote_blob（大文本块）
        # 为什么这么做？
        # 相比于“拿关键词逐个去每条引用里找”，直接把所有引用拼成一篇大文章，再拿关键词去查大文章，代码更简单且高效
        quote_blob = "\n".join(str(c.get("quote", "")) for c in actual_citations)

        # 【关键词检索】：在上面拼出的大文本块里，查是否包含某个关键词
        # 1. for kw in expected_keywords: 遍历期望的关键词（比如 ["退款", "人工客服"]）
        # 2. if kw: 过滤掉标注数据里的空字符串（"" in "任何文本" 永远是 True，不过滤会导致严重误判！）
        # 3. kw in quote_blob: 只要这个关键词作为子字符串出现在了大文本块里，该项就为 True
        # 4. any(...): 只要命中任意一个关键词，整句立即判定为 True
        if any(kw in quote_blob for kw in expected_keywords if kw):
            return True

    # 路径 A（文档名）和路径 B（关键词）都没对上，判定本次引用未命中
    return False


def compute_refusal_correct(actual_refused: bool, should_refuse: bool) -> bool:
    """计算拒答决策是否正确（评估模型对系统业务安全边界的把控能力）。

    :param actual_refused: 模型的【实际表现】（算法侧输出）
        - 含义：本次运行大模型/系统实际上有没有做出拒答动作。
        - 取值：
            * True  -> 系统触发了拒答（例如回答：“知识库中未找到相关内容，无法为您解答”）。
            * False -> 系统正常输出了业务回答。

    :param should_refuse: 标注集的【标准答案】（测试用例预期）
        - 含义：这道评测题根据业务规则，到底“该不该”被拒答。
        - 取值：
            * True  -> 属于超出业务范围、恶意提问、或知识库完全没有的越界题，【预期必须拒答】。
            * False -> 属于正常业务提问，知识库有明确依据，【预期正常回答】。

    :return:
        布尔值（True/False）。只有“实际做的”和“期望做的”完全吻合时，才返回 True（判对）。

    【为什么用严格等号 `==` 校验（双向对称性）？】：
    - 漏防风险（该拒没拒）：should_refuse=True 但 actual_refused=False
      隐患：知识库明明没有，模型却强行编造，产生严重业务幻觉。
    - 误杀风险（不该拒却拒）：should_refuse=False 但 actual_refused=True
      隐患：系统明明能查到有效答案，却冷漠推脱，严重损害可用性。
    只有两者完全相等（True == True 或 False == False），决策才算正确。
    """
    # 只要实际表现符合预期，判定为及格（True）；只要有任何偏差即为不及格（False）
    return actual_refused == should_refuse


def classify_bad_case(
    *,
    should_refuse: bool,
    actual_refused: bool,
    refusal_correct: bool,
    citation_hit: bool | None,
    faithfulness: float | None,
    answer_relevancy: float | None,
    context_precision: float | None,
    context_recall: float | None,
    is_error: bool,
) -> BadCaseRule:
    """Bad Case 归因决策引擎：采用短路漏斗机制，按优先级自底向上锁定首要根因。

    【参数详细拆解（按数据流来源划分）】：

    一、系统运行层入参（调用链基础设施）：
    :param is_error:
        - 含义：本次评测用例在执行链路中是否遭遇了致命系统异常。
        - 来源：框架捕获的运行崩溃标记（如 LLM API 超时、网络抖动 500、代码抛出未捕获异常）。
        - 逻辑影响：若为 True，说明连完整答案都没跑出来，下游所有评测指标均无意义，直接定性为底层故障。

    二、业务安全与准入门控入参（拒答决策链）：
    :param should_refuse:
        - 含义：标注集给出的【预期标准】——该题根据业务规则到底该不该拒答。
        - 取值：True（违规/超纲/库内无答案题，必须拒答）；False（库内有据可依的正常业务题）。
    :param actual_refused:
        - 含义：模型给出的【实际表现】——本次回答系统最终到底有没有执行拒答。
        - 取值：True（输出了标准拒答语）；False（输出了正常业务回答）。
    :param refusal_correct:
        - 含义：拒答决策的【一致性校验结果】（即 compute_refusal_correct 的返回值）。
        - 计算逻辑：actual_refused == should_refuse。
        - 逻辑影响：若为 False，说明安全闸门判错，优先级极高，需立刻拦截。

    三、证据支撑层入参（知识检索与引用链）：
    :param citation_hit:
        - 含义：模型输出的引用标记，是否命中了期望文档或关键事实。
        - 取值：
            * True  -> 引用合规且有效命中。
            * False -> 模型生成了回答，但引用完全对不上期望（严重检索/对齐缺失）。
            * None  -> 【关键特例】该用例本身就是拒答题（should_refuse=True），本就不该有引用；
                       或者评测集未配置引用校验。None 代表“不适用”，不可当成未命中！

    四、RAGAS 专业量化评估入参（范围通常在 0.0 ~ 1.0 之间，低于 0.5 视作显著低分）：
    :param context_recall:
        - 含义：【检索召回率】。测试集期望参考的事实，检索到的上下文 Chunk 覆盖了百分之多少？
        - 诊断环节：检索模块（Embedding / Keyword）。
    :param context_precision:
        - 含义：【上下文精准率/排序质量】。检索到的真阳性（含答案）Chunk 是否排在最前面？
        - 诊断环节：重排模块（Rerank / RRF 融合）。
    :param faithfulness:
        - 含义：【忠实度/真实性】。模型回答的每句话，能否全部从检索到的上下文中找到推导证据？
        - 诊断环节：大模型幻觉抑制（Anti-Hallucination）。
    :param answer_relevancy:
        - 含义：【回答相关性】。模型回答的内容是否真正正面解答了用户的问题？
        - 诊断环节：模型指令遵循（Instruction Following）与 System Prompt 约束。
    - 注意：以上 4 项若为 None，代表评分服务偶发超时或跳过了该项打分，防御机制规定“缺失不算低分”。

    :return: BadCaseRule 不可变对象，包含是否为坏例（is_bad_case）及具体根因分类（category）。
    """

    # =========================================================================
    # Level 0：基础设施与运行环境异常（硬故障拦截）
    # =========================================================================
    # 逻辑：模型直接挂了（超时、502、解析崩溃），此时去分析“模型说得对不对”毫无意义。
    # 归因：other（运维/网络/外部服务异常）
    # 动作：排查服务健康状态、网络连接池、API 负载限制与降级熔断配置。
    if is_error:
        return BadCaseRule(is_bad_case=True, category="other")

    # =========================================================================
    # Level 1：安全与意图门控故障（拒答决策失准）
    # =========================================================================
    # 逻辑：RAG 系统第一道门槛是对齐意图与安全边界。拒答判错是最严重的业务事故。
    if not refusal_correct:
        # 分支 1.1：该拒答但没拒答（漏防 -> 产生致命幻觉）
        if should_refuse and not actual_refused:
            # 现象：用户问了知识库完全没有的事，系统却一本正经地胡说八道。
            # 动作：收紧边界判决 Prompt（如增加“无依据直接回答无法解答”），调高相似度截断阈值。
            return BadCaseRule(is_bad_case=True, category="context_judge_too_loose")

        # 分支 1.2：不该拒答却拒答了（误杀 -> 损伤产品可用性）
        # 现象：知识库有明确证据，系统却回复“查不到，无法回答”，导致用户体验极差。
        # 动作：排查是否检索阈值定得过高导致有效材料被过滤，或放宽判断模型的严格程度。
        return BadCaseRule(is_bad_case=True, category="context_judge_too_strict")

    # =========================================================================
    # Level 2：检索证据链彻底脱节（引用未命中）
    # =========================================================================
    # 【防御关键】：必须显式判断 `is False`，绝对不能写成 `if not citation_hit`！
    # 细节：对于正确拒答的 Case，citation_hit 是 None（合理无需引用）。
    #       如果用 `not citation_hit`，None 也会转为 True，导致正常拒答的用例被冤枉判定为坏例。
    if citation_hit is False:
        # 现象：模型生成了回答，但引用的材料里完全不包含期望文档或关键词。
        # 根因：检索环节没有召回对齐的事实论据，模型回答成了空中楼阁。
        # 动作：检查向量库索引、切分块（Chunking）是否割裂了文档名与上下文。
        return BadCaseRule(is_bad_case=True, category="embedding_recall_miss")

    # =========================================================================
    # Level 3：RAGAS 专业四维量化评估（按流水线逻辑短路：召回 -> 排序 -> 幻觉 -> 相关）
    # =========================================================================

    # 3.1 召回率过低（检索根本没查到关键信息）
    # 漏斗位置：检索阶段（Retrieval）
    if _is_low(context_recall):
        # 动作：换用更强语义的 Embedding 模型、开启 BM25 稀疏检索做混合搜索、扩大初始检索 Top-K。
        return BadCaseRule(is_bad_case=True, category="embedding_recall_miss")

    # 3.2 精准率过低（排序错乱，噪声切片抢占黄金窗口）
    # 漏斗位置：重排阶段（Rerank）
    if _is_low(context_precision):
        # 动作：引入 Cross-Encoder 架构的 Rerank 模型，确保核心论据排在前 3 位，过滤无关噪声。
        return BadCaseRule(is_bad_case=True, category="rerank_order_error")

    # 3.3 忠实度过低（材料找对了也排对了，但大模型自由发挥、虚构事实）
    # 漏斗位置：大模型生成阶段（Generation - Factuality）
    if _is_low(faithfulness):
        # 动作：在 System Prompt 中加强约束：“仅依据参考材料回答，严禁加入常识推演与未提及内容”。
        return BadCaseRule(is_bad_case=True, category="generation_off_context")

    # 3.4 回答相关性过低（材料没问题、也没有胡编，但答非所问、抓不住用户核心诉求）
    # 漏斗位置：大模型表述阶段（Generation - Relevancy）
    if _is_low(answer_relevancy):
        # 动作：优化 Prompt 结构（加入任务拆解、Few-Shot 优质范式），约束输出的行文重点。
        return BadCaseRule(is_bad_case=True, category="prompt_constraint_weak")

    # =========================================================================
    # 恭喜！一路顺畅通过所有严苛门槛，判定为完全合格的优质样例
    # =========================================================================
    return BadCaseRule(is_bad_case=False, category=None)


def _is_low(score: float | None) -> bool:
    """辅助判定指标是否“显著低于预期阈值”。

    【防御性设计 - 缺失值（None）安全处理】：
    - 若某一外部指标返回 None（可能由于网络超时、模型评估服务中断导致未打出分），
      此处必须判定为 False（不视为分数过低）。
    - 理由：未成功产出评分是“系统运行缺省”，绝不能混淆为“算法效果低分”，
      防止因服务偶发抖动而错误给算法策略扣上 Bad Case 的帽子。
    """
    return score is not None and score < _LOW_SCORE_THRESHOLD