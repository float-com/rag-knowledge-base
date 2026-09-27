# 05_Celery 异步任务

> 期：**Day12 · 缓存、限流、异步任务与增量索引**
> 章：**大纲第 4 节「Celery 异步任务」**（`4.1 Celery 应用实例` / `4.2 ingestion_tasks 表` / `4.3 IngestionTaskRepository` / `4.4 Celery 任务函数和 pipeline 重构` / `4.5 DocumentService 切到 Celery` / `4.6 路由层去掉 BackgroundTasks` / `4.7 启动 worker 验证`）
> 记录日期：2026.09.27
> 说明：本篇按"我没细看代码"来写 —— 每处设计都配了"它解决什么问题、少了它会怎样"。
> 特别提示：本节教程只覆盖了**旧的多段上传链路**，本项目第 3 期还做过一次**直传优化**，
> 因此本节多出一整块教程没有的内容（第六节），以及一个教程写法在真实运行下必然触发的
> BUG（第七节）。这两块是本篇的重点。

---

## 一、本章在整条链路里的位置

### 1.1 前三节攒下的东西，到这一节才开始"离开 API 进程"

第 1~3 节引入的 Redis、语义缓存、滑动窗口限流，全部是**横切能力**：它们被挂在请求链路上，
但代码始终在 FastAPI 进程里跑。

这一节换了性质 —— 它要**搬家**：

```
前 3 节：  请求进来 → 在 API 进程里同步把活干完（或丢给 BackgroundTasks 在同一个进程里补干）
第 4 节：  请求进来 → 只做校验和落库 → 把工单投进 Redis 队列 → 立刻返回
                                                    ↓
                                        另一个进程（Celery worker）取走工单去干重活
```

一句话：**前 3 节管"进来的请求"，这一节管"出去的活"。**

### 1.2 BackgroundTasks 的三个硬伤

改造前，文档上传的解析/切分/向量化是这样跑的：

```python
background_tasks.add_task(ingest_document, document.id)
```

它有三个绕不过去的问题：

| # | 硬伤 | 后果 |
| --- | --- | --- |
| 1 | **它是"响应后执行"，不是"进程外执行"** | 重活照样占着 API 进程的 CPU、内存、数据库连接。一次上传要发几十次 embedding 请求，期间这个进程对别的请求就是卡的 |
| 2 | **进程一重启，活就丢了** | 开发时 `uvicorn --reload` 每次改代码都会重启；正在跑的入库任务连人带活一起消失，数据库里那条记录永远停在中间态，**没有任何地方能告诉你它去哪了** |
| 3 | **没有状态、没有重试、没有并发控制** | `BackgroundTasks` 只提供"排队执行"这一个语义。想查"跑到哪了"、想失败重来、想限制同时跑几个 —— 都得自己造 |

第 2 条最难忍：它不是"性能差"，而是**静默丢数据**。

### 1.3 本节的三层结构

```
① 传输层   FastAPI 进程   只负责：校验 + 写库 + 投递任务（.delay）
                 ↓ Redis db1（broker）
② 执行层   Celery worker  负责：下载、解析、切分、向量化、写 chunks
                 ↓ PostgreSQL
③ 台账层   ingestion_tasks 表  负责：回答"这次任务跑到哪了、成功还是失败"
```

---

## 二、⚠️ 开篇必须先说的：本项目有【两条】上传链路

### 2.1 教程只改了旧的那条

教程 4.5 的原文是「把 `DocumentService` 里原来的 `BackgroundTasks` 调用全部切到 Celery 的 `.delay()`」，
4.6 是「`backend/app/api/routes/documents.py` 的 `upload_document` 和 `retry_document`
去掉 `background_tasks: BackgroundTasks`」。

而本项目的实际情况是：

```
旧链路（教程覆盖）
    POST /api/documents                     multipart 上传
        → DocumentService.upload
        → background_tasks.add_task(ingest_document, ...)

直传链路（教程完全没提，但【前端实际在用】）
    POST /api/documents/uploads/init        签发 COS 预签名 URL
    （浏览器直接 PUT 到腾讯云 COS）
    POST /api/documents/uploads/{id}/complete
        → DocumentUploadService.complete_upload
        → background_tasks.add_task(self.finalize_upload, upload_id)
            → finalize_upload 内部：await ingest_document(document.id)   ← 当场跑完几十秒
```

