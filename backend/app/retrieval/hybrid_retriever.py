"""混合检索器：向量 + 中文全文双路召回 + RRF 融合。

【为什么用 RRF 而不是直接相加两路分数】：
1. 两路分数量纲完全不同（cosine sim ∈ [0,1] vs ts_rank ∈ [0,+∞)）。
   向量分是"方向夹角的余弦"，被约束在 [0,1]；ts_rank 是"词频与权重的累积"，
   取值无上界、随语料与词频浮动。把 0.7 和 0.09 相加，那个 0.09 既可能意味着
   "完美命中"也可能意味着"勉强沾边"——加起来没有物理意义。
2. RRF 只看排名，不依赖分数尺度，是工业界混合检索的事实标准。
   排名天然消解并列（ts_rank 实测大量重复值），也天然规避量纲问题。
3. 公式：score(d) = Σ_i 1 / (k + rank_i(d))，rank 从 1 开始（"第 1 名"而非"第 0 名"）。
4. 常数 k（默认 60）越小越偏向高排名条目；越大越平滑，越依赖"两路都命中"这件事本身。

【实现选择】：
在应用层用 dict 累加，而不是写一条 FULL OUTER JOIN 大 SQL。
目的是让学习者能逐行看清楚 RRF 融合到底在做什么——SQL 版本会把融合逻辑藏进
数据库执行计划里，调试与教学都困难。当前语料规模下，两次查询 + Python 侧合并
的开销可以忽略。

【调用方须知】：
本类刻意【不接收外部 session】，而是每次检索自己开两个独立会话。
原因见 HybridRetriever 类文档。
"""

import asyncio
from uuid import UUID

# 第 9 期：观测 SDK 的装饰器。本模块走裸 SQLAlchemy，SDK 自动捕获不到，
# 必须手动打点才能让 trace 树里出现"检索"这一层。
from langsmith import traceable

# 引入全局系统配置单例（读取 rrf_k 等融合参数）
from app.core.config import settings
# 引入统一日志工厂
from app.core.logging import get_logger
# 引入会话工厂：本模块刻意自行创建会话，不复用调用方的 session
from app.db.session import AsyncSessionLocal
# 引入两路检索器：混合检索的两条腿
from app.retrieval.keyword_retriever import KeywordRetriever
# 引入统一检索数据契约与向量检索器
from app.retrieval.vector_retriever import RetrievedChunk, VectorRetriever

# 模块级 logger：日志里会带上本模块的 __name__，便于按模块过滤
logger = get_logger(__name__)


