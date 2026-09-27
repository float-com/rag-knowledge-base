"""Celery 应用实例。

启动 worker（Windows 必须显式指定 solo 池 —— 默认的 prefork 池在 Windows 上不可用）:
    $env:NO_PROXY="*"; $env:no_proxy="*"      # 本机需摘掉系统代理，否则 embedding 会走代理超时
    python -m celery -A app.celery_app worker -l info --pool=solo

任务定义见 `app.ingestion.tasks`（ingest_document / reindex_document / finalize_upload），
通过 include 让 worker 启动时自动发现 —— 少了它 worker 会报 "Received unregistered task"。
"""

# 导入 Celery 核心应用类
from celery import Celery

# 导入项目的全局配置对象（包含了 Redis 连接串等环境变量）
from app.core.config import settings

# ==========================================
# 步骤 1：实例化 Celery 应用对象
# ==========================================
celery_app = Celery(
    # 1. main 参数（应用命名空间）：
    #    标识当前 Celery 应用的名字，方便在监控工具（如 Flower）或日志中识别。
    "rag_knowledge_base",
    # 2. broker（任务消息代理中间件）：
    #    配置项使用的是 settings.celery_broker_url（通常指向 Redis 的 db 1）。
    #    职责：充当“邮局/消息中转站”，Web 端派发的待处理任务会排队存放在这里。
    broker=settings.celery_broker_url,
    # 3. backend（结果存储后端）：
    #    配置项使用的是 settings.celery_result_backend（通常指向 Redis 的 db 2）。
    #    职责：异步任务执行完毕后的返回值、执行状态会短暂存放在这里供查询。
    backend=settings.celery_result_backend,
    # 4. include（任务模块自动注册列表）：
    #    明确告诉 Worker 进程启动时要去自动导入并扫描哪些模块。
    #    这里写了 "app.ingestion.tasks"，Worker 启动时就会自动发现并注册里面用 @celery_app.task 装饰的后台任务。
    #    ⚠️ 少了它会怎样：那些 @celery_app.task 装饰器就【从未被执行过】，任务根本没注册。
    #       表现是生产者一切正常（.delay() 不报任何错、消息也确实进了队列），
    #       worker 端却一直报 "Received unregistered task" —— 这是 Celery 最常见的一个坑。
    include=["app.ingestion.tasks"],
)

# ==========================================
# 步骤 2：定制 Celery 运行参数（4 组：确认时机 / 预取 / 序列化 / 时区）
# ==========================================
# 文档入库是「长任务、最终一致」语义：拿到任务先 ack，业务侧用 ingestion_tasks
# 表自己跟踪状态，不靠 broker 重传保证不丢
celery_app.conf.update(
    # ------------------------------------------
    # 核心行为 1：ACK 确认时机（先确认，不靠 broker 重传）
    # ------------------------------------------
    # 【task_acks_late=False】（立即确认 / 早确认）：
    # - 它的真实行为：Worker 刚从队列里领出任务，就立刻向 Redis Broker 回 ACK（“我拿到了”）。
    #   此后这条消息在 broker 里就【不复存在】了 —— 之后 Worker 是死是活、任务是成是败，
    #   broker 都不知道也不关心。
    # - 为什么不用 True（跑完再 ACK）：
    #   ① True 的语义是 at-least-once：只要任务没被 ACK，就会【被重新投递】。
    #      而本项目的 _run_ingest 目前【不幂等】—— 写库阶段只做 bulk_add，
    #      不会先清空该文档已有的 chunks。一旦重投，同一份文档会写出【两套切片】，
    #      检索时同一段内容被命中两次、评分被拉偏，而且这个过程不报任何错。
    #   ② 入库是几十秒到几分钟的长任务，崩溃与重启的概率不低；配上重投，
    #      一个能稳定把 worker 跑崩的文档（毒丸任务）会被反复重试，白烧 embedding 算力。
    # - ⚠️ 但选 False 不是白赚的，代价必须一起记住：
    #   Worker 崩溃时任务会【静默丢失】—— ingestion_tasks 那一行永远停在 pending 或 running，
    #   documents.status 永远停在 uploading，没有任何东西会自动修复它。
    #   所以选 False 的前提是：状态真相落在业务库（ingestion_tasks 表）里，
    #   于是"卡住"这件事是【可见、可查、可手工重投】的。
    #   本质是拿"可观测性"换掉"broker 重传" —— 这是本节的核心取舍，不是纯粹调优。
    # - 📌 什么时候该回来重新评估这一项：等 _run_ingest 真正变成幂等之后。
    #   现在挡住 True 的其实只有①（重投会写出两套切片）这一条；② 只是"重复烧算力"，
    #   那是可以接受的成本。而①成立的根本原因是 pipeline.py 阶段 6 的写库
    #   只 bulk_add、不先清空旧切片（详见该处注释）。
    #   若第 5 节做增量索引时顺手让"写库前先删旧切片"成为常规动作，① 就不再成立，
    #   届时本项可以重新权衡 —— 但改动前请先确认幂等性真的落地了，再动这一行。
    task_acks_late=False,
    # ------------------------------------------
    # 核心行为 2：Worker 预取数量（长任务必须为 1）
    # ------------------------------------------
    # 【worker_prefetch_multiplier=1】（把预取压到最小）：
    # - 它到底控制什么：这一项不是"预取几条"，而是一个【乘数】：
    #       实际预取上限 = 并发数 × 该乘数
    #   乘数默认是 4，所以 --pool=solo（并发 1）下默认会预取 4 条；
    #   而 --pool=threads --concurrency=4 时默认会预取 16 条。
    # - ⚠️ 一个容易记反的地方：设成 1 是【最严格】的限制（一次只领 1 条）；
    #   设成 0 才是"不限制、能抓多少抓多少"。别把 1 理解成"禁用预取限制"。
    # - 为什么必须设为 1：
    #   极短任务（发邮件之类）预取能省网络往返；但入库任务一份要跑几十秒到几分钟，
    #   预取相当于让一个 worker 把队列里的长任务提前锁在自己手里，
    #   旁边的 worker 再闲也拿不到 —— 表现为"明明有空闲 worker，任务却排在一个 worker 后面干等"。
    #   设为 1 意味着"干完手里这 1 个，才去队列领下 1 个"，长任务才能真正均摊。
    worker_prefetch_multiplier=1,
    # ------------------------------------------
    # 核心行为 3：安全序列化协议
    # ------------------------------------------
    # 明确指定任务参数与结果均采用 JSON 格式序列化与反序列化，
    # 坚决杜绝使用旧版不安全的 pickle（防止任意代码执行反序列化漏洞）。
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ------------------------------------------
    # 核心行为 4：时区与时间格式统一
    # ------------------------------------------
    # 统一使用 UTC 时区，避免跨服务器部署、定时任务或记录时间戳时出现时区错乱。
    # ⚠️ 注意这两项的作用范围（很容易被高估）：
    #   它们只管 Celery 自己的调度（crontab 表达式按哪个时区解释）与 worker 日志的时间显示，
    #   【不会】改变业务代码里 datetime.now(timezone.utc) 的行为，
    #   也【不会】影响数据库 —— 本项目所有时间列本来就是 TIMESTAMPTZ（见 db/models.py）。
    timezone="UTC",
    enable_utc=True,
)