前端 `directDocumentUpload.ts` 走的是**第二条**。

### 2.2 直传链路会直接 ImportError，而不只是"没生效"

这一处比第 3 节的限流漏挂更严重：

```
教程 4.4 把 pipeline.ingest_document 拆成
        _run_ingest(document_id, task_id)   主流程
        run_ingest_sync(...)                sync 入口给 Celery
        ↓ 拆分之后，pipeline 里【不再有 ingest_document 这个名字】
        ↓
而直传链路里有这么一行：
        document_upload_service.py   await ingest_document(document.id)
        ↓
   → ImportError，直传链路的收尾任务当场崩
```

即使手工把名字改对，直传链路也依然在 API 进程里跑重活 ——
表现为「旧链路的测试全绿、改造看起来完成了」，但真正的上传路径**一点没变**。

> 这与第 11 期漏鉴权、第 3 节漏限流是**同一个模式**：
> 按【文件】对照教程，而不是按【协议端点】对照。
> 一条流程教训：横切改造请从 OpenAPI 端点清单逐个打钩。

### 2.3 冲突清单与本节的处理方式

| # | 教程做法 | 本项目的做法 |
| --- | --- | --- |
| 1 | `pipeline.ingest_document` → `_run_ingest` + `run_ingest_sync` | 照做，**同时把 `finalize_upload` 里的调用点一并改掉** |
| 2 | 只改 `DocumentService.upload` / `retry` | **额外改造直传链路**：`finalize_upload` 变成 Celery 任务，建档后 `.delay()` 投递 ingest |
| 3 | 路由层去掉 `background_tasks`（仅 `documents.py`） | `documents.py` **和** `document_uploads.py` 都去掉 |
| 4 | 迁移里补 `DocumentChunk` 的 hnsw/gin 索引声明 | **跳过** —— 本项目第 10 期就已经声明在 `models.py` 里了 |
| 5 | `run_reindex_sync` 留到"增量索引"一节实现 | 照做，并给 `_run_reindex` 一个显式的占位实现（避免引用未定义的名字） |
| 6 | 迁移里 `drop_index('ix_documents_permission_tags')` | **本项目不需要** —— 同上，该索引已声明，autogenerate 不会生成那一行 |

---

## 三、Celery 应用实例（4.1）

### 3.1 文件位置：`backend/app/celery_app.py`

注意它**不在 `app/core/` 下**，而是直接放在 `app/` 根目录。这与 `core/redis.py` 的位置选择
看似矛盾，但理由不同：`redis.py` 是"应用侧共享资源"，而 `celery_app.py` 是**整个 worker 进程的入口**，
`-A app.celery_app` 这个启动参数直接指向它，放在浅层更好找。

### 3.2 三库隔离（第 1 节埋的伏笔在这里兑现）

```
redis db0  ←  应用数据（语义缓存 + 限流）
redis db1  ←  Celery broker    （队列里的待执行任务）
redis db2  ←  Celery result    （任务执行结果）
```

为什么要分三个库，而不是共用一个：

- 生命周期不同。清缓存（`FLUSHDB`）是日常操作，清队列是**灾难**（所有待执行任务全丢）。
- 混在一起时，"清理某一类数据"就变成"全库清空"，没有中间选项。
- 排查时 `INFO keyspace` 一眼能看出"是缓存涨了还是队列积压了"。

### 3.3 `task_acks_late=False` —— 本节最重要的一个取舍

```python
task_acks_late=False
worker_prefetch_multiplier=1
```

`task_acks_late` 的两种取值，差别**只在 worker 崩溃时体现**：

```
False（教程选的）：worker 一拿到任务就向 broker 确认"我收到了"
    → 任务从队列里消失
    → 如果 worker 进程随后崩了，这条任务【永久丢失】

True：任务真正跑完才确认
    → worker 崩了，Redis 会把任务重新投给别的 worker（至少一次语义）
    → 代价：任务可能被【重复执行】
```

教程选 False 的理由写在注释里：「业务侧用 `ingestion_tasks` 表自己跟踪状态，不靠 broker 重传保证不丢」。

这个取舍是成立的，但要**把话说完整**：

