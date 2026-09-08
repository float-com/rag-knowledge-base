"""
【模块职责说明】
本模块为知识库离线摄取流水线中的「文本切分适配层」（Text Splitter / Chunking Module）。
文档经 Docling 转换为完整的 Markdown 文本后，由于篇幅较长、语义范围较宽，直接喂给 Embedding 模型
会导致关键信息稀释且超出大模型上下文窗口。本模块的核心作用如下：

1. 层次化语义递归切分（Recursive Chunking）：
   封装 LangChain 的 RecursiveCharacterTextSplitter，优先按自然段落（\n\n）、单换行（\n）及
   标点符号递进切分，最大限度保留上下文语义完整性，避免生硬截断句意。

2. 本地化中文标点适配：
   默认的英文分隔符不适合中文长段落；在此定制扩展了中文字符边界（。！？；，），
   使中文文档能在自然的标点停顿处断句。

3. 切片元数据增强（Metadata Enrichment）：
   在文档分块后，为每一个 chunk 动态注入两项关键元数据：
   - `chunk_index`：切片相对整篇文档的逻辑顺序编号，用于后续检索召回结果的时序排序、上下文拼接与前端高亮还原。
   - `chunk_hash`：基于切片正文计算的 MD5 指纹摘要，为后续向量库增量更新、去重及缓存命中判定提供唯一幂等标识。
"""

import hashlib

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings


def _build_splitter() -> RecursiveCharacterTextSplitter:
    """构建并配置针对中文优化的递归字符切分器实例。"""
    # 语法（外部框架 LangChain）：RecursiveCharacterTextSplitter(chunk_size, chunk_overlap, ...)
    #   参数1 (chunk_size: int)：单个文本块的最大字符数上限（读取系统全局统一配置项）
    #   参数2 (chunk_overlap: int)：相邻文本块之间的滑动重叠字符数（防止核心语义在切分边界被截断）
    #   参数3 (separators: list[str])：切分优先级列表，由大到小逐级尝试切分，兜底为空字符串（逐字切断）
    #   参数4 (length_function: Callable)：计算文本长度的度量函数，使用 Python 内建 len 统计字符数
    #   参数5 (is_separator_regex: bool)：指定 separators 是否为正则表达式模式，此处设为 False 代表纯字符串匹配
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        # 中文场景下默认分隔符过于偏向英文，这里加入常见中文全角标点；空串作为最后兜底
        #   按优先级递归切分：\n\n(自然段) -> \n(换行) -> 。！？(完整句) -> ；(并列句) -> ，(短语) -> 空格(英文词界) -> 空串(无标点超长文本逐字硬截断兜底)
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
        length_function=len,
        is_separator_regex=False,
    )


def split(documents: list[Document]) -> list[Document]:
    """切分输入的文档列表，并自动补齐 chunk 级别的索引与指纹元数据。

    【参数说明】：
    - documents: 待切分的 LangChain Document 列表（通常由 parser 模块解析后传入）

    【返回值】：
    - list[Document]: 切分后的细粒度切片实体列表，继承了父文档的元数据并新增 chunk_index 与 chunk_hash
    """
    # 语法（业务私有方法）：调用切分器构造工厂，获取配置好中文边界的分块引擎
    splitter = _build_splitter()

    # 语法（外部框架 LangChain）：splitter.split_documents(documents)
    #   参数1 (documents: list[Document])：输入的顶层文档列表
    #   方法特性：对列表中每个 Document 执行递归拆解，同时自动克隆继承父级 metadata（如 source 文件名）
    chunks = splitter.split_documents(documents)

    # 语法（Python 原生内建）：enumerate(iterable, start=0)
    #   参数1 (iterable: list[Document])：待遍历的切片列表
    #   语法解包 (index, chunk)：同时获取当前元素从 0 开始的递增索引序号与 Document 实例
    for index, chunk in enumerate(chunks):
        # 语法（Python 原生字典写入）：chunk.metadata["key"] = value
        #   metadata 说明：LangChain Document 的字典属性，写入切片时序编号，用于后续排序与定位
        chunk.metadata["chunk_index"] = index

        # 语法（外部框架 LangChain 属性访问）：chunk.page_content -> str
        #   属性说明 (page_content)：Document 实例的核心正文载荷，存储该切片所包含的 Markdown/纯文本字符串
        # 语法（Python 原生字符串编码）：str.encode(encoding="utf-8") -> bytes
        #   参数1 (encoding: str)：指定将切片正文字符串转换为 utf-8 二进制字节流，以满足哈希计算对二进制输入的要求
        # 语法（标准库 hashlib）：hashlib.md5(data).hexdigest() -> str
        #   参数1 (data: bytes)：待计算摘要的原始二进制字节切片
        #   方法调用 (hexdigest())：导出 32 位的十六进制散列字符串，作为切片内容防重与增量同步比对的唯一内容指纹
        # 语法（Python 原生字典写入）：chunk.metadata["chunk_hash"] = ...
        #   键名赋值 (["chunk_hash"])：动态向元数据字典注入内容哈希指纹
        chunk.metadata["chunk_hash"] = hashlib.md5(
            chunk.page_content.encode(encoding="utf-8")
        ).hexdigest()

    # 返回补齐元数据后的切片实体列表
    return chunks