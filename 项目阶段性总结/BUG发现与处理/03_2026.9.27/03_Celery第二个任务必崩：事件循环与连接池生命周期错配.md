# 03_Celery 第二个任务必崩：事件循环与连接池生命周期错配

> 期：**Day12 · 缓存、限流、异步任务与增量索引** → 第 4 节「Celery 异步任务」
> 发现日期：2026.09.27
> 严重级别：**高** —— 接口全部返回成功，但任务一个都没跑成，且没有任何报错暴露给调用方
> 一句话：按教程写法（每个任务 `asyncio.run`）启动 worker，**从第 2 个任务起必然失败**。

---

## 症状（极具欺骗性）

worker 日志原文（第一次真实上传）：

```
[INFO/MainProcess] Task finalize_upload[70430072-...] received
[INFO/MainProcess] Task finalize_upload[70430072-...] succeeded in 1.078s: None   ← 第 1 个任务正常

[ERROR/MainProcess] Exception terminating connection <AdaptedConnection <asyncpg.connection.Connection ...>>
Traceback (most recent call last):
  ...
  File ".../asyncpg/connection.py", line 1682, in _cancel_current_command
    self._cancellations.add(self._loop.create_task(self._cancel(waiter)))
  File ".../asyncio/base_events.py", line 455, in create_task
    self._check_closed()
RuntimeError: Event loop is closed

[INFO/MainProcess] Task ingest_document[59b130f8-...] received
[INFO/MainProcess] ingest start: document_id=bd0cdc19-...
[ERROR/MainProcess] Task ingest_document[59b130f8-...] raised unexpected:
AttributeError("'NoneType' object has no attribute 'send'")
```

**数据库侧的真实后果**：

```
documents.status        = uploading        （永远不变）
ingestion_tasks.status  = pending          （永远不变）
document_chunks         该文档 0 行
```

而**接口侧看到的是**：

```
POST /documents/uploads/{id}/complete  →  200  status=finalizing
```

也就是说：**调用方拿到的是"成功"，实际上一个 chunk 都没写进去。**
这是本次改造里最危险的一个状态组合 —— 失败没有被任何一层暴露出来。

---

## 复现方式

稳定复现，不需要任何特殊条件：**连续跑两个任务即可**。

```
1. 起 worker：python -m celery -A app.celery_app worker -l info --pool=solo
2. 上传一份文档（直传链路：init → PUT COS → complete）
3. 观察 worker 日志
```

在"直传链路改造完成"之后，一次上传本身就会投递**两个**任务
（`finalize_upload` → `ingest_document`），所以**第一次上传就必然踩中**：
第 1 个任务正常，第 2 个任务崩。

若是走旧 multipart 链路（只投递 1 个任务），则表现为"第一次上传正常，
第二次上传开始出问题"—— 更容易被误判成"某份文档有问题"。

---

## 根因：两种生命周期长度对不齐

### 事件循环的生命周期 = 一个 Celery 任务

Celery 的任务函数必须是**同步可调用对象**，而本项目的业务代码全是 `async` 的。
教程的桥接写法是：

```python
def run_ingest_sync(document_id, task_id) -> None:
    asyncio.run(_run_ingest(document_id, task_id))
```

`asyncio.run` 的语义是：**新建一个事件循环 → 跑完 → 把这个循环关掉**。

### 被缓存的 I/O 资源的生命周期 = 整个 worker 进程

worker 进程里有两类资源**不会随任务结束而释放**：

| 资源 | 位置 | 缓存方式 |
| --- | --- | --- |
| asyncpg 连接 | `app/db/session.py` 的 `engine` | SQLAlchemy 连接池是**进程级**的，用完只"归还池中"，不 close |
| httpx 连接池 | `app/ingestion/embedder.py` 的 `_embeddings` | 模块级单例，`OpenAIEmbeddings.async_client` 只在第一次用时创建一次 |

### 两者一相乘，就出事

```
第 1 个任务（loop A）
    ├─ 从池中取连接 → 池是空的 → 新建连接 C1（绑定在 loop A 上）
    ├─ 用完 → C1 归还池中（【没有关闭】）
    └─ asyncio.run 结束 → 关闭 loop A
                          ↑ 此时 C1 的底层 socket / proactor transport 已经死了

第 2 个任务（loop B）
    ├─ 从池中取连接 → 池里正好有 C1 → 取出来
    ├─ 用它执行 SELECT（还会先走 pool_pre_ping 的 ping）
    └─ asyncpg 往 C1 的 socket 写数据
         → proactor 的 _proactor 已随 loop A 一起销毁（为 None）
         → AttributeError: 'NoneType' object has no attribute 'send'
```

那句话可以概括成一条铁律：

> **连接（以及任何绑定事件循环的 I/O 资源）绝不能活得比创建它的那个事件循环更长。**