- 选 False 之后，"任务会不会丢"这个问题从 broker 转移到了我们自己身上 ——
  worker 崩溃时，`ingestion_tasks` 那一行会永远停在 `pending` 或 `running`，
  `documents.status` 永远停在 `uploading`。**没有任何东西会自动修复它。**
- 它的价值在于：**这个"卡住"是可见的**。查一次表就知道哪份文档没跑完，然后走
  `POST /api/documents/{id}/retry` 重投（不过 retry 目前只接受 `FAILED` 态，
  卡在 `uploading` 的需要手工改状态 —— 这是本节结束后仍待补的一个小口子）。
- 选 True 则需要 `_run_ingest` 严格幂等（同一个文档跑两遍不能写出两套 chunks）。
  本项目 `_run_ingest` 的写库阶段只做 `bulk_add`，**不幂等**，所以这里不能改成 True。

一句话：**教程选 False 是在"可能丢但可见"和"可能重但不幂等"之间做了取舍，前提是台账表能兜住可观测性。**

### 3.4 `worker_prefetch_multiplier=1`

默认值下，一个 worker 会一次预取"并发数 × 4"个任务囤在自己手里。入库任务是长任务（几十秒起），
囤积的后果是：**另一个 worker 明明空闲，任务却排在第 1 个 worker 后面干等**。
设成 1 之后，worker 一次只拿一个，队列里的任务能真正分到各个 worker 上。

---

## 四、任务台账表 `ingestion_tasks`（4.2）

### 4.1 为什么不是"给 documents 加一个状态字段"

`documents.status` 已经存在，为什么还要单独一张表？

因为**语义不同**：

```
documents.status        回答「这份文档现在是什么状态」   —— 只有一份，唯一真值
ingestion_tasks.status  回答「这一次尝试是什么结果」     —— 每次尝试一条，可累积
```

同一份文档会被反复入库：首次上传、失败重试、将来第 5 节的增量重建索引。
如果只存一个状态，第二遍就把第一遍的记录覆盖了 —— **"上次为什么失败"永远查不到**。

存成任务流水之后，可以回答这些 `documents.status` 根本答不了的问题：

- 这份文档一共失败过几次？
- 每次任务从投递到开工之间排队了多久？（`created_at` 与 `started_at` 的差）
- 上次失败的错误信息是什么？

### 4.2 `progress_total` / `progress_done`

向量化是全流程最慢的一步（本项目 10 个文档 259 个 chunk，要发几十次 embedding 请求）。
如果只暴露"运行中"这一个状态，前端的进度条会长时间僵在原处，用户分不清"在跑"还是"卡死了"。

所以：

```
progress_total = 切分完成后的 chunk 总数（分母，阶段 4 末尾确定）
progress_done  = 已 embedding 完成的条数（分子，每批 embedding 结束后累加）
```

`_embed_with_progress` 手动按 `EMBEDDING_BATCH_SIZE` 分批，就是为了让分子**跟着批次走**。
（LangChain 的 `aembed_documents` 内部也会分批，但回调粒度藏在 SDK 里，拿不到。）

### 4.3 ★ 教程在这里埋了一个坑：迁移里建的索引必须在模型里声明

教程 4.2 让在迁移文件里手工补一个复合索引：

```python
op.create_index(
    "ix_ingestion_tasks_document_created",
    "ingestion_tasks",
    ["document_id", sa.text("created_at DESC")],
)
```

**但教程没有说要在 `models.py` 里同步声明它。**

而 Alembic 的 `autogenerate` 只拿【模型侧推导出的结构】与【数据库实际结构】做差集 ——
迁移脚本里建的、模型里没声明的索引，在它眼里就是"数据库里多出来的东西"，
于是**下一次 autogenerate 会生成一句 `drop_index` 把它悄悄删掉**。

这正是教程自己在 4.2 节开头解释 `ix_documents_permission_tags` 时讲的那个道理
（"这是因为这两个索引没有在数据库里说明，我们在 models.py 里补一下就好了"），
却在同一节的几行之后又犯了一次。

本项目第 10 期（`DocumentChunk` 的 GIN / HNSW）与第 11 期
（`Document.ix_documents_permission_tags`）都因为这一点踩过坑，第 11 期之后已经定下规矩：
**任何索引都要在模型里声明**。

