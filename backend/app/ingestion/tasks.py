"""Celery 任务定义（生产者与 worker 之间的「契约层」）。

【这个模块为什么存在】
Celery 的 worker 是【同步进程】，而本项目的业务流程全是 async。
两头对不上，中间就需要一层薄薄的适配器 —— 就是这个模块：

    FastAPI 进程（生产者）                    worker 进程（消费者）
    ────────────────────                     ────────────────────
    ingest_document_task.delay(id, task)      ingest_document_task(id, task)   ← 同一个函数
             │ 只做一件事：把参数丢进 Redis           │
             └──────────  Redis db1  ──────────────> │
                                                     ↓
                                              run_ingest_sync(id, task)      ← 同步入口
                                                     ↓
                                              _run_ingest(id, task)          ← 真正的 async 业务
                                              跑在 run_worker_coro 提供的常驻事件循环上

⚠️ 更正一处已经过时的说法：这里【不是】 `asyncio.run`。
   教程写的是 asyncio.run（每个任务新建一个事件循环、跑完就关掉），
   但实测【第 2 个任务必崩】—— 因为数据库连接池、以及 embedder 单例里的 httpx 连接池
   都是跨任务复用的，"连接不能活得比创建它的那个循环更长"。
   现在统一改走 app/db/session.py 的 run_worker_coro（常驻事件循环）。
   完整现象、根因与并发前提见该函数的注释，或 BUG发现与处理/03_2026.9.27/03。

【任务函数为什么都这么"薄"】
因为它们必须同时满足三个硬性条件，所以只能薄：
  1. 必须是【模块级】函数 —— worker 靠 import 这个模块来发现任务，
     定义在类里、函数里或闭包里的都不行；
  2. 必须是【同步】函数 —— Celery 直接调用它，不会 await；
  3. 必须【不依赖请求上下文】—— worker 里没有 FastAPI 的 Depends、
     没有请求级数据库会话、没有当前登录用户。
因此任务函数一律只做两件事：
    把 JSON 反序列化出来的字符串转成 UUID  →  转交给真正的同步入口。
业务逻辑一行都不放在这里，全部下沉到 pipeline / service —— 这样它们才好测试。

【三个任务的关系】
    ingest_document     首次入库：下载 → 解析 → 切分 → 向量化 → 写 chunks
    reindex_document    增量重建：按 chunk_hash 对齐，只重算发生变化的切片
    finalize_upload     直传链路的前置：算指纹 → 秒传去重 → 建档 → 再投递 ingest

    ingest 与 reindex 是"真正干活的"两个；
    finalize 是直传链路的专属入口，它跑完会【再投递】一个 ingest ——
    所以"一次直传上传"在队列里体现为【两个串联的任务】。
"""

from uuid import UUID

# 【为什么本模块顶层的 import 只剩 celery_app 这一行】
# 原本 app.ingestion.pipeline 也在顶层导入，当时的理由是"它不回头 import 本模块、没有环"。
# 那个理由只回答了「会不会循环导入」，漏掉了「会不会把无关进程一起拖下水」：
#
#   app.services.document_service 在顶层 import 本模块（为了拿到 ingest_document_task 去投递），
#   于是【FastAPI 进程】也会顺着本模块把 pipeline → parser → docling / torch / transformers
#   整套深度学习推理栈加载一遍。实测线上 uvicorn 常驻 RSS 844 MB，其中绝大部分是它
#   根本用不到的推理依赖 —— 真正需要这些的只有 worker 进程。
#   在 2 核 / 3.6G 的服务器上，这 800 MB 直接把整机推到 OOM 边缘。
#
# 改成函数内导入之后：
#   · FastAPI 进程：只拿到"任务对象"用于 .delay() 投递，不再加载任何推理依赖；
#   · worker 进程：任务真正执行时才导入，且全局只导入一次（Python 的模块缓存）。
#
# 注意这与下面 finalize_upload_task 用函数内导入的理由【不同】：
#   那里是为了躲循环导入，这里是为了躲无关进程的内存开销；结论相同，理由别混。
from app.celery_app import celery_app


# ==============================================================================
# 1. 首次入库
# ==============================================================================
# 语法（Celery 任务装饰器）：@celery_app.task(name="...", bind=True)
#   参数1 (name: str) —— 任务在 broker 里的【标识】。生产者的 .delay() 与 worker 的
#                       注册表都用它来对号入座，⚠️ 它与下面的函数名【没有关系】：
#                         改函数名不影响已在队列里的消息，但改 name 会让新旧两端对不上
#                         （worker 会报 "Received unregistered task"）。
#   参数2 (bind: bool) —— 设为 True 后，任务函数的第一个参数会被注入成 self（任务实例）。
#                       它的用途是访问 self.request（任务 id、重试次数）或调用 self.retry()。
#                       ⚠️ 本项目三个任务【目前一次都没用到 self】——
#                          保留 bind=True 是照着教程的写法，同时给将来做自动重试留个位置。
#                          所以别被那个 self 绕住：它不是"类的方法"，
#                          只是 Celery 在调用时塞进来的任务对象。
@celery_app.task(name="ingest_document", bind=True)
def ingest_document_task(self, document_id: str, task_id: str) -> None:
    """首次入库任务（全量：解析 → 切分 → 向量化 → 写 chunks）。

    【两个参数为什么声明成 str 而不是 UUID】
    broker 用 JSON 序列化，UUID 落到消息体里就是字符串；而 Celery
    【不会】按类型注解帮你转回 UUID。也就是说签名上写 UUID 是骗人的 ——
    运行时拿到的仍然是 str。显式声明 str、显式转 UUID，是最不容易误判的写法。

    【两个参数分别是给谁用的】
    - document_id：告诉 pipeline"要处理哪份文档"；
    - task_id    ：告诉 pipeline"去更新 ingestion_tasks 里的哪一行台账"。
      入库过程中所有的状态与进度（running / success / failed / progress_done）
      都是靠它定位到那一行再回写的 —— 见 pipeline._mark_* 等辅助函数。

    【函数内延迟导入的原因】见模块顶部注释：把 docling / torch 这坨推理依赖
    留给真正干活的 worker 进程，别让 FastAPI 进程为了一次 .delay() 也背一份。
    """
    from app.ingestion.pipeline import run_ingest_sync

    run_ingest_sync(UUID(document_id), UUID(task_id))