class HybridRetriever:
    """混合检索器：两路独立 session 并发召回，再用 RRF 融合成一份结果。

    【刻意不接收外部 session】：
    - SQLAlchemy 的 AsyncSession 【不支持并发执行】：一个 session 对应一个数据库连接
      与一个事务状态，两路 gather 共用一个 session 会让两条语句在同一连接上交错下发，
      把底层 asyncpg 连接搞成 InFailedSQLTransactionError（事务已失败，后续语句全部拒绝）。
    - 检索过程纯读，不写库，因此与调用方的写事务（落库 user / assistant 消息）
      天然解耦——各自开会话反而更干净，不会因为检索失败而污染调用方事务。
    """
    # 说明：类里没有任何 __init__，也刻意不保存任何实例状态。
    # 两个原因：① 会话是每次 search 现场创建的，不能长期挂在实例上（否则并发调用会互相污染）；
    #          ② 无状态对象天然线程/协程安全，可以被路由层安全地复用或每次 new 一个。

    # 【第 9 期】手动打点：本方法内部是裸 SQLAlchemy 查询，不是 LangChain 对象，
    #   不装饰则 trace 树里"混合检索"这一层缺失，也看不到它耗了多久。
    @traceable(name="HybridRetriever.search", run_type="retriever")
    async def search(
        self,
        query: str,
        *,
        recall_top_k: int,
        final_top_k: int,
        permission_tags: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """两路并发召回 + RRF 融合 + 取 final Top-K。

        【为什么用 `*` 强制关键字传参】：
        recall_top_k 与 final_top_k 都是 int，位置传参极易写反（写反的后果是
        "用 5 去召回、再截到 20"，静默地少召回一批候选，不报错）。
        强制关键字后，写反不可能编译通过。

        【任一路异常都退化为另一路结果】：
        避免一处抖动阻断整个问答。例如某次 embedding 服务 5xx，向量路返回空，
        关键词路照常工作，整个回答还是能拿到关键词路的结果；反之也一样。

        :param query: 用户原始提问（两路使用同一个查询串，各自内部按自身语义处理）
        :param recall_top_k: 单路召回宽度（应大于 final_top_k，为融合留足候选）
        :param final_top_k: 融合后交给 LLM 的最终切片数
        :param permission_tags: 【第 9 章新增】调用方有效权限标签，**同时**下发给两路。
                                **None = 不做权限过滤**（admin / 离线评测 / 启动期种子）。
                                【为什么在这里就分发给两路、而不是融合后再过滤】：
                                过滤必须发生在 SQL（召回阶段），而不是融合之后 ——
                                否则无权分块会先占掉 top_k 名额，把有权分块挤出候选窗口，
                                结果是"权限过滤生效了，但用户能拿到的资料也变少了"。
        :return: 按 RRF 融合分降序排列的 RetrievedChunk 列表
        """
        # 元组解包：gather 的返回顺序【严格等于】传入参数的顺序，
        # 因此第 1 个协程的结果必然落在 vector_hits，第 2 个落在 keyword_hits。
        # 注意：gather 不保证"谁先跑完谁在前"，只保证"按传入顺序返回"。
        vector_hits, keyword_hits = await asyncio.gather(
            # 第 1 路：向量检索。传入【类对象】而非实例——实例化由 _safe_search 在
            # 它自己新建的会话里完成，这样两路才能各持一个独立会话。
            self._safe_search(
                VectorRetriever, query, recall_top_k, "vector", permission_tags
            ),
            # 第 2 路：关键词检索。与第 1 路走【完全相同的代码路径】，
            # 只是换了类对象与日志标签——这正是统一 RetrievedChunk 契约换来的对称性。
            self._safe_search(
                KeywordRetriever, query, recall_top_k, "keyword", permission_tags
            ),
        )
        # 两路结果到手后交给融合函数；这里 return 直接透传，无需再加工：
        # 融合函数已经负责了去重、累加、排序、截断、以及最终契约的构造。
        return rrf_fuse(
            # 向量路结果（可能为空列表——该路降级时）
            vector_hits=vector_hits,
            # 关键词路结果（同样可能为空列表）
            keyword_hits=keyword_hits,
            # k 从全局配置读取（默认 60），不硬编码：便于不改代码就调融合平滑度
            k=settings.rrf_k,
            # 最终交给 LLM 的条数，与召回宽度 recall_top_k 是两个不同的口径
            top_k=final_top_k,
        )

    # @staticmethod：本方法不需要 self，也不读写任何实例状态。
    # 好处是两路调用形式完全一致（都是 _safe_search(类, 查询, 条数, 标签, 权限标签)），
    # 且可以直接被单元测试单独调用，无需先构造 HybridRetriever 实例。
    @staticmethod
    async def _safe_search(
        retriever_cls: type[VectorRetriever] | type[KeywordRetriever],
        query: str,
        top_k: int,
        label: str,
        permission_tags: list[str] | None,
    ) -> list[RetrievedChunk]:
        """单路检索的异常兜底：任何异常都降级为"空结果 + 记日志"，绝不向上抛。

        把异常兜底封装成静态方法，让两路都使用统一的
        「单路异常 → 返回空列表 + 记日志，不阻断整条问答」语义。
        两路走完全一致的代码路径，也正是第 6 期统一 RetrievedChunk 契约的收益兑现。

        【为什么必须自己开会话】：
        见 HybridRetriever 类文档——AsyncSession 不支持并发，两路必须各持一个。

        【except Exception 是否过宽】：
        这里是有意为之。检索是"尽力而为"的旁路能力，任何一路失败都不应让用户看不到回答；
        且失败已被完整记入日志（含堆栈），可排查、可告警，不存在"异常被吞掉无从查起"的问题。

        【第 9 章 · permission_tags 为什么是【必传】的普通参数，而不是带默认值的可选参数】
        这个方法是本模块内部的私有函数，唯一调用方就是上面的 search()。
        写成必传位置参数，等于让编译器帮忙保证"两路都别漏传"——
        如果给它 `= None` 默认值，将来新加一路检索时忘了传，就会静默变成"不过滤"，
        那正是最危险的失效方式（越权召回且无任何报错）。宁可传参麻烦一点。

        :param retriever_cls: 检索器类（向量或关键词），由调用方传入以保证两路对称
        :param query: 查询文本
        :param top_k: 本路召回条数
        :param label: 日志标签，纯为排查时看清是哪一路挂了
        :param permission_tags: 调用方有效权限标签；None = 不做权限过滤
        :return: 本路召回结果；异常时返回空列表
        """
        # try 包住整个"开会话 + 检索"过程，而不仅仅是 search() 那一行：
        # 因为 AsyncSessionLocal() 本身（获取连接）、检索器构造、以及 search 内部
        # 的向量化/建连/执行 SQL 都可能抛异常，全部都要落在同一套降级语义里。
        try:
            # async with 保证无论正常返回还是异常退出，会话都会被关闭并归还连接池。
            # 这是"自己开会话"的配套纪律：不 close 会耗尽连接池，表现为后续请求全部卡住。
            async with AsyncSessionLocal() as session:
                # 用当前这一路专属的会话实例化检索器（两路各持一个，互不干扰）。
                # retriever_cls 由参数传入，所以这一行对向量路与关键词路完全相同。
                retriever = retriever_cls(session)
                # await 本路检索：向量路内部会调 embedding 模型，关键词路内部是纯 SQL；
                # 两者耗时不同，但都通过 async I/O 让出事件循环，因此能被 gather 真正并发。
                # 【第 9 章】把权限标签继续往下传，最终会变成 SQL 里的可见性 WHERE。
                return await retriever.search(
                    query, top_k, permission_tags=permission_tags
                )
        # 兜住一切异常（含超时、网络、SQL、模型返回异常结构等）。
        # 注意顺序：except 必须在 async with 之外，否则会话退出时的清理异常会被漏掉。
        except Exception:
            # logger.exception 会【自动附上当前异常堆栈】——这正是"宽兜底不等于吞异常"的关键：
            # 返回空列表让用户无感，同时日志里留下可排查、可告警的完整现场。
            # label 参数把"哪一路挂了"写进日志，否则两路日志长得一模一样，无法区分。
            logger.exception("hybrid retrieve %s 路异常，降级为空结果", label)
            # 返回空列表而非 None：让 gather 的解包、rrf_fuse 的遍历都能无条件继续，
            # 调用方无需任何 None 判断——空列表本身就是"这一路没有结果"的合法表达。
            return []


def rrf_fuse(
    vector_hits: list[RetrievedChunk],
    keyword_hits: list[RetrievedChunk],
    *,
    k: int,
    top_k: int,
) -> list[RetrievedChunk]:
    """RRF 融合：rank 从 1 开始，分数 = Σ 1/(k + rank)。

    保留两路的 rank / score 与命中来源，便于前端调试面板展示。

    【算法流程】：
    1. 先把向量路结果全部放进 by_id，每条算它的 RRF 贡献 1/(k + rank)；
    2. 再扫一遍关键词路：
       - 已存在的 chunk（两路都命中）→ 累加两路分数，并把 sources 合并成 ("vector","keyword")；
       - 尚不存在的 chunk（仅关键词路命中）→ 直接以关键词路身份放进 by_id。
    3. 最后整体按 rrf_score 降序，截到 top_k。

    第 2 步的"尚不存在则新建"分支是关键词腿存在的意义所在：
    没有它，关键词路就只能在向量路已召回的候选里重排，永远无法引入向量路遗漏的文档。

    :param vector_hits: 向量路召回结果（已按相似度降序）
    :param keyword_hits: 关键词路召回结果（已按 ts_rank 降序）
    :param k: RRF 平滑常数（默认取 settings.rrf_k = 60）
    :param top_k: 融合后保留条数
    :return: 融合并按 rrf_score 降序排列的 Top-K 列表
    """
    # 以 chunk_id 为键聚合：同一个切片可能被两路同时召回，需要在同一条记录上累加
    # 用 dict 而非 list 的三个理由：① 按 id 去重是融合的前提；② 查找是 O(1)，避免两层循环 O(n²)；
    # ③ chunk_id 是 UUID（不可变、可 hash），天然适合当键。
    by_id: dict[UUID, RetrievedChunk] = {}

    # 1. 向量路：先建立基线，为每条切片算出 RRF 基础分
    # enumerate(..., start=1)：rank 从 1 起，与 RRF 公式的"第 1 名"口径一致
    # （若从 0 起，第 1 名会算成 1/k，与文档、与另一路的编号习惯都不一致）。
    for rank, hit in enumerate(vector_hits, start=1):
        # 注意这里赋的是 _with_vector 的【新对象】，而不是修改 hit：
        # RetrievedChunk 是 frozen dataclass，本身不可改，只能构造新对象来承载融合信息。
        by_id[hit.chunk_id] = _with_vector(hit, rank=rank, k=k)

    # 2. 关键词路：已存在的累加分数，不存在的直接放入
    for rank, hit in enumerate(keyword_hits, start=1):
        # .get() 而非 []：仅关键词路命中的切片在 by_id 里不存在，
        # 用 [] 会 KeyError；用 .get() 得到 None，正好作为"两路是否都命中"的判据。
        existing = by_id.get(hit.chunk_id)
        # 判据关键点：by_id 的【值域是 RetrievedChunk，永远不为 None】，
        # 因为三个工具函数没有一个返回 None。所以 `is None` 精确等价于"键不存在"。
        if existing is None:
            # 仅关键词路命中：以关键词身份新建。这类切片正是向量路的盲区
            # （型号/接口路径/错误码等字面精确匹配），必须保留
            # ——没有这个分支，关键词路就只能在向量路的结果里重排，整条关键词腿形同虚设。
            by_id[hit.chunk_id] = _with_keyword(hit, rank=rank, k=k)
        else:
            # 同 chunk 跨两路命中：合并 sources / 累加 RRF 分数
            # 这是 RRF"两路都命中会自动上浮"的实现位置：
            # 两路都排第 5（2×0.01538=0.03077）也会高过单路排第 1（0.01639）。
            by_id[hit.chunk_id] = _merge_keyword_into(existing, hit, rank=rank, k=k)

    # 3. 全局按融合分降序排序后截断
    #    rrf_score or 0.0：防御性兜底，理论上所有条目都已被赋分，但避免 None 参与比较抛 TypeError
    #    .values() 只取记录本身（键 chunk_id 已经冗余在记录内部，不再需要）。
    fused = sorted(
        by_id.values(),
        # key 取 rrf_score：因为融合之后排序依据已不是任何单路原始分，而是融合分本身。
        # or 0.0 的两点作用：① 类型上 rrf_score 是 float | None，None 参与 < 比较会 TypeError；
        # ② 兜住任何意外路径下的 None。注意 0.0 or 0.0 == 0.0，所以合法零分不会被算错。
        key=lambda c: c.rrf_score or 0.0,
        # reverse=True 即降序：RRF 分越大越相关，要排在最前。
        reverse=True,
    )
    # 切片截断到 top_k：sorted 已经把【全部候选】排好序，这里只是取前 N 条。
    # 用切片而非提前 break，是因为排序本身必须先看到全量才能保证顺序正确。
    return fused[:top_k]


# ==============================================================================
# 以下 3 个辅助函数都是纯函数：
# 把「构造一个新 RetrievedChunk 并填好对应字段」的细节封装起来。
# 纯函数意味着同样输入必得同样输出、不修改入参，因此融合逻辑本身可以被单独推理与测试。
# ==============================================================================
def _with_vector(hit: RetrievedChunk, *, rank: int, k: int) -> RetrievedChunk:
    """把一条「仅向量路命中」的切片转成融合视图。

    注意 score 被改写成 rrf_score：因为融合之后，排序依据已经不再是余弦相似度，
    而是 RRF 融合分（这正是第 6 期契约里 score = "本次排序依据"的语义）。
    原始相似度仍完整保留在 vector_score 里，供调试面板与落库使用。
    """
    # RRF 公式本体：只用到 rank（名次），完全不用原始分数——这就是"不依赖量纲"的落地。
    rrf_score = 1.0 / (k + rank)
    # 构造【新的】RetrievedChunk（frozen dataclass 不可原地修改），逐字段搬运：
    return RetrievedChunk(
        # 以下 6 个业务字段原样搬运，融合不改变切片内容与溯源信息
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_name=hit.document_name,
        content=hit.content,
        page_no=hit.page_no,
        section_path=hit.section_path,
        # score 改写为 RRF 分：融合后它代表"本次排序依据"，不再是余弦相似度
        score=rrf_score,
        # 命中来源标记为单路向量，前端据此知道这条只被一条腿看中
        sources=("vector",),
        # 保留向量路名次（1 起），供调试面板与 retrieval_meta 落库
        vector_rank=rank,
        # 原始余弦相似度原样保留，绝不丢失：原始分与排序分是两个层级的信息
        vector_score=hit.vector_score,
        # RRF 融合分，与 score 同值；单独留一个字段是为了语义显式（可与 score 分别取值）
        rrf_score=rrf_score,
    )


def _with_keyword(hit: RetrievedChunk, *, rank: int, k: int) -> RetrievedChunk:
    """把一条「仅关键词路命中」的切片转成融合视图。

    与 _with_vector 严格对称，只有 sources 与填充的字段不同。
    这类切片在最终结果里 vector_rank / vector_score 均为 None——
    这不是"缺数据"，而是如实表达"向量路没有召回它"，正是拒答判定需要的信息。
    """
    # 与 _with_vector 完全相同的公式：RRF 对两路一视同仁，不因为来源不同而加权
    # （这也正是 RRF 抗量纲差异的原因——它压根不看分数量级）。
    rrf_score = 1.0 / (k + rank)
    # 构造新的融合视图，字段搬运方式与 _with_vector 严格对称
    return RetrievedChunk(
        # 业务字段原样搬运
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_name=hit.document_name,
        content=hit.content,
        page_no=hit.page_no,
        section_path=hit.section_path,
        # score 同样改写为 RRF 分
        score=rrf_score,
        # 来源标记为单路关键词
        sources=("keyword",),
        # 保留关键词路名次（1 起）
        keyword_rank=rank,
        # 原始 ts_rank 原样保留（无上界、只能在本路内部比较，绝不可跨路比较）
        keyword_score=hit.keyword_score,
        # RRF 融合分
        rrf_score=rrf_score,
        # 【关键】此处刻意不填 vector_rank / vector_score，让它们保持默认值 None。
        # None 表示"向量路没有召回它"——这是信息，不是缺数据。
        # 若图省事填成 0，该切片会被当成"向量路第 0 名"，凭空获得 1/(k+0) 的超高分。
    )


def _merge_keyword_into(
    existing: RetrievedChunk,
    keyword_hit: RetrievedChunk,
    *,
    rank: int,
    k: int,
) -> RetrievedChunk:
    """把关键词路的贡献合并进一条「已经带有向量信息」的切片。

    existing 已经携带向量路的 rank / score，因此只需要把关键词路的
    rank / score 补上，并把两路的 RRF 贡献相加。

    【为什么用 (existing.rrf_score or 0.0) 而不是直接相加】：
    existing.rrf_score 理论上必然有值（_with_vector 已赋分），
    但类型标注是 float | None，用 or 0.0 可以同时安抚类型检查器并防住意外 None。
    """
    # 两路贡献累加：这正是 RRF "两路都命中会自动上浮" 的实现位置
    # existing.rrf_score 是向量路的贡献（1/(k+v_rank)），1/(k+rank) 是关键词路的贡献。
    new_rrf = (existing.rrf_score or 0.0) + 1.0 / (k + rank)
    # 以 existing 为基底构造新对象：向量路的信息全部保留，只补关键词路的字段
    return RetrievedChunk(
        # 业务字段全部沿用 existing（同一条切片，内容与溯源自然相同）
        chunk_id=existing.chunk_id,
        document_id=existing.document_id,
        document_name=existing.document_name,
        content=existing.content,
        page_no=existing.page_no,
        section_path=existing.section_path,
        # score 更新为累加后的融合分
        score=new_rrf,
        # 来源合并成两路皆命中：前端据此高亮"这条被两条腿同时看中"
        sources=("vector", "keyword"),
        # 向量路的 rank / score 取自 existing（这一步是"保留"，不是"重算"）
        vector_rank=existing.vector_rank,
        vector_score=existing.vector_score,
        # 关键词路的 rank / score 取自本次传进来的 keyword_hit（新补上的信息）
        keyword_rank=rank,
        keyword_score=keyword_hit.keyword_score,
        # 融合后的总分
        rrf_score=new_rrf,
    )