实测验证（本次改造真实跑过）：

```
不声明时：alembic revision --autogenerate 生成
    op.drop_index(op.f('ix_ingestion_tasks_document_created'), table_name='ingestion_tasks')

补上 __table_args__ 里这条声明之后，同一个命令再跑一遍：
    def upgrade() -> None:
        pass          ← 干净，零操作
```

所以本项目在 `IngestionTask` 里补了：

```python
__table_args__ = (
    Index(
        "ix_ingestion_tasks_document_created",
        "document_id",
        text("created_at DESC"),
    ),
)
```

---

## 五、仓储层与任务函数（4.3 / 4.4）

### 5.1 `IngestionTaskRepository`

一组直白的状态迁移方法：`create` / `mark_running` / `mark_success` / `mark_failed` /
`set_progress_total` / `increment_progress` / `get_latest_by_document`。

两个细节值得记：

- `mark_failed` 写入时统一 `error_message[:500]` 截断 —— 与 `_set_status` 里
  `str(exc)[:500]` 同一思路，防止超长堆栈撑爆列。
- `increment_progress(task_id, delta)` 用**增量**而不是绝对赋值，
  这样 `_embed_with_progress` 不必自己维护计数器，每批跑完加一批的条数即可。

### 5.2 `pipeline.py` 的三段式重构

改造前后的结构对比：

```
改造前：  ingest_document(document_id)                一个 async 函数从头跑到尾

改造后：  第一部分  任务状态辅助方法
              _mark_task / _mark_task_failed / _mark_task_success
              _set_task_total / _increment_task_progress
          第二部分  _embed_with_progress(texts, task_id)     分批向量化 + 上报进度
          第三部分  _run_ingest(document_id, task_id)        首次入库主流程
          最后      run_ingest_sync / run_reindex_sync       给 Celery worker 调的 sync 入口
```

第一部分的每个辅助方法都**各自开一个独立短事务**，理由与 `_set_status` 相同：
让前端轮询能立刻看到中间态，而不是等整个流程跑完才一起提交。

### 5.3 任务签名只传 UUID 字符串

```python
@celery_app.task(name="ingest_document", bind=True)
def ingest_document_task(self, document_id: str, task_id: str) -> None:
    run_ingest_sync(UUID(document_id), UUID(task_id))
```

- **不传 ORM 对象**：broker 序列化用 JSON，ORM 对象过不去。
- **不传 Session**：worker 是独立进程，它根本没有 API 进程的那个 Session。
- **不传文件内容**：几十 MB 塞进 Redis 队列不可行，worker 自己从 COS 下载。

这三条合起来就是"**消息只传标识，资源各自重取**" —— 分布式任务的基本纪律。

### 5.4 `_run_ingest` 里的两个小改进

```python
message = str(exc).strip() or exc.__class__.__name__
```

有些异常（如 `ValueError()` 不带参数）转成字符串是空的。若直接落库，
前端会看到一个**没有任何信息**的"失败"。至少有类名可查。

```python
zip(chunks, embeddings, strict=True)
```

不加 `strict=True` 时，`zip` 会"按短的截断"。一旦 embedding 少返回一条，
整份文档就会**静默少存一个切片** —— 检索时表现为"某段内容永远搜不到"，极难排查。

---

## 六、直传链路改造（教程未覆盖，本项目补）

### 6.1 改造后变成"两个 Celery 任务串联"

```
改造前（进程内串行，一条 BackgroundTasks 拖到尾）
    complete_upload
        └─ BackgroundTasks ─> finalize_upload ──await──> ingest_document（跑几十秒）

改造后（两个独立任务，各自可观测、可重投）
    complete_upload
        └─ .delay() ─> finalize_upload（worker）
                          ├─ 算 SHA256 指纹 / 秒传去重 / 建 Document
                          ├─ 落一条 ingestion_tasks 台账行
                          └─ .delay() ─> ingest_document（worker）
                                            └─ 下载 → 解析 → 切分 → 向量化 → 写 chunks
```

**这一步还顺带修掉了一个隐藏的可靠性问题**：`finalize_upload` 原先挂在 `BackgroundTasks` 上，
开发时一次 `uvicorn --reload` 重启就会让它消失，上传会话永远卡在 `FINALIZING`。
搬到 Celery 之后它由独立 worker 持有，API 进程重启不再影响它。