### 一个佐证：日志里那句 `Exception terminating connection`

它比 `AttributeError` 更早出现，是同一个根因的另一半：
`asyncio.run` 关闭 loop A 时，SQLAlchemy 试图优雅关闭池里的连接，
但这个"关闭"动作本身也必须在 loop A 上跑 —— 而 loop A 正在被关闭。
于是 `RuntimeError: Event loop is closed`。

**看到这句话就应该立刻想到：有连接活得比循环长。**

---

## 为什么这个 BUG 特别难查

### 1. 它不在第一次任务时出现

第一次任务里一切都是新建的，完全正常。要等到第二次任务**复用到缓存资源**时才炸。
这个"延迟一步爆发"的特性，会把人引向错误的排查方向（"第 2 个文档有问题"）。

### 2. 它会伪装成网络错误 —— 本次差点因此放过真正的根因

第一次修正尝试（`--pool=threads`）时，ingestion 报的是：

```
openai.APIConnectionError: Connection error.

The above exception was the direct cause of the following exception:
  httpcore2/_async/http_proxy.py, line 301, in handle_async_request
      stream = await stream.start_tls(**kwargs)
  httpcore2.ConnectError
```

看起来完全是"代理抽风"。当时的排查路径是：
查 Windows 系统代理（`127.0.0.1:7897`）→ 发现 `ProxyOverride` 不含百炼域名 →
手工测试 embedding **不加/加 `no_proxy` 都能通** → 说明代理本身没问题。

真实原因是：httpx 的连接池同样绑定在已关闭的循环上，
TLS 流在关闭的循环里操作，抛出来的却是 `ConnectError`。
**报错的位置离根因隔了两层抽象**，这是异步 I/O 的典型陷阱。

> 教训：当"网络错误"只在 worker 里、且只在**第二次**任务时出现，
> 先怀疑"连接池跨事件循环"，而不是先去查网络。

### 3. 它对调用方完全静默

接口返回 200，`upload_sessions.status` 还会正常推进到 `COMPLETED`。
只有去查 `documents.status` 与 `ingestion_tasks` 才能发现异常。
**如果没有 `ingestion_tasks` 这张台账表，这个 BUG 会完全隐形。**

---

## 修法：让事件循环活得和进程一样久

### 思路上的关键转变

第一反应通常是"**在每个任务结束时把被缓存的资源清理掉**"。
这条路能走通，但很脆：

- 要清理的东西是**枚举式**的：数据库连接池 + `embedder` 单例 + 将来的 chat 模型客户端
  + reranker 客户端 + …… **漏掉任何一个，BUG 就会以新的形式复发**；
- 每个任务都要重建 TCP 连接与 TLS 握手。

反过来做更稳：**让事件循环常驻，资源与它的绑定关系就永远成立。**

### 代码（`backend/app/db/session.py`）

```python
_worker_loop: asyncio.AbstractEventLoop | None = None


def run_worker_coro(coro: Coroutine[Any, Any, T]) -> T:
    """Celery worker 的任务函数专用入口：在常驻事件循环里跑协程。"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop.run_until_complete(coro)      # ← 注意：不关闭循环
```

**它与 `asyncio.run` 的唯一区别，就是最后不关循环。**

两个 sync 入口统一改用它：

```python
# app/ingestion/pipeline.py
def run_ingest_sync(document_id, task_id) -> None:
    run_worker_coro(_run_ingest(document_id, task_id))

# app/services/document_upload_service.py
def run_finalize_upload_sync(upload_id) -> None:
    run_worker_coro(_run_finalize_upload(upload_id))
```

### 为什么放在 `app/db/session.py`

因为它是"**worker 进程的运行支撑**"，与 engine / session 工厂同属一类关注点；
而且两个 sync 入口分处不同模块，放在共同的底层模块才能都被复用。

---

## 并发前提（这个修法的适用边界，必须记住）

常驻循环**只对"一个进程同一时刻只跑一个任务"成立**。因此 worker 必须用：

```
--pool=solo                        ← 推荐；Windows 上唯一无坑的选择
或 --pool=threads --concurrency=1  ← 线程池但只开一个线程
```

**不能用 `--pool=threads --concurrency=N`（N>1）**，原因有两层：

1. `_worker_loop` 是模块级全局变量，多个线程会共用同一个循环，
   在一个正在运行的循环上再调 `run_until_complete` 会直接抛 RuntimeError；
2. 就算把循环改成 thread-local，**连接池仍然是进程级共享的** ——
   线程 A 归还的连接会被线程 B 取走，同样是"跨循环复用连接"，
   只是从"先后复用"变成"并发偷取"，反而更难查。

