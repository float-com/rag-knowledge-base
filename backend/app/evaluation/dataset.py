"""评测集加载与校验解析引擎（JSONL Parser）。

【模块架构定位与设计哲学】：
1. 静态数据向领域实体的高壁垒映射（Raw JSON to Domain Contract）：
   - JSONL 文本行属于弱类型的外部易变数据，本模块作为物理文件与 RAG 执行引擎之间的第一道安全关卡；
   - 将弱类型 Python 字典（dict）严格映射为不可变对象（Frozen Dataclass），杜绝运行时属性被意外覆写，
     同时赋予下游 IDE 与类型检查器（Mypy/Pyright）完备的代码补全与静态推导能力。

2. 运维友好型错误归一化设计（Operational-First Error Surfacing）：
   - 评测集文件通常由人工（算法工程师、标注人员、业务专家）在本地或在线编辑器中手填维护，
     人工书写不可避免会出现 JSON 逗号遗漏、括号未闭合或 Schema 键名敲错；
   - 若系统仅粗暴抛出 `JSONDecodeError` 或通用 500 错误，排查人员面对数百行的文件必须借助二分法逐行排查；
   - 本模块建立**精确到行号（Line-Level Precision）与字段名（Field-Level Precision）**的错误感知体系，
     无论语法崩坏还是契约缺失，均能精准指出“第几行第几个键”，实现毫秒级人工定位与修复。

3. 计算轻量化与元数据下沉（Metadata Discovery）：
   - 在前端创建评测会话（Run）的下拉选单阶段，需要展示数据集规模并据此固化 Run 的 `dataset_size`；
   - 本模块提供轻量级的流式探测能力，不全量将 JSON 反序列化进内存，以最小的 I/O 代价完成条目发现。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import NotFoundError, ValidationError

# 评测集持久化目录基准路径（默认位于当前模块平级下的 datasets/ 目录）。
#   【路径解析原理】：
#   1. `__file__` 获取当前 `.py` 源码文件的绝对/相对物理路径；
#   2. `.resolve()` 展开符号链接并规范化解析为绝对路径（Absolute Path），防止因当前工作目录（CWD）切换引发路径失效；
#   3. `.parent` 获取当前文件所在目录，并通过 `/ "datasets"` 操作符构造目标目录对象；
#   4. 适用性：本模块为常规 Python 文件（Regular Module），非 `__init__.py` 缺失的隐式命名空间包，故 `__file__` 恒定安全存在。
DATASETS_DIR = Path(__file__).resolve().parent / "datasets"


@dataclass(frozen=True)
class EvaluationCase:
    """单条评测样本的强类型领域模型（不可变值对象）。

    【核心设计考量】：
    - `frozen=True`：将实体声明为不可变对象（Immutable），阻止任何执行流程在运行时篡改测试用例的
      问题、预期答案或断言规则，保障评测实验的可复现性与基线纯洁度；
    - 数据类仅承载纯净属性，不包含复杂业务行为，作为只读契约在评测管线中流转。

    【引用命中（Citation Hit）判定策略协同说明】：
    评测框架在后续评估 RAG 实际返回的引用证据（citations）时，采用双重宽松匹配策略：
    1. 文档名命中：实际引用中的来源文件名完全或部分命中了 `expected_document_names`；
    2. 关键词命中：实际引用切片的文本片段（quote）命中了 `expected_keywords` 中的任意一个核心词。
    任一条件成立即代表该 Case 的证据链路可信，有效包容了切片切分粒度不同导致的来源微小偏差。
    """

    # 用例业务编号（如 "LAW-001"、"FIN-102"），用于跨批次评测时追踪同一用例的指标波动
    case_id: str
    # 送入 RAG 问答链路的原始用户提问 Prompt
    question: str
    # 标准参考答案（Ground Truth），用于作为 RAGAS 上下文召回率评估及事实比对的基准文本
    expected_answer: str
    # 期望回答必须引用的核心源文档名称集合
    expected_document_names: list[str]
    # 期望答案或引用内容中必须包含的关键事实实体、专业术语列表
    expected_keywords: list[str]
    # 该用例是否属于越界/违规/无答案而必须被系统主动拒答的边界探测样本
    should_refuse: bool
    # 维度分类标签集合（如 ["长文档", "表格计算", "多跳推理"]），用于评测大盘的分类下钻
    tags: list[str]


def list_datasets() -> list[tuple[str, int]]:
    """扫描 `datasets/` 目录下的全部 JSONL 文件，并汇总返回评测集元数据。

    【吞吐与性能权衡（Why Counting Now）】：
    - 前端在展示“新建评测”表单时，下拉列表不仅需要名称，更需要直观展示“该样本集包含多少条 Case”；
    - 后端在持久化 `EvaluationRun` 实体时，必须立刻固化 `dataset_size` 作为后续计算百分比进度的稳定分母；
    - 采用纯文本流式行扫描（`sum(1 for ...)`），不执行昂贵的 `json.loads` 反序列化，
      能够在数毫秒内完成数十万行文本的纯 I/O 行统计，彻底避免了前后端两次往返通信的开销。

    :return: 按文件名称字典序升序排列的二元组列表，格式为 `[(评测集名称去除后缀, 有效样本行数), ...]`；
             若底层目录尚未创建或不存在，防御性返回空列表 `[]`。
    """
    # 目录存在性防空检查：防止服务冷启动或本地未配置评测集目录时直接崩溃
    if not DATASETS_DIR.exists():
        return []

    results: list[tuple[str, int]] = []
    # 1. 遍历排序后的路径对象
    # sorted() 将 glob 返回的文件按字母顺序升序排列，避免不同系统下遍历顺序混乱
    for path in sorted(DATASETS_DIR.glob("*.jsonl")):
        # 2. 安全打开文件（上下文管理器）
        # path.open() 是 pathlib.Path 对象自带的方法，等同于内置的 open(path, ...)
        # "r": 只读模式；encoding="utf-8": 避免跨平台中文编码报错
        # with 语句确保代码块执行完毕（或中途报错）时，文件会被自动关闭释放
        with path.open("r", encoding="utf-8") as f:
            # 3. 统计非空行数（生成器表达式 + sum）
            # for line in f: 逐行流式读取文件，不会一次性把整个大文件载入内存
            # line.strip(): 剔除每行首尾的空白字符（空格、制表符 \t、换行符 \n）
            # if line.strip(): 条件判断，如果剔除空白后内容非空（真值），才保留
            # 1 for line ...: 只要遇到符合条件的有效行，就产出一个数字 1
            # sum(...): 对产出的所有 1 进行累加求和，得到有效样本总行数
            count = sum(1 for line in f if line.strip())

        # 4. 提取主文件名并存入结果列表
        # path.stem: pathlib 的属性，获取不带扩展名的文件名（如 "test.jsonl" -> "test"）
        # (path.stem, count): 构造一个元组 (tuple)，包含“纯文件名”和“样本行数”
        # results.append(...): 将这个元组追加存入外部的 results 列表中
        results.append((path.stem, count))

    return results


def load_dataset(name: str) -> list[EvaluationCase]:
    """根据评测集唯一标识名称，加载并全量校验解析一份评测集文件。

    【执行与容错机制】：
    1. 路径定位与存在性校验：直接定位目标文件，若不存在则显式向上抛出业务级 `NotFoundError`；
    2. 逐行游标遍历：采用缓冲迭代器按行读取大文件，内存占用恒定；
    3. 行号关联错误封装：通过 `enumerate(..., start=1)` 将物理行号从 1 开始记录，任何解析故障
       均会被捕获并转化为携带物理行号的 `ValidationError`，由全局异常处理器直译为 HTTP 400 状态码。

    :param name: 评测集文件名称（不包含 `.jsonl` 扩展名，如 `seed`、`smoke`）
    :return: 按照文件中声明顺序由上至下解析出的 `EvaluationCase` 不可变实例列表
    :raises NotFoundError: 找不到指定名称的评测集文件（对外返回 HTTP 404）
    :raises ValidationError: 目标文件中存在语法损坏的非标准 JSON 行，或缺失必填契约字段（对外返回 HTTP 400）
    """
    # 1. 拼出目标文件的完整路径
    # 作用：把“文件夹目录”和“文件名.jsonl”拼在一起（比如 "data" 和 "smoke" 拼成 "data/smoke.jsonl"）
    # 注意：这里的 / 不是除法，是专门用来拼路径的符号，它会自动补上中间的斜杠 /，不用担心字符粘连
    path = DATASETS_DIR / f"{name}.jsonl"

    # 显式校验文件物理存在性；若文件丢失，抛出业务级异常（会被外部框架映射为 HTTP 404）
    if not path.exists():
        raise NotFoundError(f"评测集不存在: {name}")

    # 预分配空列表，类型注解指明其中存放的是 EvaluationCase 领域模型对象
    cases: list[EvaluationCase] = []

    # 2. 安全打开文件
    # path.open() 是 Path 对象的语法糖；utf-8 避免跨平台编码错乱；with 确保退出时释放文件句柄
    with path.open("r", encoding="utf-8") as f:

        # 3. 带物理行号的流式遍历
        # enumerate(f, start=1):
        # - f 作为可迭代对象是流式懒加载的，不会把整份大文件一次性载入内存
        # - start=1 强制行号从 1 开始累加（与 VS Code、Sublime 等文本编辑器的行号完全对齐）
        for line_no, raw in enumerate(f, start=1):

            # 去除每行首尾的所有空白字符（包含空格、\t、\r、\n）
            line = raw.strip()

            # 空行容错：若剥离后为空字符串（False），跳过当前循环，防止将空行误当做损坏数据报错
            if not line:
                continue

            # 4. JSON 反序列化与语法容错（将纯文本字符串解析为 Python 字典）
            try:
                # 【注意不是“转成 JSON”，而是“解开 JSON”】：
                # 读出来的 line 本质上是一串纯文本字符串（如 '{"prompt": "你好"}'，无法用 key-value 读数据）。
                # json.loads (load string) 负责做反序列化：把符合 JSON 规范的字符串，翻译成 Python 内存中的原生字典对象 dict。
                # 这样后续代码才能通过 data["prompt"] 直接读取字段。
                data = json.loads(line)

            except json.JSONDecodeError as exc:
                # 【JSON 损坏兜底处理】：
                # 如果文件里某一行格式写坏了（比如漏了双引号、多了逗号、单引号代替双引号等），底层会抛出 JSONDecodeError。
                #
                # 这里做异常转译（重新打包）：
                # 1. exc.msg：提取出底层的具体报错原因（例如 "Expecting property name enclosed in double quotes"）。
                # 2. 上下文绑定：将抽象的系统报错，转为人类和前端能看懂的提示——明确告知是哪个评测集、具体第几行。
                # 3. raise ... from exc（异常链机制）：
                #    抛出系统统一约定的业务异常 ValidationError（对外触发 HTTP 400 报错响应），
                #    同时利用 "from exc" 将原始底层崩溃堆栈挂载在后面，方便后端工程师在日志中追溯底层根本原因。
                raise ValidationError(
                    f"评测集 {name} 第 {line_no} 行 JSON 非法: {exc.msg}"
                ) from exc

            # 5. 数据校验与实体映射
            # 调用私有辅助函数 _parse_case，将原始字典转为强类型的 EvaluationCase 对象
            # 传入 line_no 与 dataset 是为了在字段类型校验失败时（如缺少必填字段），能同样精准报告行号
            cases.append(_parse_case(data, line_no=line_no, dataset=name))

    # 6. 返回全部校验并解析完毕的用例列表
    return cases



# *: 这是一个“分隔符”，规定后面的 line_no 和 dataset 必须以“关键字参数”形式传入（如 line_no=10），防止参数传错位置
def _parse_case(data: dict, *, line_no: int, dataset: str) -> EvaluationCase:
    """将单行原始字典数据映射并清洗为强类型 `EvaluationCase` 实例。

    【字段解析策略（Strict vs. Permissive）】：
    - 绝对必需字段（Fail-Fast 策略）：
      * `id` 与 `question`：采用直接索引 `data["id"]` 与 `data["question"]`。
        若缺失，用例将丧失执行主体与标识依据，失去评测价值，立即触发 `KeyError` 阻断流程；
    - 可选/有默认语义字段（Graceful Fallback 策略）：
      * `expected_answer`：缺失默认回退为空字符串 `""`（如纯边界拒答题可能无需提供详细标准答案）；
      * `expected_document_names` / `expected_keywords` / `tags`：
        采用 `data.get(...) or []` 双重防御，不仅针对 key 缺失，也防止 JSON 中手滑显式写了 `"tags": null`
        时引发类型转换故障，统一下沉为空列表 `[]`；
      * `should_refuse`：缺失默认按标准正面作答用例处理（`default=False`）。

    :param data: `json.loads()` 解析后的原始单行字典
    :param line_no: 当前解析行在原文件中的物理行号（从 1 起始）
    :param dataset: 评测集文件标识名，用于报错时精准归因
    :return: 校验清洗完成的 `EvaluationCase` 实例
    :raises ValidationError: 核心必填字段缺失时抛出，指明缺失的具体属性名
    """
    try:
        # 实例化 EvaluationCase 数据对象，并对各字段进行数据清洗与类型强制转换
        return EvaluationCase(
            # 【策略 A：严格校验必填项】
            # 使用 data["key"] 直取：如果数据中漏写了 "id" 或 "question"，Python 会立刻触发 KeyError
            # str(...)：强制转为字符串，防止用户在 json 中写了数字 ID（如 "id": 1001）导致后续比较逻辑出错
            case_id=str(data["id"]),
            question=str(data["question"]),

            # 【策略 B：选填项带默认值兜底】
            # data.get("key", "")：如果没写该字段，就使用兜底默认值空字符串 ""
            expected_answer=str(data.get("expected_answer", "")),

            # 【策略 C：列表字段的双重防御机制 (None 防御)】
            # 为什么不用 data.get("tags", [])？
            # 因为如果 JSON 里显式写了 `"tags": null`，.get 会拿到 None。
            # 而 None or [] 会利用短路求值将 None 替换为 []，最后由 list(...) 确保拿到的是纯列表类型
            expected_document_names=list(data.get("expected_document_names") or []),
            expected_keywords=list(data.get("expected_keywords") or []),

            # 【策略 D：布尔型字段兜底】
            # 缺失时默认认为不需要拒答（default=False），bool(...) 确保最终值一定是 True 或 False
            should_refuse=bool(data.get("should_refuse", False)),
            tags=list(data.get("tags") or []),
        )
    except KeyError as exc:
        # 【捕获必填字段缺失异常】：
        # 当 data["id"] 或 data["question"] 找不到键时，Python 会抛出 KeyError
        # exc.args[0]：可以直接拿到那个缺失的字段名字（比如字符串 'id' 或 'question'）
        # 将底层晦涩的 KeyError 包装为更人性化的 ValidationError，明确告诉用户哪个评测集第几行缺了哪个字段
        # from exc：保留底层的完整堆栈，方便后台排查
        raise ValidationError(
            f"评测集 {dataset} 第 {line_no} 行缺失字段: {exc.args[0]}"
        ) from exc