### 6.2 ★ 一个纯技术障碍：bound method 跨不过进程边界

这是教程没写、但一动手就撞上的问题：

```python
# 旧写法：BackgroundTasks 是"同一个进程里的事后回调"，可以传绑定方法
background_tasks.add_task(self.finalize_upload, upload_id)
```

`self.finalize_upload` 是一个**绑定方法**（bound method），它身上带着 `self`。
这在同进程里完全没问题，但换到 Celery 就撞两堵墙：

1. **序列化**：broker 用 JSON，绑定方法序列化不了。
2. **进程**：就算能序列化，worker 是另一个进程，**那里没有这个 `self` 实例**
   （没有请求级 Session、没有那份 `file_service`）。

所以必须把它"降级"成模块级、可导入、同步的可调用对象。本项目用了两级包装：

```
finalize_upload_task          app/ingestion/tasks.py        Celery 任务函数（模块级、同步）
    → run_finalize_upload_sync    document_upload_service.py    同步入口
        → _run_finalize_upload    document_upload_service.py    自己开独立 Session
            → DocumentUploadService.finalize_upload              真正的业务方法（async）
```

顺带一个配套改动：`finalize_upload` 原先自己 `async with AsyncSessionLocal()` 开会话
（因为它是被 BackgroundTasks 直接调的），现在**会话改由调用方创建并注入** ——
这才与 `init` / `complete` / `abort` 三个方法的约定一致：
服务方法只使用 `self.session`，不自己造会话。

### 6.3 为什么 `finalize_upload_task` 要用函数内延迟导入

```
document_upload_service.py  ──顶层 import──>  app.ingestion.tasks   （为了拿 ingest_document_task）
app.ingestion.tasks         ──顶层 import──>  app.services.document_upload_service   ← 循环！
```

把 service 的导入放进任务函数体内，等任务真正执行时两个模块都已加载完毕，环自然解开。
这是 Celery 任务里处理循环依赖的常规做法。

---

## 七、★ 实测撞到的 BUG：第二个任务必崩

### 7.1 现象（worker 日志原文）

按教程写法（每个任务 `asyncio.run`）启动 worker，第一次上传后：

```
Task finalize_upload received
Task finalize_upload succeeded in 1.078s          ← 第 1 个任务正常
    ERROR/MainProcess  Exception terminating connection <asyncpg...>
        RuntimeError: Event loop is closed        ← 关闭事件循环时报的
Task ingest_document received
Task ingest_document raised unexpected:
    AttributeError: 'NoneType' object has no attribute 'send'
```

结果：`ingestion_tasks` 永远停在 `pending`、`documents.status` 永远停在 `uploading`、
**一个 chunk 都没写进去** —— 而接口调用方全程看到的是"成功"。

### 7.2 根因：两种生命周期长度对不齐

```
事件循环的生命周期  = 一个 Celery 任务
    因为 Celery 任务函数必须是同步的，教程写法是每个任务 asyncio.run 一次；
    而 asyncio.run 的语义是「新建循环 → 跑完 → 把循环关掉」。

被缓存的 I/O 资源的生命周期  = 整个 worker 进程
    · SQLAlchemy 连接池是进程级、跨任务复用的（用完只"归还池中"，并不关闭）
    · app/ingestion/embedder.py 的 OpenAIEmbeddings 是模块级单例，
      它内部的 httpx 连接池同样只在第一次用时创建一次

于是：loop A 里建的连接被缓存下来，loop A 关闭；loop B 复用这条连接时，
      底层 socket / proactor transport 绑在【已关闭的 loop A】上 → 直接崩。
```

### 7.3 为什么这个 BUG 特别难查

- **它不在第一次任务时出现**。第一次任务里一切都是新建的，完全正常；
  要等到第二次任务复用到缓存资源时才炸。
  排查时极易误判成"第 2 个文档有问题""Celery 配置不对"。
- **它会伪装成网络错误**。本次实测中它还额外造出过一次
  `openai.APIConnectionError: Connection error.`（httpx 走系统代理时，
  连接池的 TLS 流属于已关闭的循环，报出来的却是 ConnectError）。
  差点被误判成"代理抽风"而放过真正的根因。