真要让入库任务并发，还得再加一条：**worker 侧改用 `NullPool`**（每个任务独立建连接、
用完即关，池里永远没有可复用的陈旧连接）。

**本项目为什么不这么做**：入库任务是长任务（单份文档几十秒），
并发的收益远小于"多一套 NullPool + 线程本地循环"带来的排查成本。
需要提高吞吐时，更合理的方向是**多起几个 `--pool=solo` 的 worker 进程**
（进程之间天然隔离，不需要任何额外处理）—— 这也是 Celery 推荐的横向扩容方式。

---

## 可迁移的教训

1. **同步执行器 + 异步业务代码，是一类结构性风险。**
   只要出现 `asyncio.run`（或 `loop.run_until_complete` 后关循环）**加上**任何
   "进程级缓存的、绑定事件循环的资源"，就一定会踩这个坑。
   识别信号：**报错只在"第二次"出现**。

2. **优先让"生命周期对齐"，而不是逐个清理资源。**
   清理是枚举式的、会随代码增长而失效；对齐是结构性的、一次到位。

3. **异步 I/O 的报错位置常常离根因很远。**
   `AttributeError: 'NoneType' object has no attribute 'send'`、
   `ConnectError`、`RuntimeError: Event loop is closed` 看起来是三件不同的事，
   这里其实是同一个根因。看到 `Event loop is closed`，就该往生命周期方向想。

4. **"接口返回成功"和"事情真的做成了"是两件事。**
   这次能这么快定位，靠的正是 `ingestion_tasks` 台账表 ——
   它把"静默失败"变成了"一条卡在 pending 的记录"。
   异步改造中，**可观测性设施要先于功能落地**。

5. **教程的代码是"教学正确"，不等于"运行正确"。**
   教程写 `asyncio.run` 时没有任何问题意识，因为它示例里的任务只跑一次。
   把它放进"会连续跑很多次任务的 worker"里，前提就变了。
   **照抄之前先问一句：这段代码成立的前提是什么？我这里满足吗？**

---

## 修复后的验证结果

修完用**连续两份内容不同**的文档验证（第二份必须是不同内容，否则会走"秒传去重"分支，
跳过真正的入库链路，等于没测到）：

```
第 1 份
[1] init                       200  session=4bbe5759  status=initiated
[2] 直传 COS PUT               200  6704 bytes  (0.23s)
[3] complete                   200  status=finalizing  (0.48s 内返回)
       1.71s  doc=parsing   task=running  progress=0/0
       2.80s  doc=indexing  task=running  progress=0/7
       3.91s  doc=ready     task=success  progress=7/7
     -> doc=ready  chunks=7  总耗时 4.1s

第 2 份                                          ← 修复前必然崩在这里
[3] complete                   200  status=finalizing  (0.26s 内返回)
       0.36s  doc=uploading task=pending  progress=0/0
       1.46s  doc=indexing  task=running  progress=0/7
       2.56s  doc=ready     task=success  progress=7/7
     -> doc=ready  chunks=7  总耗时 2.7s

通过 2/2 份
```

worker 侧日志（与上表逐条对应，无任何 ERROR）：

```
Task finalize_upload received   → succeeded in 1.062s
Task ingest_document received   → ingest start → docling 解析
    HTTP Request: POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings "HTTP/1.1 200 OK"
    ingest done: chunks=7       → succeeded in 2.328s
Task finalize_upload received   → succeeded in 0.094s
Task ingest_document received   → ingest start → ...
    HTTP Request: POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings "HTTP/1.1 200 OK"
    ingest done: chunks=7       → succeeded in 1.5s
```

另外回归验证：

- 旧 multipart 链路：201 / 0.14s 返回，doc=ready，chunks=5，task=success
- 失败重试链路：任务台账从 1 行变 2 行，两次都 success 5/5
  （证明"新开一条台账行"而不是覆盖旧记录）
- OpenAPI paths 28 / operations 39 / 唯一 operationId 39 —— 全部不变
- 数据复原：3 份测试文档全部删除，文档数 12、chunks 123

---

## 归档交接

- 复现文件：本次验证脚本（临时目录）两个
  - `day12_celery_verify.py` —— 直传链路连续两份端到端
  - `day12_celery_verify2.py` —— 旧 multipart 链路 + 失败重试链路
- 相关代码：`backend/app/db/session.py`（`run_worker_coro` 的完整注释）
- 相关归档：`Day12_.../02_2026.9.27/04_.../05_Celery异步任务.md` 第七节
- 遗留待办（不属于本 BUG，但相邻）：
  - 卡在 `uploading` / `running` 的任务目前没有自动回收或重投入口
    （`retry` 只接受 `FAILED` 态，需要手工改状态才能重投）
  - 若将来要让入库任务并发，需按上面"并发前提"一节补 `NullPool` + 线程本地循环