# ==============================================================================
# 2. 增量重建索引
# ==============================================================================
@celery_app.task(name="reindex_document", bind=True)
def reindex_document_task(self, document_id: str, task_id: str) -> None:
    """增量重建任务（按 chunk_hash 对齐，只重算变化的切片）。

    ⚠️ 现在调用它会抛 NotImplementedError —— pipeline._run_reindex 还是占位实现，
       等下面「5、增量索引」一节补上。

       不过目前【没有任何生产者】会触发它（还没有对应的 API 端点），
       所以这个坑暂时踩不到。本节先把它注册进 worker，是为了让"任务清单"完整：
       worker 启动日志里的 [tasks] 列表一眼能看到这一章规划了哪三件事。
    """
    from app.ingestion.pipeline import run_reindex_sync

    run_reindex_sync(UUID(document_id), UUID(task_id))


# ==============================================================================
# 3. 直传收尾（教程未覆盖，本项目补）
# ==============================================================================
@celery_app.task(name="finalize_upload", bind=True)
def finalize_upload_task(self, upload_id: str) -> None:
    """直传收尾任务：算指纹 → 秒传去重 → 建档 → 投递 ingest 任务。

    【为什么只有它收一个 upload_id，而不是 document_id / task_id】
    因为在这一步【文档还没被建出来】。这条链路的前半段是：

        init 签发预签名 URL → 浏览器直传 COS → complete 校验对象
            → 【这里】finalize：算哈希、查重、把 Document 行建出来
            → 再投递 ingest_document（这一步才有 document_id 和 task_id）

    所以它拿到的是"上传会话 id"，而不是"文档 id" —— 注意第三个参数确实不存在：
    入库台账 ingestion_tasks 的那一行，也是在 finalize 建完档之后才创建的。

    【为什么这里必须用函数内延迟导入】
    app.services.document_upload_service 在模块顶层会 import 本模块
    （为了拿到 ingest_document_task 去投递），若这里也在顶层 import 它，
    两个模块就形成循环导入。

    放进函数体内之后，等任务真正被执行时两边的模块都早已加载完毕，环自然解开。
    这也是 Celery 任务里处理循环依赖的常规做法。

    【为什么这个任务是被"补"出来的，而不是教程里的】
    教程的 Celery 改造只覆盖了「旧的多段上传链路」（DocumentService.upload / retry），
    而本项目的第 3 期做过一次直传优化，前端实际走的是另一条链路：

        POST /documents/uploads/init      签发 COS 预签名 URL
        （浏览器直传 COS）
        POST /documents/uploads/{id}/complete
            → 原先：background_tasks.add_task(finalize_upload, upload_id)
                    → finalize_upload 内部再 await ingest_document(...) 把重活跑完

    如果这次只按教程改 DocumentService，会出现两种情况之一：
      ① 直传链路的 `await ingest_document(...)` 直接 ImportError（该函数已被 _run_ingest 取代）；
      ② 就算把名字修好，直传链路也依然在 API 进程里跑重活 ——
         表现为「改造看起来完成了」（旧链路的测试全绿），但真正的上传路径一点没变。
    这与第 12 期加限流时踩的坑是同一类：只改了前端不用的那条链路。

    因此这里补一个 finalize_upload 任务，把直传链路也整体搬到 Celery：
        complete_upload → finalize_upload（worker）→ ingest_document（再投一个任务）
    """
    from app.services.document_upload_service import run_finalize_upload_sync

    run_finalize_upload_sync(UUID(upload_id))


# ==============================================================================
# 附：关于三个任务的返回值
# ==============================================================================
# 上面三个任务全都返回 None。这不是疏忽，而是一个刻意的分工：
#   任务的执行结果【不靠 Celery 的 result backend】，而是写进 ingestion_tasks 表。
#
# 原因有两个：
#   · result backend 里只保留 1 小时（见 celery_app.py 的 result_expires），
#     而"这份文档上次为什么失败"是要长期可查的；
#   · 前端也不该为了看进度去连 Celery 的 result backend，查一张普通表就够。
#
# 所以 result backend（Redis db2）在本项目里只承担"即时排查"的角色，
# 真正的状态真相始终在 PostgreSQL 里 —— 这条分工与 celery_app.py 中
# task_acks_late=False 的取舍是同一个设计的一部分。