### 7.4 修法：让事件循环活得和进程一样久

思路上的关键转变 —— **与其在每个任务结束时去"逐个清理所有被缓存的 I/O 资源"
（数据库连接池 + 每一个 LLM/embedding 客户端 + 将来新加的客户端，漏一个就复发），
不如反过来：让事件循环常驻，资源与它的绑定关系就永远成立。**

```python
_worker_loop: asyncio.AbstractEventLoop | None = None

def run_worker_coro(coro):
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop.run_until_complete(coro)      # 注意：不关闭循环
```

它与 `asyncio.run` 的唯一区别就是最后**不关循环**。放在 `app/db/session.py` 里，
两个 sync 入口（`run_ingest_sync` / `run_finalize_upload_sync`）统一改用它。

**并发前提（必须记住）**：常驻循环只对"单任务串行"成立，所以 worker 必须用

```
--pool=solo                        ← 推荐，Windows 上唯一无坑的选择
或 --pool=threads --concurrency=1  ← 线程池但只开一个线程
```

若真要用 `--pool=threads --concurrency=N` 让多任务并发，还需要在 worker 侧改用 `NullPool`
并把事件循环改成"每个线程一个" —— 因为并发任务会互相取到对方循环里的连接，
光靠常驻循环堵不住。本项目入库任务是长任务，并发收益远小于排查成本，故不采用。

> 附带说明：这个 BUG 与"代理是否开启"无关。为排除干扰，本次验证的 worker 启动命令
> 加了 `no_proxy=*`（教程的启动命令里也有这一项），把系统代理从链路上摘掉。

### 7.5 worker 的启动命令（Windows）

```powershell
$env:NO_PROXY="*"; $env:no_proxy="*"      # ← 本机必须加，理由见下
python -m celery -A app.celery_app worker -l info --pool=solo
```

- **`--pool=solo`**：Windows 用不了默认的 prefork 池，必须显式指定；
  它同时也是第七节"常驻事件循环"这个修法的**前提条件**。
- **摘掉系统代理**：本机开着系统代理（`127.0.0.1:7897`），而它的 `ProxyOverride`
  里没有百炼域名。不摘的话 embedding 请求会先走代理，代理侧一抖动就报
  `openai.APIConnectionError`，而那个报错的样子极像代码 BUG —— 本次排查就被它误导过一次。
  教程的启动命令里同样带了 `no_proxy="*"`，原因就在这里。

---

## 八、验证结果（全部真实数据）

### 8.1 直传链路端到端（2/2 通过，连续两份文档）

这是**前端实际使用的那条链路**，且故意连跑两份 —— 第一份验证链路通，第二份验证
"第二个任务不再崩"。

```
[0] 登录 admin                     200  is_admin=True
第 1 份
[1] init                           200  session=4bbe5759  status=initiated
[2] 直传 COS PUT                   200  6704 bytes  (0.23s)
[3] complete                       200  status=finalizing  (0.48s 内返回)
       1.71s  doc=parsing   task=running  progress=0/0
       2.80s  doc=indexing  task=running  progress=0/7
       3.91s  doc=ready     task=success  progress=7/7
     -> doc=ready  chunks=7  总耗时 4.1s
第 2 份
[3] complete                       200  status=finalizing  (0.26s 内返回)
       0.36s  doc=uploading task=pending  progress=0/0
       1.46s  doc=indexing  task=running  progress=0/7
       2.56s  doc=ready     task=success  progress=7/7
     -> doc=ready  chunks=7  总耗时 2.7s
```

worker 侧日志（两侧对得上）：

```
Task finalize_upload received → succeeded in 1.062s
Task ingest_document received → ingest start → docling 解析 → 
    HTTP Request: POST https://dashscope.aliyuncs.com/.../embeddings "HTTP/1.1 200 OK"
    ingest done: chunks=7 → succeeded in 2.328s
Task finalize_upload received → succeeded in 0.094s
Task ingest_document received → ... → ingest done: chunks=7 → succeeded in 1.5s
```

关键观察：**接口在 0.26~0.48 秒内返回，而整份文档真正处理完要 2.7~4.1 秒** ——
这 5~10 倍的差值，就是"重活已经离开 API 进程"的直接证据。

### 8.2 旧 multipart 链路（教程覆盖的那条）

```
POST /api/documents        201  响应耗时 0.14s
       0.32s  doc=parsing
       1.18s  doc=ready
     -> doc=ready  chunks=5  tasks=['success']
```

### 8.3 失败重试链路（验证"任务流水式记录"）

```
[2] 人为置为 failed，当前任务台账行数 = 1
[3] retry                  200  响应耗时 0.06s
       0.10s  doc=parsing
       1.03s  doc=ready
     -> doc=ready  chunks=5
          task#1 [ingest] status=success progress=5/5
          task#2 [ingest] status=success progress=5/5
     重试后任务台账行数 = 2   ← 新建一条，而不是覆盖旧记录
```

### 8.4 迁移与契约回归

```
ingestion_tasks 表结构   id / document_id / task_type / status / retry_count /
                        error_message / progress_total / progress_done /
                        started_at / finished_at / created_at
索引                     ingestion_tasks_pkey
                        ix_ingestion_tasks_document_id          btree (document_id)
                        ix_ingestion_tasks_document_created     btree (document_id, created_at DESC)
外键                     document_id → documents.id  ON DELETE CASCADE

documents.version        integer  NOT NULL  DEFAULT 1   （12 行存量全部回填为 1）

alembic_version          b7f4a2c9e1d3
autogenerate 漂移检查     upgrade() 为 pass（零操作）

worker 注册的任务         finalize_upload / ingest_document / reindex_document
OpenAPI                  paths 28 / operations 39 / 唯一 operationId 39  ← 全部不变
删除 background_tasks 形参没有改变对外契约（它不是请求参数）

数据复原                 验证产生的 3 份测试文档全部删除，文档数回到 12、chunks 回到 123
```

---

## 九、本章地图

本节的直观版配图共 7 张，都在本目录下。
**01~03 是本章全景**（读完正文再看），**11~14 是「读代码顺序 · 第 1 步」的配套图**（边读代码边看）。

### 9.1 本章全景（01~03）

```
01_两条上传链路与Celery的落点.png
    蓝色 = API 进程做的事（校验、落库、投递）
    橙色 = 队列与 worker 接力做的事（收尾、再投递、入库）
    绿色 = 两条链路最终汇合的那个任务
    → 一眼看清"教程改了哪条、哪条才是前端真正在用的"

02_ingestion_tasks任务台账的生命周期.png
    蓝色 = 正常推进的四个中间态
    绿色 = 成功终态
    红色 = 两种失败形态
    → 重点是最后那条红线：worker 崩掉时，任务会永远停在 pending 或 running，
      不会自动变成 failed —— 这是 task_acks_late=False 这个取舍的代价

03_第二个任务必崩：事件循环与连接池生命周期错配.png
    红色 = 教程写法下必然发生的七步崩溃链
    绿色 = 修法（常驻事件循环）之后的三步正常链
    → 第七节那个 BUG 的一图版，与 BUG发现与处理/03_2026.9.27/03 同一份内容
```

### 9.2 读代码顺序 · 第 1 步配套图（11~14）

第 1 步要读的三个东西是 `celery_app.py` / 迁移脚本 / `models.py` 的 `IngestionTask` 段。
下面 4 张图分别对应它们，**建议边读代码边对照**：

```
11_三个Redis库各自装什么.png
    对应 celery_app.py 的 broker 与 backend 两个参数
    → 两个进程各连哪几个库、三个库各装什么、为什么必须分开
      落点：清缓存是日常操作，清队列等于丢光待办

12_ingestion_tasks台账的实体与索引.png
    对应迁移脚本 + models.py 的 IngestionTask 段（ER 图）
    → 11 个字段逐个标了"它是为回答什么问题而存在的"，
      尤其 progress_total 与 progress_done 一个当分母一个当分子

13_四个核心行为各自防什么.png
    对应 celery_app.conf.update 里的四组配置
    → 每个配置项一条「配置 → 后果」链，四条链最后收在一个结论上：
      所以必须有 ingestion_tasks 表把"卡住"变成可见

14_索引为什么必须在模型里声明.png
    对应迁移脚本与 models.py 里的两处索引声明
    → 那个坑的一图版：
      迁移里建了 → 模型里没声明 → autogenerate 判定为多余的 → 下次悄悄 drop_index
      绿色部分是修法与实测结果（补上声明后同一条命令输出 pass）
```

> 这 4 张图是配合"读代码顺序"生成的，正文第二、四、五节讲的是同一批内容，
> 但正文按**设计理由**组织，这 4 张按**文件归属**组织 —— 读代码时用后者更方便对照。

### 9.3 一句话版的整体链路

```
客户端 → API 进程（校验 + 落库 + 投递，0.3 秒内返回）
              ↓ Redis db1
         worker 收尾（算指纹 / 去重 / 建档）
              ↓ Redis db1
         worker 入库（下载 → 解析 → 切分 → 向量化 → 写 chunks，2~4 秒）
              ↓ 全程写 PostgreSQL
         ingestion_tasks 台账（前端靠它回答"跑到哪了"）
```

**本节最该记住的数字**：接口 0.26~0.48 秒返回，文档真正处理完要 2.7~4.1 秒。
这 5~10 倍的差值，就是"重活已经离开 API 进程"的直接证据。

---

## 十、自查 5 题

1. `task_acks_late=False` 之后，"任务丢了谁来兜"？为什么本项目不能改成 `True`？
2. `documents.status` 和 `ingestion_tasks.status` 各自回答什么问题？为什么后者必须是"多条"？
3. 为什么 Celery 任务函数只能收 UUID 字符串，不能收 ORM 对象或 Session？
4. 教程在迁移里建了 `ix_ingestion_tasks_document_created`，为什么还要在 `models.py` 里再写一遍？
5. `run_worker_coro` 与 `asyncio.run` 只差"最后关不关循环"，为什么这个差别是致命的？

---

## 十一、本章实际新增或修改文件

```
后端
├── backend/app/celery_app.py                                  【新增】Celery 应用实例（3 库隔离 / acks_late / prefetch）
├── backend/app/ingestion/tasks.py                             【新增】三个 Celery 任务函数
│                                                                   ingest_document / reindex_document / finalize_upload
├── backend/app/db/repositories/ingestion_task_repo.py         【新增】任务台账仓储
├── backend/alembic/versions/b7f4a2c9e1d3_*.py                 【新增】迁移：ingestion_tasks 表 + documents.version
├── backend/app/db/models.py                                   【修改】+ 两个枚举 + IngestionTask 模型
│                                                                   + Document.version + ingestion_tasks 反向关系
│                                                                   + IngestionTask.__table_args__ 复合索引声明（★ 教程漏了这步）
├── backend/app/db/session.py                                  【修改】+ run_worker_coro 常驻事件循环（★ BUG 修复）
├── backend/app/ingestion/pipeline.py                          【修改】拆成 任务状态 / 分批向量化 / 主流程 / sync 入口 四段
│                                                                   ingest_document 改名为 _run_ingest（★ 破坏性改名）
│                                                                   + zip(strict=True) + 错误信息兜底
├── backend/app/services/document_service.py                   【修改】upload / retry 去掉 BackgroundTasks 形参
│                                                                   + IngestionTaskRepository + 建台账 + .delay() 投递
├── backend/app/api/routes/documents.py                        【修改】两个端点去掉 background_tasks 形参与传参
├── backend/app/services/document_upload_service.py            【修改】★ 直传链路：complete 改投递 finalize_upload
│                                                                   finalize_upload 改用注入会话 + 建台账 + 投递 ingest
│                                                                   + _run_finalize_upload / run_finalize_upload_sync
└── backend/app/api/routes/document_uploads.py                 【修改】★ 直传链路：complete 去掉 background_tasks

项目阶段性总结
├── BUG发现与处理/03_2026.9.27/
│   └── 03_Celery第二个任务必崩：事件循环与连接池生命周期错配.md   【新增】第七节的完整记录
└── Day12_缓存、限流、异步任务与增量索引/02_2026.9.27/
    └── 04_backend_app_celery_app++backend_app_db++backend_app_ingestion++backend_app_services++backend_app_api_routes/
        ├── 05_Celery异步任务.md                              【本文件】
        ├── 01_两条上传链路与Celery的落点.png
        ├── 02_ingestion_tasks任务台账的生命周期.png
        ├── 03_第二个任务必崩：事件循环与连接池生命周期错配.png
        └── 上传日志.md
```
